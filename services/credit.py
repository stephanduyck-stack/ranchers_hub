"""Finance credit control (20 Jul 2026).

The order manager no longer self-declares the credit check at acceptance.
A customer is orderable when finance (CEO/CFO/finance manager, see
User.can_clear_credit) has ticked credit_cleared on the profile AND the
account is not blocked. Manual control until the accounting AR link exists.

When an order arrives (portal submit or rep place) for a customer failing
the gate, an open CreditAlert is raised (one per order) and every CFO /
finance manager account with an email address is mailed a link to the
Credit alerts queue. Acceptance of a gated order is refused and raises the
same alert, so nothing slips through even if the arrival hook missed.

Finance decides each alert:
  unblock      -> account back to ok, credit_cleared ticked (with who/when),
                  alert closed; the order manager can accept.
  keep blocked -> alert closed; the customer gets a portal message that the
                  order is on hold for credit reasons (the notify sweep
                  emails it), and the customer's reps get an email.
All decisions and alerts are written to the audit log. Mail is best effort:
a failure never blocks the order flow.
"""
from datetime import datetime

from flask import has_request_context, request, url_for

from extensions import db
from services.audit import log


def _base_url():
    from services import settings as settings_svc
    if has_request_context():
        return request.url_root.rstrip("/")
    return (settings_svc.get("app_base_url") or "").rstrip("/")


def outstanding_ugx(customer):
    """The customer's outstanding balance in whole UGX.

    Switched 27 Jul 2026 (owner's call): when the Odoo receivable snapshot
    exists on the customer it is the base — the accounting system's true
    invoices-minus-payments-minus-credits balance — plus any imported
    invoices flagged unpaid and dated AFTER the snapshot, plus the app's own
    accounting-module unpaid invoices. Never below zero (credit balances
    count as no debt for the limit check).

    Customers without a snapshot fall back to the old flag-sum: imported
    invoices flagged 'Not Paid'/'Partially Paid' at full total, which
    OVERSTATES where the flags are stale. Refresh the snapshot by loading the
    res.partner contact export."""
    if customer is None:
        return 0
    from models import Invoice, AccInvoice
    if customer.odoo_receivable is not None:
        base = float(customer.odoo_receivable)
        cutoff = customer.odoo_receivable_at
        q = db.select(db.func.coalesce(db.func.sum(Invoice.total), 0)).where(
            Invoice.customer_id == customer.id,
            Invoice.payment_status.in_(("Not Paid", "Partially Paid")))
        if cutoff is not None:
            q = q.where(Invoice.invoice_date > cutoff)
        imported = float(db.session.scalar(q) or 0)
    else:
        base = 0.0
        imported = float(db.session.scalar(
            db.select(db.func.coalesce(db.func.sum(Invoice.total), 0)).where(
                Invoice.customer_id == customer.id,
                Invoice.payment_status.in_(("Not Paid", "Partially Paid")))) or 0)
    acc = 0
    for inv in db.session.scalars(
            db.select(AccInvoice).where(AccInvoice.customer_id == customer.id,
                                        AccInvoice.kind == "invoice",
                                        AccInvoice.status == "posted")):
        bal = max((inv.gross_minor or 0) - (inv.paid_minor or 0), 0)
        if (inv.currency or "UGX") != "UGX" and inv.fx_rate:
            bal = int(round(float(bal) * float(inv.fx_rate) / 100.0))
        acc += bal
    return max(int(round(base + imported)) + int(acc), 0)


def oldest_unpaid_days(customer):
    """Age in days of the customer's OLDEST unpaid invoice (imported unpaid
    flags plus accounting-module unpaid), or None when nothing is unpaid.
    Feeds the days credit limit: any unpaid invoice older than the limit
    trips the automatic block."""
    if customer is None:
        return None
    from datetime import date as _date
    from models import Invoice, AccInvoice
    dates = []
    d1 = db.session.scalar(
        db.select(db.func.min(Invoice.invoice_date)).where(
            Invoice.customer_id == customer.id,
            Invoice.payment_status.in_(("Not Paid", "Partially Paid"))))
    if d1:
        dates.append(d1)
    d2 = db.session.scalar(
        db.select(db.func.min(AccInvoice.invoice_date)).where(
            AccInvoice.customer_id == customer.id,
            AccInvoice.kind == "invoice", AccInvoice.status == "posted",
            AccInvoice.gross_minor > AccInvoice.paid_minor))
    if d2:
        dates.append(d2)
    if not dates:
        return None
    oldest = min(dates)
    if hasattr(oldest, "date"):
        oldest = oldest.date()
    elif isinstance(oldest, str):
        oldest = _date.fromisoformat(oldest[:10])
    return (_date.today() - oldest).days


