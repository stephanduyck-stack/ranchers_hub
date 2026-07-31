"""Staff side of customer messaging. A customer's thread is visible to the reps
assigned to that customer, plus anyone who can fulfil orders, and managers/admins."""
from datetime import datetime

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort)
from flask_login import login_required, current_user

from extensions import db
from models import Message, Customer
from services.audit import log

bp = Blueprint("messages", __name__, url_prefix="/messages")


def can_see_thread(user, customer):
    if user.can_manage_all or user.can_fulfill or getattr(user, "can_accept_orders", False):
        return True
    # The CFO reads every thread — oversight over what customers are told
    # (credit chases, disputes) without any fulfilment role.
    if getattr(user, "role", None) == "cfo":
        return True
    return any(c.id == customer.id for c in user.assigned_customers)


def visible_customers(user):
    custs = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    return [c for c in custs if can_see_thread(user, c)]


def staff_unread_count(user):
    """Unread customer messages across threads this user can see."""
    cust_ids = [c.id for c in visible_customers(user)]
    if not cust_ids:
        return 0
    return db.session.scalar(
        db.select(db.func.count(Message.id)).where(
            Message.customer_id.in_(cust_ids),
            Message.sender_type == "customer",
            Message.read_by_staff.is_(False))) or 0


@bp.route("/")
@login_required
def index():
    customers = visible_customers(current_user)
    threads = []
    for c in customers:
        msgs = db.session.scalars(
            db.select(Message).filter_by(customer_id=c.id)
            .order_by(Message.created_at.desc())).all()
        if not msgs:
            continue
        unread = sum(1 for m in msgs if m.sender_type == "customer" and not m.read_by_staff)
        threads.append({"customer": c, "last": msgs[0], "unread": unread,
                        "count": len(msgs)})
    threads.sort(key=lambda t: (-(t["unread"] > 0), -t["last"].created_at.timestamp()))
    return render_template("messages/index.html", threads=threads,
                           customers=customers)


@bp.route("/<int:customer_id>", methods=["GET", "POST"])
@login_required
def thread(customer_id):
    customer = db.session.get(Customer, customer_id)
    if customer is None or not can_see_thread(current_user, customer):
        abort(403)
    if request.method == "POST":
        body = (request.form.get("body") or "").strip()
        if body:
            m = Message(
                customer_id=customer.id, sender_type="staff",
                sender_user_id=current_user.id, sender_name=current_user.full_name,
                body=body, read_by_staff=True, read_by_customer=False)
            db.session.add(m)
            log("message", "customer", customer.id, detail="staff message sent")
            db.session.commit()
            # No immediate email: the notify sweep emails the customer only if
            # the message is still unread after ~10 minutes (batched).
        return redirect(url_for("messages.thread", customer_id=customer.id))

    msgs = db.session.scalars(
        db.select(Message).filter_by(customer_id=customer.id)
        .order_by(Message.created_at)).all()
    # mark customer messages as read by staff
    changed = False
    for m in msgs:
        if m.sender_type == "customer" and not m.read_by_staff:
            m.read_by_staff = True
            changed = True
    if changed:
        db.session.commit()
    return render_template("messages/thread.html", customer=customer, msgs=msgs)
