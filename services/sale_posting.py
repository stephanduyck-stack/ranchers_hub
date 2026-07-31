"""Sale posting (accounting Phase 3): order completion -> fiscal invoice +
balanced journal + valued COGS, then EFRIS fiscalization AFTER commit.

The double entry for a sale (each rule commented where it posts):

    DR 1100 Accounts Receivable          gross            } revenue leg
    CR 4000/4010/4100 Revenue            net, per line class
    CR 2100 VAT Output Payable           VAT on vatable net

    DR 5000/5010 COGS                    weighted-average cost of valued lines
    CR 1210 Inventory — Finished Goods   same amount      } cost leg

Line classing: export market -> 4100 (zero-rated); vatable (processed)
-> 4010/5010; the rest (fresh) -> 4000/5000.

Valuation honesty: a line whose item has no valued stock does NOT invent a
COGS figure. The sale posts its revenue leg in full; the skipped lines are
recorded on the invoice (cogs_skipped) and surface on the inventory worklist.
Books stay balanced; the gross-margin gap is visible instead of fictional.

Money: document totals are integer minor units in the ORDER currency, built
per line so the lines always sum to the header. The ledger books UGX at the
stamped rate; USD legs carry orig_currency/orig_amount_minor on the journal
lines. A reconciliation guard refuses to post when the built gross drifts
more than 1 UGX (or 1 cent) from the order's own computed total.
"""
from datetime import date, datetime

from extensions import db
from models import (AccInvoice, AccInvoiceLine, AccEfrisQueue, AccItem,
                    SalesOrder)
from services import ledger
from services import inventory_costing as inv

RETRY_BASE_MINUTES = 5           # backoff: 5, 10, 20, 40... capped in efris.py


class SalePostingError(ValueError):
    """The sale could not be posted; the order must not complete silently."""


def _revenue_key(order, line):
    if (order.market or "local") == "export":
        return "rev_export"
    return "rev_processed" if line.is_vatable else "rev_fresh"


def _cogs_key(line):
    return "cogs_processed" if line.is_vatable else "cogs_fresh"


def invoice_for_order(order):
    return db.session.scalar(
        db.select(AccInvoice).where(AccInvoice.order_id == order.id,
                                    AccInvoice.kind == "invoice",
                                    AccInvoice.status != "void"))


