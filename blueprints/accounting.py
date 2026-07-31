"""Accounting module (Phase 1) — journal, chart of accounts, trial balance.

Screens (all server-side gated, plain language for non-accountants):

  /accounting/journal           list of journal entries (posted + own drafts)
  /accounting/journal/new       manual journal entry form (needs post_journal)
  /accounting/journal/<id>      one entry with its lines
  /accounting/accounts          chart of accounts with balances
  /accounting/trial-balance     trial balance as of a date, debit = credit proof

Money enters as major-unit strings from the form and converts to integer UGX
shillings ONCE, through services.ledger.to_minor. All posting goes through
services.ledger.post_entry — this blueprint contains no ledger logic.
"""
from datetime import date, datetime

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort)
from flask_login import login_required, current_user

from extensions import db
from models import (AccAccount, AccJournalEntry, AccJournalLine, AccItem,
                    AccInvMovement, AccInvoice, AccEfrisQueue, SalesOrder,
                    AccSupplier, AccPurchase, AccReceipt, AccSupplierPayment,
                    Customer)
from services.permissions import has_perm
from services import ledger
from services import inventory_costing as inv

bp = Blueprint("accounting", __name__, url_prefix="/accounting")

PAGE = 50


@bp.before_request
@login_required
def _guard():
    # QA audit 5 Jul 2026 C1: never serve accounting when the database-level
    # append-only triggers are absent (non-SQLite backend, or a failed
    # install). Set by _install_acc_triggers() at boot.
    from flask import current_app
    if not current_app.config.get("ACC_DB_INTEGRITY", False):
        abort(503, description=(
            "Accounting is disabled: the database integrity triggers are not "
            "installed on this backend. See migrations/acc_00*.sql."))
    # The cashier role reaches ONLY the receipt screens (record_receipts)
    # without the rest of the accounting module; everyone else needs
    # view_accounting.
    ep = request.endpoint or ""
    if ep.startswith("accounting.receipt") and has_perm(current_user, "record_receipts"):
        return None
    if ep.startswith("accounting.shop") and has_perm(current_user, "record_shop_sales"):
        return None
    if not has_perm(current_user, "view_accounting"):
        abort(403)


def _require_post():
    if not has_perm(current_user, "post_journal"):
        abort(403)


@bp.get("/")
def index():
    return redirect(url_for("accounting.journal"))


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------
@bp.get("/journal")
def journal():
    page = request.args.get("page", 1, type=int) or 1
    q = (db.select(AccJournalEntry)
         .order_by(AccJournalEntry.entry_date.desc(), AccJournalEntry.id.desc()))
    src = request.args.get("source") or ""
    if src:
        q = q.where(AccJournalEntry.source_type == src)
    total = db.session.scalar(
        db.select(db.func.count()).select_from(q.subquery())) or 0
    entries = db.session.scalars(
        q.limit(PAGE).offset((page - 1) * PAGE)).all()
    return render_template("accounting/journal_list.html",
                           entries=entries, page=page, total=total,
                           pages=(total + PAGE - 1) // PAGE, source=src,
                           sources=AccJournalEntry.SOURCES,
                           can_post=has_perm(current_user, "post_journal"))


@bp.get("/journal/new")
def journal_new():
    _require_post()
    accounts = db.session.scalars(
        db.select(AccAccount)
        .where(AccAccount.is_postable.is_(True), AccAccount.active.is_(True))
        .order_by(AccAccount.code)).all()
    return render_template("accounting/journal_new.html",
                           accounts=accounts, today=date.today().isoformat())


@bp.post("/journal/new")
def journal_create():
    _require_post()
    try:
        entry_date = datetime.strptime(
            request.form.get("entry_date", ""), "%Y-%m-%d").date()
    except ValueError:
        flash("Give the entry a date.", "danger")
        return redirect(url_for("accounting.journal_new"))
    memo = (request.form.get("memo") or "").strip()

    # Parallel arrays from the line rows; blanks are skipped.
    accounts = request.form.getlist("account_id")
    debits = request.form.getlist("debit")
    credits = request.form.getlist("credit")
    memos = request.form.getlist("line_memo")
    lines = []
    for i, acct_id in enumerate(accounts):
        if not acct_id:
            continue
        raw_dr = (debits[i] if i < len(debits) else "").strip()
        raw_cr = (credits[i] if i < len(credits) else "").strip()
        if not raw_dr and not raw_cr:
            continue
        try:
            # Form amounts are UGX major units (shillings); ledger wants ints.
            dr = ledger.to_minor(raw_dr or 0, "UGX")
            cr = ledger.to_minor(raw_cr or 0, "UGX")
        except Exception:
            flash(f"Line {i + 1}: amounts must be numbers.", "danger")
            return redirect(url_for("accounting.journal_new"))
        lines.append({"account": int(acct_id), "debit": dr, "credit": cr,
                      "line_memo": (memos[i].strip() if i < len(memos) and memos[i] else None)})

    try:
        entry = ledger.post_entry(entry_date=entry_date, memo=memo, lines=lines,
                                  source_type="manual",
                                  user_id=current_user.id)
    except ledger.LedgerError as e:
        flash(str(e), "danger")
        return redirect(url_for("accounting.journal_new"))
    except Exception:
        db.session.rollback()
        flash("The entry was rejected by the ledger integrity checks.", "danger")
        return redirect(url_for("accounting.journal_new"))
    flash(f"Journal {entry.entry_no} posted.", "success")
    return redirect(url_for("accounting.journal_view", entry_id=entry.id))