def order_total_ugx(order):
    """An order's gross total in whole UGX (USD orders at the stamped rate)."""
    if order is None:
        return 0
    total = float(order.total or 0)
    if (order.currency or "UGX") != "UGX" and order.exchange_rate_value:
        total *= float(order.exchange_rate_value)
    return int(round(total))


def gate(customer, order=None):
    """(ok, reason) — may this customer's orders be accepted for fulfilment?

    Checks, in order: manual block, finance clearance tick, then the credit
    limit. With a limit set, the account blocks once the outstanding reaches
    the limit, and (given an order) acceptance also needs headroom for the
    order's own value. A CEO/CFO/finance-manager release on the specific
    order (credit_override_at) bypasses the LIMIT check only."""
    if customer is None:
        return False, "not_cleared"
    if customer.account_status == "blocked":
        return False, "blocked"
    if not customer.credit_cleared:
        return False, "not_cleared"
    released = order is not None and order.credit_override_at
    limit = customer.credit_limit_ugx or 0
    if limit > 0 and not released:
        out = outstanding_ugx(customer)
        if out >= limit or (order is not None
                            and out + order_total_ugx(order) > limit):
            return False, "over_limit"
    # Days limit — whichever of the two limits trips first kicks in.
    days = customer.credit_days or 0
    if days > 0 and not released:
        age = oldest_unpaid_days(customer)
        if age is not None and age > days:
            return False, "overdue"
    return True, None


def finance_recipients():
    from models import User
    return [u for u in db.session.scalars(
        db.select(User).filter(User.role.in_(("cfo", "finance_manager"))))
        if u.is_active and (u.email or "").strip()]


def raise_alert(order):
    """Open a CreditAlert for a gated order (one open alert per order) and
    email finance. Returns the alert, or None when the gate passes or an
    open alert already exists. Caller commits."""
    from models import CreditAlert, Message
    ok, reason = gate(order.customer, order=order)
    if ok:
        return None
    existing = db.session.scalar(
        db.select(CreditAlert).filter_by(order_id=order.id, status="open"))
    if existing:
        return None
    alert = CreditAlert(order_id=order.id, customer_id=order.customer_id,
                        reason=reason)
    db.session.add(alert)
    log("credit_alert", "sales_order", order.id,
        detail=f"{order.number} held for finance: account {reason} "
               f"({order.customer.name})")
    # Over-limit: the customer hears IMMEDIATELY (owner's spec, 21 Jul 2026).
    # Manual blocks and missing clearance stay silent until finance decides.
    if reason in ("over_limit", "overdue"):
        why = ("insufficient credit on your account"
               if reason == "over_limit"
               else "overdue invoices on your account")
        db.session.add(Message(
            customer_id=order.customer_id, sender_type="staff",
            sender_name="Finance", order_id=order.id,
            body=(f"Your order {order.number} is on hold due to {why}. "
                  f"Please contact our finance team to settle your "
                  f"outstanding balance so the order can be released."),
            read_by_customer=False, read_by_staff=True))
    _email_finance(alert, order)
    return alert


def _email_finance(alert, order):
    from services import comms
    cust = order.customer
    if alert.reason == "over_limit":
        why = (f"the credit limit is reached (outstanding UGX "
               f"{outstanding_ugx(cust):,}, limit UGX "
               f"{(cust.credit_limit_ugx or 0):,}, this order UGX "
               f"{order_total_ugx(order):,})")
    elif alert.reason == "overdue":
        why = (f"unpaid invoices exceed the {cust.credit_days or 0}-day credit "
               f"limit (oldest unpaid {oldest_unpaid_days(cust) or 0} days)")
    elif alert.reason == "blocked":
        why = "the account is BLOCKED"
    else:
        why = "the account has no finance credit clearance"
    base = _base_url()
    link = (base + url_for("customers.credit_alerts")) if base else ""
    body = (f"Order {order.number} for {cust.name} is waiting for fulfilment "
            f"but {why}.\n"
            f"Account note: {cust.account_note or '-'}\n"
            f"Payment terms: {cust.payment_terms or '-'}\n"
            f"Order total: {order.currency} {order.total:,.0f}\n\n"
            f"Decide in the Credit alerts queue: unblock the account, or keep "
            f"it blocked (the customer and the rep are then informed the "
            f"order is on hold)."
            + (f"\n\n{link}" if link else ""))
    sent_any = False
    for u in finance_recipients():
        try:
            comms.send_email(u.email, f"Credit hold: order {order.number} — {cust.name}",
                             body)
            sent_any = True
        except Exception:
            pass
    if sent_any:
        alert.emailed_at = datetime.utcnow()