def post_sale(order, user_id=None):
    """Create and post the fiscal invoice + journal + COGS for a completed
    order, in ONE transaction. Idempotent: an already-invoiced order returns
    its invoice untouched. Does NOT talk to URA — call
    services.efris.try_fiscalize(invoice) after commit."""
    existing = invoice_for_order(order)
    if existing:
        return existing

    ccy = order.currency or "UGX"
    rate = float(order.exchange_rate_value) if order.exchange_rate_value else None
    if ccy != "UGX" and not rate:
        raise SalePostingError(
            f"Order {order.number} is in {ccy} with no stamped exchange rate; "
            "stamp a rate before completing.")

    def to_ugx_minor(doc_minor):
        """Document minor units -> integer UGX shillings at the stamped rate."""
        if ccy == "UGX":
            return int(doc_minor)
        return int(round(doc_minor / 100 * rate))   # cents -> units -> UGX

    # ---- build lines in document minor units (lines must sum to header) ----
    inv_lines, net_total, vat_total = [], 0, 0
    rev_by_key, vat_ugx_total = {}, 0
    for l in order.lines:
        qty = l.delivered_qty or 0
        if qty <= 0:
            continue
        line_net = ledger.to_minor(l.line_total, ccy)
        line_vat = 0
        if order.vat_applicable and l.is_vatable:
            line_vat = ledger.to_minor(
                float(l.line_total) * (order.vat_rate or 0) / 100.0, ccy)
        net_total += line_net
        vat_total += line_vat
        key = _revenue_key(order, l)
        rev_by_key[key] = rev_by_key.get(key, 0) + to_ugx_minor(line_net)
        vat_ugx_total += to_ugx_minor(line_vat)
        inv_lines.append((l, qty, line_net, line_vat))
    if not inv_lines:
        raise SalePostingError(f"Order {order.number} has no delivered lines to invoice.")
    gross_total = net_total + vat_total

    # ---- reconciliation guard: our build vs the order's own computation ----
    order_gross = ledger.to_minor(order.total, ccy)
    if abs(gross_total - order_gross) > 1:
        raise SalePostingError(
            f"Reconciliation failed on {order.number}: built gross "
            f"{gross_total:,} != order total {order_gross:,} (minor units). "
            "Refusing to post a figure that disagrees with the order.")

    # ---- COGS leg: weighted-average issue per valued line ------------------
    items_by_pid = {i.product_id: i for i in db.session.scalars(
        db.select(AccItem).where(AccItem.product_id.in_(
            [l.product_id for l, *_ in inv_lines if l.product_id]))).all()}
    cogs_by_key, cogs_movs, skipped = {}, [], []
    for l, qty, _n, _v in inv_lines:
        item = items_by_pid.get(l.product_id)
        # Invoice-after-delivery (31 Jul 2026): variance the customer did not
        # accept and the driver did not bring back (weighed short, loss) is
        # gone goods — its cost joins this sale's COGS even though only the
        # accepted quantity is billed. Margin stays honest instead of the
        # missing kilos sitting in inventory forever.
        cogs_qty = qty + (getattr(l, "cogs_extra_qty", 0) or 0)
        if item and (item.qty_on_hand or 0) + 1e-9 >= cogs_qty and (item.value_on_hand or 0) > 0:
            mv = inv.issue(item, cogs_qty, "sale", note=f"Sale {order.number}",
                           user_id=user_id, order_id=order.id, order_line_id=l.id)
            cogs_movs.append(mv)
            key = _cogs_key(l)
            cogs_by_key[key] = cogs_by_key.get(key, 0) + (-mv.value_ugx)
        else:
            skipped.append(l.description or l.article_no or f"line {l.id}")
    cogs_total = sum(cogs_by_key.values())

    # ---- journal ------------------------------------------------------------
    fx = {"orig_currency": ccy, "fx_rate": order.exchange_rate_value} \
        if ccy != "UGX" else {}
    lines = [{"account": "ar_control", "debit": to_ugx_minor(gross_total),
              "customer_id": order.customer_id,
              "orig_amount_minor": gross_total if ccy != "UGX" else None,
              "line_memo": f"Invoice for {order.number}", **fx}]
    for key, amount in sorted(rev_by_key.items()):
        if amount:
            lines.append({"account": key, "credit": amount,
                          "line_memo": "Revenue"})
    if vat_ugx_total:
        lines.append({"account": "vat_output", "credit": vat_ugx_total,
                      "line_memo": f"VAT {order.vat_rate:g}%"})
    for key, amount in sorted(cogs_by_key.items()):
        if amount:
            lines.append({"account": key, "debit": amount, "line_memo": "COGS"})
    if cogs_total:
        lines.append({"account": "inv_finished", "credit": cogs_total,
                      "line_memo": "Inventory out at weighted average"})

    entry = ledger.post_entry(
        entry_date=date.today(),
        memo=f"Sale {order.number} — {order.customer.name if order.customer else ''}",
        lines=lines, source_type="invoice", source_id=order.id,
        channel=(order.source_pricelist.channel if order.source_pricelist else None),
        user_id=user_id)

    # ---- invoice record ------------------------------------------------------
    # Born as a draft so the number can be derived from the flushed id; the
    # flip to 'posted' below is the moment the freeze trigger (acc_003) locks
    # the money and identity columns for good.
    invoice = AccInvoice(
        kind="invoice", order_id=order.id, customer_id=order.customer_id,
        buyer_name=(order.customer.name if order.customer else None),
        buyer_tin=(order.customer.tax_id if order.customer else None),
        invoice_date=date.today(), currency=ccy,
        fx_rate=order.exchange_rate_value,
        net_minor=net_total, vat_minor=vat_total, gross_minor=gross_total,
        cogs_minor=cogs_total,
        cogs_skipped=("; ".join(skipped) if skipped else None),
        journal_entry_id=entry.id, efris_status="pending",
        status="draft", created_by_id=user_id)
    db.session.add(invoice)
    db.session.flush()
    from services.order_vat import derive_number
    invoice.invoice_no = derive_number("INV", invoice.id)
    db.session.flush()
    invoice.status = "posted"
    for l, qty, line_net, line_vat in inv_lines:
        item = items_by_pid.get(l.product_id)
        db.session.add(AccInvoiceLine(
            invoice_id=invoice.id, order_line_id=l.id,
            item_id=(item.id if item else None),
            description=l.description, qty=qty,
            unit_price_minor=ledger.to_minor(l.unit_price or 0, ccy),
            net_minor=line_net,
            vat_rate=(order.vat_rate if (order.vat_applicable and l.is_vatable) else 0),
            vat_minor=line_vat,
            efris_goods_code=(item.efris_goods_code if item else None)))
    for mv in cogs_movs:
        mv.journal_entry_id = entry.id
    db.session.add(AccEfrisQueue(invoice_id=invoice.id, action="fiscalize"))
    db.session.commit()
    return invoice


