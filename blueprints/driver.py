"""Delivery driver portal: a restricted login that shows only the deliveries
assigned to this driver. The driver accepts an assignment, delivers, marks it
delivered and uploads a photo of the signed delivery note (proof of delivery)."""
import os
from datetime import datetime, date

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort, current_app, send_file, send_from_directory)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from extensions import db
from models import SalesOrder
from services.audit import log
from services import exports

bp = Blueprint("driver", __name__, url_prefix="/driver")

ACTIVE = ("ready_for_dispatch", "out_for_delivery")


@bp.before_request
@login_required
def _guard():
    if not getattr(current_user, "is_driver", False):
        abort(403)


def _my_order(order_id):
    o = db.session.get(SalesOrder, order_id)
    if o is None or o.assigned_driver_id != current_user.id:
        abort(404)
    return o


@bp.route("/")
def home():
    mine = db.session.scalars(
        db.select(SalesOrder).filter_by(assigned_driver_id=current_user.id)
        .order_by(SalesOrder.assigned_at.desc())).all()
    active = [o for o in mine if o.status in ACTIVE]
    to_accept = [o for o in active if o.status == "ready_for_dispatch"]
    on_road = [o for o in active if o.status == "out_for_delivery"]
    today = date.today()
    delivered_today = [o for o in mine if o.status == "delivered"
                       and o.delivered_at and o.delivered_at.date() == today]
    delivered = [o for o in mine if o.status == "delivered"][:20]
    return render_template("driver/home.html", active=active, delivered=delivered,
                           to_accept=to_accept, on_road=on_road,
                           n_delivered_today=len(delivered_today), today=today)


@bp.route("/order/<int:order_id>")
def order(order_id):
    o = _my_order(order_id)
    return render_template("driver/order.html", order=o)


@bp.route("/order/<int:order_id>/accept", methods=["POST"])
def accept(order_id):
    o = _my_order(order_id)
    if o.status != "ready_for_dispatch":
        abort(400)
    o.status = "out_for_delivery"
    o.driver_accepted_at = datetime.utcnow()
    o.dispatched_at = datetime.utcnow()
    log("delivery_accept", "sales_order", o.id,
        detail=f"{o.number} accepted by driver {current_user.full_name}")
    db.session.commit()
    flash("Delivery accepted. Drive safely.", "success")
    return redirect(url_for("driver.order", order_id=o.id))


@bp.route("/order/<int:order_id>/deliver", methods=["POST"])
def deliver(order_id):
    o = _my_order(order_id)
    if o.status != "out_for_delivery":
        abort(400)
    # Mobile money on delivery (26 Jul 2026): no cash on trucks. A COD order
    # cannot be confirmed delivered without the mobile money transaction
    # reference — payment happens BEFORE the goods change hands.
    if o.is_cod:
        ref = (request.form.get("momo_ref") or "").strip()
        if len(ref) < 4:
            flash("Payment first: enter the mobile money transaction reference "
                  "before confirming delivery. No cash — the customer pays by "
                  "mobile money.", "danger")
            return redirect(url_for("driver.order", order_id=o.id))
        o.momo_ref = ref[:40]
        o.momo_ref_at = datetime.utcnow()
    file = request.files.get("pod")
    if not file or not file.filename:
        flash("Attach a photo of the signed delivery note to confirm delivery.", "warning")
        return redirect(url_for("driver.order", order_id=o.id))
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".pdf"):
        flash("Attach a photo (JPG, PNG, WEBP, HEIC) or a PDF.", "danger")
        return redirect(url_for("driver.order", order_id=o.id))
    folder = os.path.join(current_app.config["UPLOAD_DIR"], "pod")
    os.makedirs(folder, exist_ok=True)
    name = f"pod_{o.id}_{datetime.utcnow():%Y%m%d%H%M%S}{ext}"
    file.save(os.path.join(folder, secure_filename(name)))
    o.pod_filename = name
    o.status = "delivered"
    o.delivered_at = datetime.utcnow()
    log("delivery_done", "sales_order", o.id,
        detail=f"{o.number} delivered by {current_user.full_name} (POD attached)")
    # Alert the customer and invite a rating.
    from models import Message
    m = Message(
        customer_id=o.customer_id, sender_type="staff",
        sender_user_id=current_user.id, sender_name="Ranchers Finest",
        body=(f"Your order {o.number} has been delivered. We hope all is well! "
              f"Tap this message to open the order and rate your delivery."),
        order_id=o.id, read_by_customer=False, read_by_staff=True)
    db.session.add(m)
    db.session.commit()
    flash("Delivery confirmed. Thank you.", "success")
    return redirect(url_for("driver.order", order_id=o.id))


@bp.route("/order/<int:order_id>/dnote.pdf")
def dnote(order_id):
    o = _my_order(order_id)
    from io import BytesIO
    pdf = exports.delivery_note_to_pdf(o)
    return send_file(BytesIO(pdf), mimetype="application/pdf",
                     download_name=f"{o.dnote_number or o.number}.pdf")


@bp.route("/order/<int:order_id>/pod")
def pod(order_id):
    o = _my_order(order_id)
    if not o.pod_filename:
        abort(404)
    folder = os.path.join(current_app.config["UPLOAD_DIR"], "pod")
    return send_from_directory(folder, o.pod_filename)