def decide(alert, user, decision, note=None):
    """Finance decision on an open alert.
    decision: 'unblock' | 'keep_blocked' | 'release_order'.
    release_order (over-limit alerts): lets THIS order past the credit limit
    without touching the limit; the customer's other orders keep gating.
    Caller commits."""
    from models import Message
    cust = alert.customer
    order = alert.order
    alert.decided_by_id = user.id
    alert.decided_at = datetime.utcnow()
    alert.note = (note or "").strip() or None
    if decision == "release_order":
        alert.status = "released"
        if order is not None:
            order.credit_override_by_id = user.id
            order.credit_override_at = datetime.utcnow()
        log("credit_release", "sales_order",
            order.id if order else cust.id,
            detail=f"{order.number if order else cust.name} released past the "
                   f"credit limit by {user.full_name}"
                   + (f": {alert.note}" if alert.note else ""))
        notify_order_managers(cust, [order] if order else [], user)
        return "released"
    if decision == "unblock":
        alert.status = "unblocked"
        cust.account_status = "ok"
        cust.credit_cleared = True
        cust.credit_cleared_by_id = user.id
        cust.credit_cleared_at = datetime.utcnow()
        log("credit_unblock", "customer", cust.id,
            detail=f"{cust.name} unblocked by {user.full_name} "
                   f"for order {order.number if order else '-'}"
                   + (f": {alert.note}" if alert.note else ""))
        # 21 Jul 2026: hand the ball straight back to the order managers.
        notify_order_managers(cust, [order] if order else [], user)
        return "unblocked"
    # keep blocked: tell the customer and the reps
    alert.status = "kept_blocked"
    if cust.account_status == "ok":
        cust.account_status = "on_hold"
    body = (f"Your order {order.number if order else ''} is on hold due to a "
            f"credit issue on your account. Please contact our finance team "
            f"to settle the outstanding balance so we can release the order.")
    if alert.note:
        body += f"\n\n{alert.note}"
    db.session.add(Message(
        customer_id=cust.id, sender_type="staff", sender_user_id=user.id,
        sender_name="Finance", body=body,
        order_id=order.id if order else None,
        read_by_customer=False, read_by_staff=True))
    _email_reps(cust, order, alert)
    log("credit_keep_blocked", "customer", cust.id,
        detail=f"{cust.name} kept blocked by {user.full_name}; order "
               f"{order.number if order else '-'} on hold"
               + (f": {alert.note}" if alert.note else ""))
    return "kept_blocked"


def notify_order_managers(cust, orders, decided_by):
    """Email every active order manager: the account is unblocked/cleared and
    the named order(s) are ready to be accepted. Best effort."""
    from models import User
    from services import comms
    oms = [u for u in db.session.scalars(
        db.select(User).filter_by(role="order_manager"))
        if u.is_active and (u.email or "").strip()]
    if not oms:
        return
    nums = ", ".join(o.number for o in orders if o) or "-"
    base = _base_url()
    lines = [f"{cust.name}'s account was unblocked and credit-cleared by "
             f"{decided_by.full_name}.",
             f"Waiting order(s) ready to accept: {nums}."]
    for o in orders:
        if o and base:
            lines.append(base + url_for("orders.detail", order_id=o.id))
    body = "\n".join(lines)
    for u in oms:
        try:
            comms.send_email(u.email,
                             f"Account unblocked: {cust.name} — order {nums} "
                             f"ready to accept", body)
        except Exception:
            pass


def _email_reps(cust, order, alert):
    from services import comms
    reps = [r for r in (cust.reps or []) if (r.email or "").strip()]
    if not reps:
        return
    body = (f"Finance decision: the account of {cust.name} stays blocked and "
            f"order {order.number if order else '-'} is ON HOLD for credit "
            f"reasons.\n"
            + (f"Note: {alert.note}\n" if alert.note else "")
            + "\nThe customer has been informed through the portal. Please "
              "follow up on payment before promising delivery.")
    for r in reps:
        try:
            comms.send_email(r.email,
                             f"Order {order.number if order else ''} on hold — "
                             f"{cust.name} (credit)", body)
        except Exception:
            pass