@bp.get("/journal/<int:entry_id>")
def journal_view(entry_id):
    entry = db.session.get(AccJournalEntry, entry_id)
    if not entry:
        abort(404)
    return render_template("accounting/journal_view.html", entry=entry,
                           can_post=has_perm(current_user, "post_journal"))


@bp.post("/journal/<int:entry_id>/reverse")
def journal_reverse(entry_id):
    _require_post()
    entry = db.session.get(AccJournalEntry, entry_id)
    if not entry:
        abort(404)
    try:
        rev = ledger.reverse_entry(entry, user_id=current_user.id,
                                   memo=(request.form.get("memo") or "").strip() or None)
    except ledger.LedgerError as e:
        flash(str(e), "danger")
        return redirect(url_for("accounting.journal_view", entry_id=entry.id))
    flash(f"Posted reversing entry {rev.entry_no}.", "success")
    return redirect(url_for("accounting.journal_view", entry_id=rev.id))


# ---------------------------------------------------------------------------
# Chart of accounts
# ---------------------------------------------------------------------------
@bp.get("/accounts")
def accounts():
    accts = db.session.scalars(
        db.select(AccAccount).order_by(AccAccount.code)).all()
    balances = ledger.account_balances()
    return render_template("accounting/accounts.html",
                           accounts=accts, balances=balances)


# ---------------------------------------------------------------------------
# Invoices + EFRIS (Phase 3)
# ---------------------------------------------------------------------------
@bp.get("/invoices")
def invoices():
    from services import efris as efris_svc
    page = request.args.get("page", 1, type=int) or 1
    q = db.select(AccInvoice).order_by(AccInvoice.id.desc())
    st = request.args.get("efris") or ""
    if st:
        q = q.where(AccInvoice.efris_status == st)
    total = db.session.scalar(db.select(db.func.count()).select_from(q.subquery())) or 0
    rows = db.session.scalars(q.limit(PAGE).offset((page - 1) * PAGE)).all()
    # Completed orders with no invoice (pre-Phase-3 history): offer a backfill.
    # Invoice-after-delivery (31 Jul 2026): while the flow is on, un-invoiced
    # in-flight orders are NORMAL (they wait for the clerk and the signed
    # note) — the backfill must not sweep them into blind invoices at
    # dispatched quantities. Only accepted-recorded orders qualify.
    uq = db.select(SalesOrder).where(
        SalesOrder.status.in_(("ready_for_dispatch", "out_for_delivery",
                               "delivered", "fulfilled")),
        SalesOrder.stock_deducted.is_(True),
        ~SalesOrder.id.in_(db.select(AccInvoice.order_id)
                           .where(AccInvoice.order_id.isnot(None))))
    from services.features import feature_on as _fon
    if _fon("invoice_after_delivery"):
        uq = uq.where(SalesOrder.accepted_recorded_at.is_not(None))
    uninvoiced = db.session.scalars(uq).all()
    return render_template("accounting/invoices.html",
                           invoices=rows, page=page, total=total,
                           pages=(total + PAGE - 1) // PAGE, efris=st,
                           queue_pending=efris_svc.pending_count(),
                           uninvoiced=uninvoiced,
                           can_post=has_perm(current_user, "post_journal"))


@bp.get("/invoices/<int:inv_id>")
def invoice_view(inv_id):
    invoice = db.session.get(AccInvoice, inv_id)
    if not invoice:
        abort(404)
    return render_template("accounting/invoice_view.html", invoice=invoice,
                           can_credit=has_perm(current_user, "approve_credit_notes"))


@bp.post("/invoices/<int:inv_id>/credit-note")
def invoice_credit_note(inv_id):
    # SoD: credit-note approval is its own capability (CFO), separate from
    # posting and from receipt recording. See permissions.py.
    if not has_perm(current_user, "approve_credit_notes"):
        abort(403)
    invoice = db.session.get(AccInvoice, inv_id)
    if not invoice:
        abort(404)
    from services import sale_posting, efris as efris_svc
    try:
        cn = sale_posting.post_credit_note(
            invoice, user_id=current_user.id,
            reason=(request.form.get("reason") or "").strip() or None,
            restock=(request.form.get("restock") != "0"))
    except sale_posting.SalePostingError as e:
        flash(str(e), "danger")
        return redirect(url_for("accounting.invoice_view", inv_id=invoice.id))
    if efris_svc.try_fiscalize(cn, action="credit_note"):
        flash(f"Credit note {cn.invoice_no} posted and fiscalized "
              f"(FDN {cn.efris_fdn}).", "success")
    else:
        flash(f"Credit note {cn.invoice_no} posted; EFRIS submission queued.",
              "success")
    return redirect(url_for("accounting.invoice_view", inv_id=cn.id))


