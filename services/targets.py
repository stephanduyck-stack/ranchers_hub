"""Rep monthly targets and actuals.

Targets live in the rep_target table at three optional levels (total, customer,
product). Actuals for a rep in a month come from invoice history for months up
to the history cut-over, and from live orders for months after it. Product-level
actuals exist only for live months (invoice history has no product detail).
"""
from collections import defaultdict

from extensions import db
from models import (Invoice, SalesOrder, SalesHistory, RepTarget)
from services.revenue import net_ugx

CONFIRMED = ("placed", "in_fulfillment", "pending", "ready_for_dispatch",
             "out_for_delivery", "dispatched", "delivered", "fulfilled")


def cutover_idx():
    """Last month covered by uploaded invoices. Invoices are the running
    sales record from 1 Jul 2026 (topped up per upload); live app orders own
    months after the latest invoice. Falls back to the sales_history pivot
    when the invoice table is empty."""
    d = db.session.scalar(db.select(db.func.max(Invoice.invoice_date)))
    if d is not None:
        return d.year * 12 + d.month
    return db.session.scalar(
        db.select(db.func.max(SalesHistory.year * 12 + SalesHistory.month))) or 0


# Kept as a thin alias so existing callers (rep_reports, customer_export) keep
# working; every revenue figure is now net (excl VAT) in UGX.
def _ugx(o):
    return net_ugx(o)


def rep_actuals(rep, year, month):
    """Return {'total','by_customer','by_product','is_live'} for a rep in a month."""
    idx = year * 12 + month
    cut = cutover_idx()
    assigned_ids = {c.id for c in rep.assigned_customers}
    total = 0.0
    by_customer = defaultdict(float)
    by_product = defaultdict(float)
    if assigned_ids:
        if idx <= cut:
            for i in db.session.scalars(db.select(Invoice).where(
                    Invoice.customer_id.in_(assigned_ids),
                    Invoice.payment_status != "Reversed", Invoice.invoice_date.isnot(None))):
                if i.invoice_date.year * 12 + i.invoice_date.month != idx:
                    continue
                v = float(i.untaxed or 0)
                total += v
                by_customer[i.customer_id] += v
        else:
            for o in db.session.scalars(db.select(SalesOrder).where(
                    SalesOrder.customer_id.in_(assigned_ids),
                    SalesOrder.status.in_(CONFIRMED),
                    SalesOrder.order_date.isnot(None))):
                if o.order_date.year * 12 + o.order_date.month != idx:
                    continue
                # Net (excl VAT) UGX for the whole order.
                v = net_ugx(o)
                total += v
                by_customer[o.customer_id] += v
                # Product split on the SAME net basis: derive the order's UGX
                # rate from net_ugx so line totals reconcile to the customer total.
                net = float(o.subtotal or 0)
                rate = (v / net) if net else (
                    1.0 if (o.currency or "UGX") == "UGX" else 0.0)
                for l in o.lines:
                    if l.product_id:
                        by_product[l.product_id] += float(l.line_total or 0) * rate
    return {"total": total, "by_customer": dict(by_customer),
            "by_product": dict(by_product), "is_live": idx > cut}


def targets_for(rep_id, year, month):
    """Return {'total': amount or None, 'customer': {cid: amt}, 'product': {pid: amt}}."""
    rows = db.session.scalars(db.select(RepTarget).where(
        RepTarget.rep_id == rep_id, RepTarget.year == year, RepTarget.month == month)).all()
    out = {"total": None, "customer": {}, "product": {}}
    for r in rows:
        amt = float(r.amount or 0)
        if r.scope == "total":
            out["total"] = amt
        elif r.scope == "customer" and r.customer_id:
            out["customer"][r.customer_id] = amt
        elif r.scope == "product" and r.product_id:
            out["product"][r.product_id] = amt
    return out


def upsert_target(rep_id, year, month, scope, amount, customer_id=None, product_id=None):
    """Create/update one target row; amount <= 0 removes it."""
    q = db.select(RepTarget).where(
        RepTarget.rep_id == rep_id, RepTarget.year == year,
        RepTarget.month == month, RepTarget.scope == scope)
    if scope == "customer":
        q = q.where(RepTarget.customer_id == customer_id)
    elif scope == "product":
        q = q.where(RepTarget.product_id == product_id)
    row = db.session.scalar(q)
    if amount is None or amount <= 0:
        if row:
            db.session.delete(row)
        return None
    if row:
        row.amount = amount
    else:
        row = RepTarget(rep_id=rep_id, year=year, month=month, scope=scope,
                        amount=amount, customer_id=customer_id, product_id=product_id)
        db.session.add(row)
    return row