def post_credit_note(invoice, user_id=None, reason=None, restock=True):
    """The ONLY correction path for a posted invoice: a credit note that
    reverses the journal and queues its own EFRIS call, in one transaction.

    restock=True returns the goods to valued inventory at the exact COGS the
    sale took out (DR 1210 / CR 5xxx), so a full credit leaves every account
    where the sale found them."""
    if invoice.kind != "invoice":
        raise SalePostingError("Only invoices take credit notes.")
    if invoice.has_credit_note:
        raise SalePostingError(f"{invoice.invoice_no} already has a credit note.")

    entry = invoice.journal_entry
    if entry is None or not entry.posted:
        raise SalePostingError("Invoice has no posted journal to reverse.")
    rev = ledger.reverse_entry(entry, user_id=user_id,
                               memo=f"Credit note for {invoice.invoice_no}"
                                    + (f" — {reason}" if reason else ""))

    if restock and invoice.cogs_minor:
        # The sale's movements carry the exact value taken; replay them
        # inverted so the goods come back at the credited cost, item by item.
        from models import AccInvMovement
        sale_movs = db.session.scalars(
            db.select(AccInvMovement).where(
                AccInvMovement.order_id == invoice.order_id,
                AccInvMovement.kind == "sale")).all()
        for mv in sale_movs:
            item = db.session.get(AccItem, mv.item_id)
            inv.receive(item, -mv.qty, -mv.value_ugx, "sale_return",
                        journal_entry_id=rev.id, user_id=user_id,
                        note=f"Credit note for {invoice.invoice_no}",
                        order_id=invoice.order_id)

    cn = AccInvoice(
        kind="credit_note", order_id=invoice.order_id,
        reverses_invoice_id=invoice.id,
        customer_id=invoice.customer_id, buyer_name=invoice.buyer_name,
        buyer_tin=invoice.buyer_tin, invoice_date=date.today(),
        currency=invoice.currency, fx_rate=invoice.fx_rate,
        net_minor=-invoice.net_minor, vat_minor=-invoice.vat_minor,
        gross_minor=-invoice.gross_minor, cogs_minor=-invoice.cogs_minor,
        journal_entry_id=rev.id, efris_status="pending",
        status="draft", created_by_id=user_id)
    db.session.add(cn)
    db.session.flush()
    from services.order_vat import derive_number
    cn.invoice_no = derive_number("CN", cn.id)
    db.session.flush()
    cn.status = "posted"
    invoice.status = "credited"
    db.session.add(AccEfrisQueue(invoice_id=cn.id, action="credit_note"))
    db.session.commit()
    return cn