@bp.post("/invoices/backfill")
def invoices_backfill():
    """Post invoices for completed orders that predate Phase 3."""
    _require_post()
    from services import sale_posting, efris as efris_svc
    done, failed = 0, []
    # Mirror of the listing filter above: with invoice-after-delivery on,
    # orders waiting for the clerk's accepted quantities are off limits.
    bq = db.select(SalesOrder).where(
        SalesOrder.status.in_(("ready_for_dispatch", "out_for_delivery",
                               "delivered", "fulfilled")),
        SalesOrder.stock_deducted.is_(True),
        ~SalesOrder.id.in_(db.select(AccInvoice.order_id)
                           .where(AccInvoice.order_id.isnot(None))))
    from services.features import feature_on as _fon
    if _fon("invoice_after_delivery"):
        bq = bq.where(SalesOrder.accepted_recorded_at.is_not(None))
    orders = db.session.scalars(bq).all()
    for o in orders:
        try:
            invoice = sale_posting.post_sale(o, user_id=current_user.id)
            efris_svc.try_fiscalize(invoice)
            done += 1
        except sale_posting.SalePostingError as e:
            failed.append(f"{o.number}: {e}")
    msg = f"Backfill: {done} invoice(s) posted."
    if failed:
        msg += " Failed: " + "; ".join(failed[:5])
    flash(msg, "warning" if failed else "success")
    return redirect(url_for("accounting.invoices"))


@bp.post("/efris/retry")
def efris_retry():
    _require_post()
    from services import efris as efris_svc
    ok, bad = efris_svc.process_queue()
    flash(f"EFRIS queue: {ok} fiscalized, {bad} still pending.", "info")
    return redirect(url_for("accounting.invoices"))


# ---------------------------------------------------------------------------
# Own shops (Phase 7): locations, transfers, daily sales
# ---------------------------------------------------------------------------
@bp.get("/shops")
def shops():
    from models import AccLocation, AccTransfer, AccShopSale
    from services import shop_ops
    shop_ops.ensure_locations()
    locations = db.session.scalars(
        db.select(AccLocation).order_by(AccLocation.is_main.desc(),
                                        AccLocation.name)).all()
    shops_ = [l for l in locations if l.kind == "shop"]
    stock_by_shop = {l.id: shop_ops.location_stock(l.id) for l in shops_}
    linked = {c.internal_location_id: c for c in db.session.scalars(
        db.select(Customer).where(Customer.internal_location_id.isnot(None))).all()}
    customers = db.session.scalars(
        db.select(Customer).where(Customer.archived.is_(False))
        .order_by(Customer.name)).all()
    transfers = db.session.scalars(
        db.select(AccTransfer).order_by(AccTransfer.id.desc()).limit(15)).all()
    sales = db.session.scalars(
        db.select(AccShopSale).order_by(AccShopSale.id.desc()).limit(15)).all()
    items = db.session.scalars(
        db.select(AccItem).where(AccItem.active.is_(True),
                                 AccItem.stock_unit.isnot(None),
                                 AccItem.product_id.isnot(None))
        .order_by(AccItem.name)).all()
    return render_template("accounting/shops.html",
                           locations=locations, shops=shops_,
                           stock_by_shop=stock_by_shop, linked=linked,
                           customers=customers, transfers=transfers,
                           sales=sales, items=items,
                           today=date.today().isoformat(),
                           can_transfer=(has_perm(current_user, "manage_stock")
                                         or has_perm(current_user, "post_journal")),
                           can_link=has_perm(current_user, "post_journal"),
                           can_sell=has_perm(current_user, "record_shop_sales"))


@bp.post("/shops/link")
def shop_link_customer():
    if not has_perm(current_user, "post_journal"):
        abort(403)
    from models import AccLocation
    loc = db.session.get(AccLocation, request.form.get("location_id", type=int) or 0)
    if not loc or loc.kind != "shop":
        abort(404)
    # unlink whoever held this location, then link the chosen customer
    for c in db.session.scalars(db.select(Customer).where(
            Customer.internal_location_id == loc.id)).all():
        c.internal_location_id = None
    cid = request.form.get("customer_id", type=int)
    if cid:
        cust = db.session.get(Customer, cid)
        if cust:
            cust.internal_location_id = loc.id
            flash(f"'{cust.name}' is now the internal customer for {loc.name}: "
                  "their fulfilled orders become stock transfers, not invoices.",
                  "success")
    else:
        flash(f"{loc.name} unlinked — orders to it invoice normally again.", "info")
    db.session.commit()
    return redirect(url_for("accounting.shops"))


@bp.post("/shops/transfer")
def shop_transfer():
    if not (has_perm(current_user, "manage_stock")
            or has_perm(current_user, "post_journal")):
        abort(403)
    from models import AccLocation
    from services import shop_ops
    f = db.session.get(AccLocation, request.form.get("from_id", type=int) or 0)
    t = db.session.get(AccLocation, request.form.get("to_id", type=int) or 0)
    if not f or not t:
        flash("Pick both locations.", "danger")
        return redirect(url_for("accounting.shops"))
    lines = []
    for i, iid in enumerate(request.form.getlist("item_id")):
        q = (request.form.getlist("qty")[i] or "").strip()
        if iid and q:
            item = db.session.get(AccItem, int(iid))
            if item:
                lines.append((item, q))
    try:
        trf = shop_ops.post_transfer(f, t, lines,
                                     notes=(request.form.get("notes") or "").strip() or None,
                                     user_id=current_user.id)
    except (shop_ops.ShopError, ValueError) as e:
        flash(str(e), "danger")
        return redirect(url_for("accounting.shops"))
    flash(f"Transfer {trf.transfer_no} posted: {f.name} → {t.name}.", "success")
    return redirect(url_for("accounting.shops"))


