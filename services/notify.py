"""Customer notifications: email alerts for staff portal messages.

Design (11 July 2026): messages are NOT emailed immediately. A sweep looks for
staff messages still unread DELAY_MIN minutes after creation and sends ONE
email per customer covering the whole unread batch. A customer active in the
portal reads the message inside the window and no email goes out at all.
While a batch sits unread no further emails are sent; after REMIND_HOURS a
single reminder goes out. Reading in the portal re-arms everything.

The sweep piggybacks on normal request traffic (see register_sweep) at most
once per SWEEP_INTERVAL_S per process, and can also run from cron via
notify_sweep.py for quiet hours. Everything is best-effort: a mail failure
never blocks a request, and state lives on message.emailed_at so restarts
lose nothing."""
import os
import time
from datetime import datetime, timedelta

from flask import current_app, has_request_context, request, url_for

from extensions import db
from services import comms
from services import settings as settings_svc
from services.audit import log

DELAY_MIN = 10          # minutes a message may sit unread before we email
REMIND_HOURS = 24       # one reminder when a batch stays unread this long
SWEEP_INTERVAL_S = 60   # min seconds between piggybacked sweeps per process

_last_sweep = 0.0


def _recipient_for(customer):
    """The portal login's email, else the customer record's email."""
    from models import User
    u = db.session.scalar(
        db.select(User).filter_by(customer_id=customer.id, role="customer"))
    if u and (u.email or "").strip():
        return (u.email or "").strip()
    return (customer.email or "").strip()


def _base_url(base_url=None):
    if base_url:
        return base_url.rstrip("/")
    if has_request_context():
        return request.url_root.rstrip("/")
    return (settings_svc.get("app_base_url") or "").rstrip("/")


def _send_batch(customer, msgs, base_url):
    """One email covering `msgs` (newest last). Returns (ok, reason)."""
    email = _recipient_for(customer)
    if not email:
        return (False, "no email address on file")
    root = _base_url(base_url)
    if not root:
        return (False, "no base URL (set app_base_url or run in a request)")
    company = settings_svc.get("company_name") or "Ranchers Finest"
    latest = msgs[-1]
    n_more = len(msgs) - 1
    if latest.order_id:
        cta_path = url_for("portal.order", order_id=latest.order_id)
        cta_label = "Open the order"
    else:
        cta_path = url_for("portal.messages")
        cta_label = "Open your messages"
    cta_url = root + cta_path
    sender = latest.sender_name or company
    body = latest.body + (
        f"\n\n(+ {n_more} earlier message{'s' if n_more > 1 else ''} waiting "
        f"in your portal)" if n_more else "")
    text = (
        f"{sender} sent you a message on the {company} Customer Portal:\n"
        f"\n{body}\n\n"
        f"Reply in the portal so the conversation stays in one place:\n"
        f"{cta_url}\n\n{company}\n")
    from services.email_templates import message_notification_html
    html = message_notification_html(company, sender, body, cta_url, cta_label)
    logo = os.path.join(current_app.static_folder, "img", "ranchers-logo.png")
    subject = (f"New message from {company}" if not n_more
               else f"{len(msgs)} new messages from {company}")
    ok, reason = comms.send_email(email, subject, text, html=html,
                                  inline_images={"rflogo": logo})
    log("message_email", "customer", customer.id,
        detail=f"unread-batch email to {email} ({len(msgs)} message"
               f"{'s' if len(msgs) > 1 else ''}): {'sent' if ok else reason}",
        commit=True)
    return ok, reason


def sweep(base_url=None):
    """Email customers whose staff messages sit unread past the delay.
    Returns the number of emails sent. Safe to call from anywhere."""
    from models import Message
    sent = 0
    try:
        now = datetime.utcnow()
        due_cutoff = now - timedelta(minutes=DELAY_MIN)
        remind_cutoff = now - timedelta(hours=REMIND_HOURS)
        unread = db.session.scalars(
            db.select(Message).where(
                Message.sender_type == "staff",
                Message.read_by_customer.is_(False))
            .order_by(Message.created_at)).all()
        by_cust = {}
        for m in unread:
            by_cust.setdefault(m.customer_id, []).append(m)
        for cid, msgs in by_cust.items():
            fresh = [m for m in msgs
                     if m.emailed_at is None and m.created_at <= due_cutoff]
            covered = [m for m in msgs if m.emailed_at is not None]
            newest_mail = max((m.emailed_at for m in covered), default=None)
            if fresh and (newest_mail is None or newest_mail <= remind_cutoff):
                pass          # send: new batch, or reminder territory
            elif covered and newest_mail and newest_mail <= remind_cutoff:
                fresh = msgs  # 24h reminder for a batch already emailed once
            elif fresh and newest_mail and newest_mail > remind_cutoff:
                # already covered by a recent email — fold in silently
                for m in fresh:
                    m.emailed_at = newest_mail
                db.session.commit()
                continue
            else:
                continue
            customer = fresh[0].customer
            ok, _reason = _send_batch(customer, fresh, base_url)
            stamp = now if ok else None
            if ok:
                for m in msgs:          # the email covers the whole batch
                    m.emailed_at = stamp
                db.session.commit()
                sent += 1
    except Exception:  # noqa: BLE001 — notifying must never break a request
        db.session.rollback()
        current_app.logger.exception("message notification sweep failed")
    return sent


def register_sweep(app):
    """Run the sweep on normal traffic, at most once a minute per process."""
    @app.before_request
    def _piggyback_sweep():  # noqa: ANN202
        global _last_sweep
        if time.monotonic() - _last_sweep < SWEEP_INTERVAL_S:
            return None
        _last_sweep = time.monotonic()
        sweep()
        return None