@bp.get("/shops/<int:loc_id>")
def shop_detail(loc_id):
    from models import AccLocation, AccShopSale
    from services import shop_ops
    loc = db.session.get(AccLocation, loc_id)
    if not loc or loc.kind != "shop":
        abort(404)
    sales = db.session.scalars(
        db.select(AccShopSale).where(AccShopSale.location_id == loc.id)
        .order_by(AccShopSale.id.desc()).limit(30)).all()
    return render_template("accounting/shop_detail.html", loc=loc,
                           stock=shop_ops.location_stock(loc.id),
                           sales=sales, today=date.today().isoformat(),
                           can_sell=has_perm(current_user, "record_shop_sales"))


@bp.post("/shops/<int:loc_id>/sale")
def shop_sale_create(loc_id):
    if not has_perm(current_user, "record_shop_sales"):
        abort(403)
    from models import AccLocation
    from services import shop_ops
    loc = db.session.get(AccLocation, loc_id)
    if not loc or loc.kind != "shop":
        abort(404)
    lines = []
    for i, iid in enumerate(request.form.getlist("item_id")):
        q = (request.form.getlist("qty")[i] or "").strip()
        g = (request.form.getlist("gross")[i] or "").strip().replace(",", "")
        if iid and q and g:
            item = db.session.get(AccItem, int(iid))
            if item:
                lines.append((item, q, g))
    try:
        sdate = datetime.strptime(request.form.get("sale_date", ""), "%Y-%m-%d").date()
    except ValueError:
        sdate = date.today()
    try:
        sale = shop_ops.post_shop_sale(
            loc, lines, sale_date=sdate,
            notes=(request.form.get("notes") or "").strip() or None,
            user_id=current_user.id)
    except (shop_ops.ShopError, ledger.LedgerError, ValueError) as e:
        flash(str(e), "danger")
        return redirect(url_for("accounting.shop_detail", loc_id=loc.id))
    flash(f"Shop sales {sale.sale_no} posted — gross {sale.gross_minor:,}, "
          f"VAT {sale.vat_minor:,}, COGS {sale.cogs_minor:,}.", "success")
    return redirect(url_for("accounting.shop_detail", loc_id=loc.id))


# ---------------------------------------------------------------------------
# Cash & bank (Phase 5)
# ---------------------------------------------------------------------------
@bp.get("/receipts")
def receipts():
    if not (has_perm(current_user, "record_receipts")
            or has_perm(current_user, "view_accounting")):
        abort(403)
    page = request.args.get("page", 1, type=int) or 1
    q = db.select(AccReceipt).order_by(AccReceipt.id.desc())
    total = db.session.scalar(db.select(db.func.count()).select_from(q.subquery())) or 0
    rows = db.session.scalars(q.limit(PAGE).offset((page - 1) * PAGE)).all()
    return render_template("accounting/receipts.html", receipts=rows,
                           page=page, pages=(total + PAGE - 1) // PAGE,
                           can_record=has_perm(current_user, "record_receipts"),
                           can_reverse=has_perm(current_user, "post_journal"))


@bp.get("/receipts/new")
def receipt_new():
    if not has_perm(current_user, "record_receipts"):
        abort(403)
    cid = request.args.get("customer_id", type=int)
    customer = db.session.get(Customer, cid) if cid else None
    open_invoices = []
    if customer:
        open_invoices = [i for i in db.session.scalars(
            db.select(AccInvoice).where(
                AccInvoice.customer_id == customer.id,
                AccInvoice.kind == "invoice",
                AccInvoice.status == "posted")
            .order_by(AccInvoice.invoice_date)).all() if i.open_minor > 0]
    customers_with_open = db.session.scalars(
        db.select(Customer).join(AccInvoice, AccInvoice.customer_id == Customer.id)
        .where(AccInvoice.kind == "invoice", AccInvoice.status == "posted",
               AccInvoice.gross_minor > AccInvoice.paid_minor)
        .group_by(Customer.id).order_by(Customer.name)).all()
    return render_template("accounting/receipt_new.html",
                           customer=customer, open_invoices=open_invoices,
                           customers=customers_with_open,
                           methods=AccReceipt.METHODS,
                           today=date.today().isoformat())


@bp.post("/receipts/new")
def receipt_create():
    if not has_perm(current_user, "record_receipts"):
        abort(403)
    from services import cash_posting as cash
    customer = db.session.get(Customer, request.form.get("customer_id", type=int) or 0)
    if not customer:
        flash("Pick a customer.", "danger")
        return redirect(url_for("accounting.receipt_new"))
    allocations = []
    inv_ids = request.form.getlist("inv_id")
    allocs = request.form.getlist("alloc")
    currency = "UGX"
    for i, iid in enumerate(inv_ids):
        raw = (allocs[i] if i < len(allocs) else "").strip()
        if not raw:
            continue
        invoice = db.session.get(AccInvoice, int(iid))
        if not invoice:
            continue
        currency = invoice.currency
        try:
            allocations.append((invoice, ledger.to_minor(raw, invoice.currency)))
        except Exception:
            flash("Allocations must be numbers.", "danger")
            return redirect(url_for("accounting.receipt_new", customer_id=customer.id))
    try:
        rdate = datetime.strptime(request.form.get("receipt_date", ""), "%Y-%m-%d").date()
    except ValueError:
        rdate = date.today()
    try:
        r = cash.post_receipt(
            customer, allocations,
            method=request.form.get("method", "cash"),
            amount=(request.form.get("amount") or "0").replace(",", ""),
            wht=(request.form.get("wht") or "0").replace(",", "") or 0,
            currency=currency,
            fx_rate=(request.form.get("fx_rate") or None),
            receipt_date=rdate,
            notes=(request.form.get("notes") or "").strip() or None,
            user_id=current_user.id)
    except (cash.CashError, ledger.LedgerError) as e:
        flash(str(e), "danger")
        return redirect(url_for("accounting.receipt_new", customer_id=customer.id))
    flash(f"Receipt {r.receipt_no} posted — {r.currency} "
          f"{r.amount_minor:,} received"
          + (f", {r.wht_minor:,} WHT credit" if r.wht_minor else "") + ".",
          "success")
    return redirect(url_for("accounting.receipts"))


@bp.post("/receipts/<int:rid>/reverse")
def receipt_reverse(rid):
    _require_post()
    r = db.session.get(AccReceipt, rid)
    if not r:
        abort(404)
    from services import cash_posting as cash
    try:
        rev = cash.reverse_receipt(r, user_id=current_user.id,
                                   reason=(request.form.get("reason") or "").strip() or None)
    except (cash.CashError, ledger.LedgerError) as e:
        flash(str(e), "danger")
        return redirect(url_for("accounting.receipts"))
    flash(f"Receipt {r.receipt_no} reversed ({rev.entry_no}).", "success")
    return redirect(url_for("accounting.receipts"))


@bp.get("/payments")
def payments():
    page = request.args.get("page", 1, type=int) or 1
    q = db.select(AccSupplierPayment).order_by(AccSupplierPayment.id.desc())
    total = db.session.scalar(db.select(db.func.count()).select_from(q.subquery())) or 0
    rows = db.session.scalars(q.limit(PAGE).offset((page - 1) * PAGE)).all()
    return render_template("accounting/payments.html", payments=rows,
                           page=page, pages=(total + PAGE - 1) // PAGE,
                           can_pay=has_perm(current_user, "pay_suppliers"))


@bp.get("/payments/new")
def payment_new():
    if not has_perm(current_user, "pay_suppliers"):
        abort(403)
    sid = request.args.get("supplier_id", type=int)
    supplier = db.session.get(AccSupplier, sid) if sid else None
    open_bills = []
    if supplier:
        open_bills = [p for p in supplier.purchases
                      if p.status == "posted" and p.on_account
                      and p.gross_minor > (p.paid_minor or 0)]
    supplier_rows = db.session.scalars(
        db.select(AccSupplier).where(AccSupplier.active.is_(True))
        .order_by(AccSupplier.name)).all()
    return render_template("accounting/payment_new.html",
                           supplier=supplier, open_bills=open_bills,
                           suppliers=supplier_rows,
                           today=date.today().isoformat())


@bp.post("/payments/new")
def payment_create():
    if not has_perm(current_user, "pay_suppliers"):
        abort(403)
    from services import cash_posting as cash
    supplier = db.session.get(AccSupplier, request.form.get("supplier_id", type=int) or 0)
    if not supplier:
        flash("Pick a supplier.", "danger")
        return redirect(url_for("accounting.payment_new"))
    allocations = []
    for i, pid in enumerate(request.form.getlist("bill_id")):
        raw = (request.form.getlist("alloc")[i] or "").strip()
        if not raw:
            continue
        p = db.session.get(AccPurchase, int(pid))
        if p:
            try:
                allocations.append((p, ledger.to_minor(raw.replace(",", ""), "UGX")))
            except Exception:
                flash("Allocations must be numbers.", "danger")
                return redirect(url_for("accounting.payment_new", supplier_id=supplier.id))
    try:
        pay = cash.post_supplier_payment(
            supplier, allocations,
            method=request.form.get("method", "bank_ugx"),
            notes=(request.form.get("notes") or "").strip() or None,
            user_id=current_user.id)
    except (cash.CashError, ledger.LedgerError) as e:
        flash(str(e), "danger")
        return redirect(url_for("accounting.payment_new", supplier_id=supplier.id))
    flash(f"Payment {pay.payment_no} posted — UGX {pay.amount_minor:,}.", "success")
    return redirect(url_for("accounting.payments"))


@bp.get("/cash-bank")
def cash_bank():
    from services import cash_posting as cash
    return render_template("accounting/cash_bank.html",
                           balances=cash.money_balances(),
                           can_transfer=has_perm(current_user, "post_journal"),
                           can_reconcile=has_perm(current_user, "reconcile_bank"),
                           today=date.today().isoformat())


@bp.post("/cash-bank/transfer")
def cash_transfer():
    _require_post()
    from services import cash_posting as cash
    try:
        entry = cash.post_transfer(
            request.form.get("from_key"), request.form.get("to_key"),
            (request.form.get("amount") or "0").replace(",", ""),
            notes=(request.form.get("notes") or "").strip() or None,
            user_id=current_user.id)
    except (cash.CashError, ledger.LedgerError) as e:
        flash(str(e), "danger")
        return redirect(url_for("accounting.cash_bank"))
    flash(f"Transfer posted ({entry.entry_no}).", "success")
    return redirect(url_for("accounting.cash_bank"))


@bp.get("/cash-bank/reconcile/<key>")
def reconcile(key):
    if not has_perm(current_user, "reconcile_bank"):
        abort(403)
    from services import cash_posting as cash
    from services.coa import account_for
    if key not in cash.MONEY_KEYS:
        abort(404)
    acct = account_for(key)
    lines = cash.uncleared_lines(acct.id)
    cleared = cash.cleared_line_ids(acct.id)
    prior_total = 0
    if cleared:
        prior_total = db.session.scalar(
            db.select(db.func.coalesce(
                db.func.sum(AccJournalLine.debit - AccJournalLine.credit), 0))
            .where(AccJournalLine.id.in_(cleared))) or 0
    return render_template("accounting/reconcile.html", account=acct, key=key,
                           lines=lines, prior_total=prior_total,
                           today=date.today().isoformat())


@bp.post("/cash-bank/reconcile/<key>")
def reconcile_close(key):
    if not has_perm(current_user, "reconcile_bank"):
        abort(403)
    from services import cash_posting as cash
    from services.coa import account_for
    if key not in cash.MONEY_KEYS:
        abort(404)
    acct = account_for(key)
    try:
        sdate = datetime.strptime(request.form.get("statement_date", ""), "%Y-%m-%d").date()
    except ValueError:
        sdate = date.today()
    try:
        bal = ledger.to_minor((request.form.get("statement_balance") or "0").replace(",", ""), "UGX")
        line_ids = [int(x) for x in request.form.getlist("line_id")]
        recon, diff = cash.close_reconciliation(acct, sdate, bal, line_ids,
                                                user_id=current_user.id)
    except (cash.CashError, ValueError) as e:
        flash(str(e), "danger")
        return redirect(url_for("accounting.reconcile", key=key))
    if diff == 0:
        flash(f"Reconciliation closed — cleared balance equals the statement.",
              "success")
    else:
        flash(f"Saved but NOT closed: cleared balance differs from the "
              f"statement by UGX {diff:,}. Find the difference.", "warning")
    return redirect(url_for("accounting.cash_bank"))


# ---------------------------------------------------------------------------
# Suppliers + purchases (Phase 4)
# ---------------------------------------------------------------------------
def _require_purchases():
    if not has_perm(current_user, "record_purchases"):
        abort(403)


@bp.get("/suppliers")
def suppliers():
    rows = db.session.scalars(
        db.select(AccSupplier).order_by(AccSupplier.name)).all()
    return render_template("accounting/suppliers.html", suppliers=rows,
                           can_edit=has_perm(current_user, "record_purchases"))


@bp.post("/suppliers")
def supplier_create():
    _require_purchases()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("A supplier needs a name.", "danger")
        return redirect(url_for("accounting.suppliers"))
    s = AccSupplier(name=name, tin=(request.form.get("tin") or "").strip() or None,
                    vat_registered=(request.form.get("vat_registered") == "1"),
                    phone=(request.form.get("phone") or "").strip() or None,
                    payment_terms=(request.form.get("payment_terms") or "").strip() or None)
    db.session.add(s)
    db.session.commit()
    flash(f"Supplier '{s.name}' added.", "success")
    return redirect(url_for("accounting.suppliers"))


@bp.post("/suppliers/<int:sid>/edit")
def supplier_edit(sid):
    _require_purchases()
    s = db.session.get(AccSupplier, sid)
    if not s:
        abort(404)
    s.name = (request.form.get("name") or s.name).strip()
    s.tin = (request.form.get("tin") or "").strip() or None
    s.vat_registered = request.form.get("vat_registered") == "1"
    s.phone = (request.form.get("phone") or "").strip() or None
    s.payment_terms = (request.form.get("payment_terms") or "").strip() or None
    s.active = request.form.get("active") != "0"
    db.session.commit()
    flash(f"Supplier '{s.name}' updated.", "success")
    return redirect(url_for("accounting.suppliers"))


@bp.get("/purchases")
def purchases():
    page = request.args.get("page", 1, type=int) or 1
    q = db.select(AccPurchase).order_by(AccPurchase.id.desc())
    total = db.session.scalar(db.select(db.func.count()).select_from(q.subquery())) or 0
    rows = db.session.scalars(q.limit(PAGE).offset((page - 1) * PAGE)).all()
    return render_template("accounting/purchases.html", purchases=rows,
                           page=page, pages=(total + PAGE - 1) // PAGE,
                           can_record=has_perm(current_user, "record_purchases"))


@bp.get("/purchases/new")
def purchase_new():
    _require_purchases()
    supplier_rows = db.session.scalars(
        db.select(AccSupplier).where(AccSupplier.active.is_(True))
        .order_by(AccSupplier.name)).all()
    items = db.session.scalars(
        db.select(AccItem).where(AccItem.active.is_(True),
                                 AccItem.stock_unit.isnot(None))
        .order_by(AccItem.stage, AccItem.name)).all()
    expense_accounts = db.session.scalars(
        db.select(AccAccount).where(AccAccount.type.in_(("expense", "cogs")),
                                    AccAccount.is_postable.is_(True),
                                    AccAccount.active.is_(True))
        .order_by(AccAccount.code)).all()
    return render_template("accounting/purchase_new.html",
                           suppliers=supplier_rows, items=items,
                           expense_accounts=expense_accounts,
                           pay_from=AccPurchase.PAY_FROM,
                           today=date.today().isoformat())


@bp.post("/purchases/new")
def purchase_create():
    _require_purchases()
    from services import purchase_posting as pp
    supplier = db.session.get(AccSupplier, request.form.get("supplier_id", type=int) or 0)
    if not supplier:
        flash("Pick a supplier (or add one on the Suppliers page first).", "danger")
        return redirect(url_for("accounting.purchase_new"))
    try:
        pdate = datetime.strptime(request.form.get("purchase_date", ""), "%Y-%m-%d").date()
    except ValueError:
        pdate = date.today()
    lines = []
    types = request.form.getlist("ltype")
    item_ids = request.form.getlist("item_id")
    qtys = request.form.getlist("qty")
    unit_costs = request.form.getlist("unit_cost")
    accounts = request.form.getlist("expense_account")
    amounts = request.form.getlist("amount")
    descs = request.form.getlist("ldesc")
    vats = request.form.getlist("lvat")
    for i, t in enumerate(types):
        vat = (vats[i] if i < len(vats) else "") == "1"
        if t == "stock" and item_ids[i]:
            lines.append({"type": "stock", "item_id": int(item_ids[i]),
                          "qty": qtys[i], "unit_cost": unit_costs[i], "vat": vat})
        elif t == "expense" and accounts[i] and (amounts[i] or "").strip():
            lines.append({"type": "expense", "account": int(accounts[i]),
                          "amount": amounts[i],
                          "description": (descs[i] if i < len(descs) else "") or None,
                          "vat": vat})
    try:
        p = pp.post_purchase(
            supplier, lines,
            pay_from=request.form.get("pay_from", "account"),
            purchase_date=pdate,
            bill_ref=(request.form.get("bill_ref") or "").strip() or None,
            notes=(request.form.get("notes") or "").strip() or None,
            user_id=current_user.id)
    except (pp.PurchaseError, ledger.LedgerError) as e:
        flash(str(e), "danger")
        return redirect(url_for("accounting.purchase_new"))
    flash(f"Purchase {p.purchase_no} posted — UGX {p.gross_ugx_minor:,} "
          f"({'on account' if p.on_account else 'paid'}).", "success")
    return redirect(url_for("accounting.purchase_view", pid=p.id))


@bp.get("/purchases/<int:pid>")
def purchase_view(pid):
    p = db.session.get(AccPurchase, pid)
    if not p:
        abort(404)
    return render_template("accounting/purchase_view.html", purchase=p,
                           can_reverse=has_perm(current_user, "post_journal"))


@bp.post("/purchases/<int:pid>/reverse")
def purchase_reverse(pid):
    # Reversal is a posting action (finance manager and up), not data entry.
    _require_post()
    p = db.session.get(AccPurchase, pid)
    if not p:
        abort(404)
    from services import purchase_posting as pp
    try:
        rev = pp.reverse_purchase(p, user_id=current_user.id,
                                  reason=(request.form.get("reason") or "").strip() or None)
    except (pp.PurchaseError, ledger.LedgerError) as e:
        flash(str(e), "danger")
        return redirect(url_for("accounting.purchase_view", pid=p.id))
    flash(f"Purchase {p.purchase_no} reversed ({rev.entry_no}).", "success")
    return redirect(url_for("accounting.purchase_view", pid=p.id))


# ---------------------------------------------------------------------------
# Inventory valuation (Phase 2)
# ---------------------------------------------------------------------------
@bp.get("/inventory")
def inventory():
    stage = request.args.get("stage") or ""
    q = db.select(AccItem).where(AccItem.active.is_(True)).order_by(
        AccItem.stage, AccItem.name)
    if stage:
        q = q.where(AccItem.stage == stage)
    items = db.session.scalars(q).all()
    n_items = db.session.scalar(
        db.select(db.func.count(AccItem.id)).where(AccItem.active.is_(True))) or 0
    ready, blocked = inv.opening_candidates()
    return render_template(
        "accounting/inventory_list.html",
        items=items, stage=stage, stages=AccItem.STAGES,
        summary=inv.valuation_summary(), n_items=n_items,
        n_ready=len(ready), n_blocked=len(blocked),
        can_post=has_perm(current_user, "post_journal"))


@bp.post("/inventory/seed")
def inventory_seed():
    _require_post()
    created = inv.ensure_items()
    flash(f"Item registry refreshed: {created['finished']} finished, "
          f"{created['raw']} raw, {created['packaging']} packaging items added.",
          "success")
    return redirect(url_for("accounting.inventory"))


@bp.post("/inventory/opening")
def inventory_opening():
    _require_post()
    try:
        entry, n, total = inv.load_opening_stock(user_id=current_user.id)
    except (inv.CostingError, ledger.LedgerError) as e:
        flash(str(e), "danger")
        return redirect(url_for("accounting.inventory"))
    if not entry:
        flash("Nothing to load: every costed item with stock already has its "
              "opening movement.", "warning")
        return redirect(url_for("accounting.inventory"))
    flash(f"Opening stock posted: {n} items, UGX {total:,} ({entry.entry_no}).",
          "success")
    return redirect(url_for("accounting.inventory"))


@bp.get("/inventory/worklist")
def inventory_worklist():
    ready, blocked = inv.opening_candidates()
    # Also surface items with no unit at all (raw/packaging included).
    unitless = db.session.scalars(
        db.select(AccItem).where(AccItem.active.is_(True),
                                 AccItem.stock_unit.is_(None))
        .order_by(AccItem.stage, AccItem.name)).all()
    return render_template("accounting/inventory_worklist.html",
                           blocked=blocked, ready=ready, unitless=unitless,
                           can_post=has_perm(current_user, "post_journal"))


@bp.post("/inventory/item/<int:item_id>/costing")
def inventory_item_costing(item_id):
    _require_post()
    item = db.session.get(AccItem, item_id)
    if not item:
        abort(404)
    unit = (request.form.get("stock_unit") or "").strip().lower()
    if unit in ("kg", "pack", "pc"):
        item.stock_unit = unit
    pw = (request.form.get("pack_weight_kg") or "").strip()
    if pw:
        try:
            item.pack_weight_kg = float(pw)
        except ValueError:
            flash("Pack weight must be a number (kg).", "danger")
            return redirect(request.referrer or url_for("accounting.inventory_worklist"))
    mc = (request.form.get("manual_cost") or "").strip()
    if mc:
        try:
            item.manual_cost_minor = ledger.to_minor(mc, "UGX")
        except Exception:
            flash("Manual cost must be a number (UGX per unit).", "danger")
            return redirect(request.referrer or url_for("accounting.inventory_worklist"))
    stage = (request.form.get("stage") or "").strip()
    if stage in AccItem.STAGES:
        item.stage = stage
    db.session.commit()
    flash(f"Costing details saved for {item.name}.", "success")
    return redirect(request.referrer or url_for("accounting.inventory_worklist"))


@bp.get("/inventory/<int:item_id>")
def inventory_item(item_id):
    item = db.session.get(AccItem, item_id)
    if not item:
        abort(404)
    cost, source, reason = inv.unit_cost_minor(item)
    return render_template("accounting/inventory_item.html", item=item,
                           cost=cost, cost_reason=reason,
                           can_post=has_perm(current_user, "post_journal"))


# ---------------------------------------------------------------------------
# Financial reports (Phase 6)
# ---------------------------------------------------------------------------
def _period():
    """Default period: this month to date; ?from=&to= override."""
    today = date.today()
    try:
        dfrom = datetime.strptime(request.args.get("from", ""), "%Y-%m-%d").date()
    except ValueError:
        dfrom = today.replace(day=1)
    try:
        dto = datetime.strptime(request.args.get("to", ""), "%Y-%m-%d").date()
    except ValueError:
        dto = today
    return dfrom, dto


def _tb_badge():
    _rows, tdr, tcr = ledger.trial_balance()
    return {"tdr": tdr, "tcr": tcr, "ok": tdr == tcr}


@bp.get("/reports")
def fin_reports():
    return render_template("accounting/fin_reports.html", tb=_tb_badge())


@bp.get("/reports/pl")
def report_pl():
    from services import reports_finance as rf
    dfrom, dto = _period()
    return render_template("accounting/report_pl.html",
                           r=rf.profit_and_loss(dfrom, dto),
                           dfrom=dfrom, dto=dto, tb=_tb_badge())


@bp.get("/reports/balance-sheet")
def report_bs():
    from services import reports_finance as rf
    try:
        as_of = datetime.strptime(request.args.get("as_of", ""), "%Y-%m-%d").date()
    except ValueError:
        as_of = date.today()
    return render_template("accounting/report_bs.html",
                           r=rf.balance_sheet(as_of), as_of=as_of, tb=_tb_badge())


@bp.get("/reports/cash-flow")
def report_cf():
    from services import reports_finance as rf
    dfrom, dto = _period()
    return render_template("accounting/report_cf.html",
                           r=rf.cash_flow(dfrom, dto),
                           dfrom=dfrom, dto=dto, tb=_tb_badge())


@bp.get("/reports/vat")
def report_vat():
    from services import reports_finance as rf
    dfrom, dto = _period()
    return render_template("accounting/report_vat.html",
                           r=rf.vat_summary(dfrom, dto),
                           dfrom=dfrom, dto=dto, tb=_tb_badge())


@bp.get("/reports/aged-receivables")
def report_ar():
    from services import reports_finance as rf
    return render_template("accounting/report_aged.html",
                           r=rf.aged_receivables(), kind="receivables",
                           buckets=[b[2] for b in rf.BUCKETS], tb=_tb_badge())


@bp.get("/reports/aged-payables")
def report_ap():
    from services import reports_finance as rf
    return render_template("accounting/report_aged.html",
                           r=rf.aged_payables(), kind="payables",
                           buckets=[b[2] for b in rf.BUCKETS], tb=_tb_badge())


# ---------------------------------------------------------------------------
# Trial balance
# ---------------------------------------------------------------------------
@bp.get("/trial-balance")
def trial_balance():
    as_of = None
    raw = request.args.get("as_of") or ""
    if raw:
        try:
            as_of = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            flash("Bad date; showing all posted entries.", "warning")
    rows, total_dr, total_cr = ledger.trial_balance(as_of=as_of)
    return render_template("accounting/trial_balance.html",
                           rows=rows, total_dr=total_dr, total_cr=total_cr,
                           as_of=raw)
