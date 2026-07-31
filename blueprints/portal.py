"""Customer self-service portal.

A customer user sees only their allocated pricelist, can build and submit an
order (with an optional LPO attachment), and track its status. Submitted orders
wait for staff confirmation before entering fulfilment.
"""
import os
from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify, abort, current_app, Response, send_file)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from extensions import db
from models import (SalesOrder, SalesOrderLine, Pricelist, PricelistLine,
                    PricelistTier, Offer, OfferLine, Message)
from services.audit import log
from services.pricing import effective_line_price
from services.allocation import allowed_pricelists_for
from services import settings as settings_svc
from services import exports
from services import order_vat

bp = Blueprint("portal", __name__, url_prefix="/portal")

# Preferred (VAT-exclusive) selling tier to price a customer order line from.
# H6: 'incl_vat' and 'rrp' are deliberately NOT in this fallback. Pricing from a
# VAT-inclusive or shelf-price tier and then adding VAT would double-charge; if no
# net-selling tier matches, the line is treated as unavailable (see _primary_tier
# callers) rather than mispriced.
# 27 Jul 2026: 'dist_price', 'wholesale', 'retail' removed — the distributor
# workbook stores these VAT-inclusive (wholesale/retail match the B2B list's
# incl-VAT and shelf prices). Distributor lists now carry a stored 'excl_vat'
# tier (net = dist price / 1.18 for vatable products); pricing from the gross
# tiers would have double-charged VAT.
_PREF_TIERS = ["excl_vat", "price_excl_vat", "price_kg", "price_pack", "price"]
_LPO_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".doc", ".docx", ".xls", ".xlsx"}


@bp.before_request
@login_required
def _guard():
    if not current_user.is_customer_user:
        abort(403)
    if current_user.customer is None:
        abort(403)


def _outlet_ids():
    return {c.id for c in current_user.portal_outlets}


def _active_id():
    from flask import session
    chosen = session.get("portal_cust")
    if chosen and chosen in _outlet_ids():
        return chosen
    return current_user.customer_id


def _customer():
    from models import Customer
    return db.session.get(Customer, _active_id()) or current_user.customer


def _my_lists():
    return allowed_pricelists_for(_customer())


def _primary_tier(pl):
    keys = {t.key: t for t in pl.tiers}
    for k in _PREF_TIERS:
        if k in keys:
            return keys[k]
    # H6: do NOT fall back to an arbitrary first tier — it may be VAT-inclusive or
    # a shelf (RRP) price, which would be mispriced under a path that then adds
    # VAT. Return None so callers treat the line as unavailable.
    return None


def _get_my_order(order_id):
    o = db.session.get(SalesOrder, order_id)
    if o is None or o.customer_id not in _outlet_ids():
        abort(404)
    return o


@bp.route("/switch", methods=["POST"])
def switch_outlet():
    from flask import session
    cid = request.form.get("cust", type=int)
    if cid in _outlet_ids():
        session["portal_cust"] = cid
    return redirect(request.referrer or url_for("portal.home"))


@bp.route("/")
def home():
    from models import Announcement
    cust = _customer()
    orders = db.session.scalars(
        db.select(SalesOrder).filter_by(customer_id=cust.id)
        .order_by(SalesOrder.created_at.desc())).all()
    # Customers see confirmed orders only (submitted onward). A draft is an
    # attempt, not an order: surface at most one resume link (the newest draft
    # that has items in the basket) and never list drafts as orders. Empty
    # attempts older than a day are deleted here as housekeeping.
    drafts = [o for o in orders if o.status == "draft"]
    resume_draft = next((o for o in drafts if o.lines), None)
    cutoff = datetime.utcnow() - timedelta(hours=24)
    stale = [o for o in drafts
             if not o.lines and o.created_at and o.created_at < cutoff]
    if stale:
        for o in stale:
            db.session.delete(o)
        db.session.commit()
    current = [o for o in orders if o.status in (
        "submitted", "placed", "in_fulfillment", "pending",
        "ready_for_dispatch", "out_for_delivery", "dispatched")]
    # A cancelled order only counts as an order if it was submitted first;
    # cancelled drafts are attempts and stay hidden.
    past = [o for o in orders if o.status in ("delivered", "fulfilled")
            or (o.status == "cancelled" and o.submitted_at)]

    seg = cust.segment or "customer"
    promos = [a for a in db.session.scalars(
        db.select(Announcement).order_by(Announcement.created_at.desc()))
        if a.is_live() and a.matches(seg)]
    offers = db.session.scalars(
        db.select(Offer).filter_by(customer_id=cust.id)
        .order_by(Offer.created_at.desc())).all()
    offers = [o for o in offers if o.status in ("issued", "converted")]

    recent_msgs = db.session.scalars(
        db.select(Message).filter_by(customer_id=cust.id)
        .order_by(Message.created_at.desc()).limit(5)).all()
    recent_msgs = list(reversed(recent_msgs))
    unread_msgs = sum(1 for m in recent_msgs if m.sender_type == "staff" and not m.read_by_customer)
    return render_template("portal/home.html", cust=cust, lists=_my_lists(),
                           resume_draft=resume_draft, current=current, past=past, promos=promos,
                           offers=offers, recent_msgs=recent_msgs, unread_msgs=unread_msgs)


# ---------------------------------------------------------------------------
# Offers visible to the customer
# ---------------------------------------------------------------------------
def _my_offer(offer_id):
    o = db.session.get(Offer, offer_id)
    if o is None or o.customer_id not in _outlet_ids() or o.status == "draft":
        abort(404)
    return o


@bp.route("/offer/<int:offer_id>")
def offer(offer_id):
    return render_template("portal/offer.html", offer=_my_offer(offer_id))


@bp.route("/offer/<int:offer_id>/pdf")
def offer_pdf(offer_id):
    o = _my_offer(offer_id)
    data = exports.offer_to_pdf(o)
    disp = "inline" if request.args.get("view") == "1" else "attachment"
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f"{disp}; filename=Quote_{o.number}.pdf"})


@bp.route("/offer/<int:offer_id>/accept", methods=["POST"])
def accept_offer(offer_id):
    o = _my_offer(offer_id)
    if o.status != "issued":
        flash("This quote can no longer be accepted online. Please contact us.", "warning")
        return redirect(url_for("portal.offer", offer_id=o.id))
    order = SalesOrder(
        customer_id=o.customer_id,
        source_pricelist_id=o.source_pricelist_id, currency=o.currency,
        market=o.market, vat_applicable=o.vat_applicable, vat_rate=o.vat_rate,
        exchange_rate_value=o.exchange_rate_value, exchange_rate_id=o.exchange_rate_id,
        status="submitted", order_date=date.today(), submitted_at=datetime.utcnow(),
        notes=f"Accepted from offer {o.number} by customer.", created_by=current_user.id)
    db.session.add(order)
    db.session.flush()
    order.number = order_vat.derive_number("SO", order.id)   # C3: id-derived number
    for ol in o.lines:
        db.session.add(SalesOrderLine(
            order_id=order.id, product_id=ol.product_id, description=ol.description,
            article_no=ol.article_no, pack_size=ol.pack_size, tier_label=ol.tier_label,
            quantity=ol.quantity, unit_price=ol.unit_price, discount_pct=ol.discount_pct,
            is_fixed=ol.is_fixed, fixed_note=ol.fixed_note,
            vat_applicable=ol.vat_applicable, sort_order=ol.sort_order))
    o.status = "converted"
    o.converted_order_id = order.id
    log("offer_accept", "offer", o.id, detail=f"{o.number} accepted by customer -> {order.number}")
    db.session.commit()
    flash(f"Quote accepted — order {order.number} submitted. We'll confirm it shortly.", "success")
    return redirect(url_for("portal.order", order_id=order.id))


# ---------------------------------------------------------------------------
# Messages (customer <-> company)
# ---------------------------------------------------------------------------
@bp.route("/messages", methods=["GET", "POST"])
def messages():
    cust = _customer()
    if request.method == "POST":
        body = (request.form.get("body") or "").strip()
        if body:
            db.session.add(Message(
                customer_id=cust.id, sender_type="customer",
                sender_user_id=current_user.id, sender_name=cust.name,
                body=body, read_by_customer=True, read_by_staff=False))
            log("message", "customer", cust.id, detail="customer message sent")
            db.session.commit()
        return redirect(url_for("portal.messages"))
    msgs = db.session.scalars(
        db.select(Message).filter_by(customer_id=cust.id).order_by(Message.created_at.desc())).all()
    changed = False
    for m in msgs:
        if m.sender_type == "staff" and not m.read_by_customer:
            m.read_by_customer = True
            changed = True
    if changed:
        db.session.commit()
    return render_template("portal/messages.html", msgs=msgs, cust=cust)


@bp.route("/promo-image/<int:promo_id>")
def promo_image(promo_id):
    from models import Announcement
    a = db.session.get(Announcement, promo_id)
    seg = _customer().segment or "customer"
    if a is None or not a.image_filename or not (a.is_live() and a.matches(seg)):
        abort(404)
    path = os.path.join(current_app.config["UPLOAD_DIR"], "promos", a.image_filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path)


@bp.route("/pricelist")
@bp.route("/pricelist/<int:list_id>")
def pricelist(list_id=None):
    lists = _my_lists()
    if not lists:
        flash("No pricelist has been assigned to you yet. Please contact us.", "warning")
        return redirect(url_for("portal.home"))
    pl = next((p for p in lists if p.id == list_id), lists[0])
    rows = []
    for line in pl.lines:
        if not line.product.is_active:
            continue
        priced = {t.key: effective_line_price(line, t.key) for t in pl.tiers}
        rows.append({"line": line, "product": line.product, "priced": priced})
    return render_template("portal/pricelist.html", pl=pl, lists=lists, rows=rows,
                           tiers=pl.tiers)


@bp.route("/order/new", methods=["GET", "POST"])
def order_new():
    lists = _my_lists()
    if not lists:
        flash("No pricelist has been assigned to you yet. Please contact us.", "warning")
        return redirect(url_for("portal.home"))
    _same = (len({(p.currency, bool(p.vat_applicable)) for p in lists}) == 1) if lists else False
    if request.method == "POST" or len(lists) == 1 or _same:
        if request.method == "POST":
            pl = next((p for p in lists if p.id == int(request.form.get("source_id", 0))), None)
        else:
            pl = lists[0]   # the order grid spans all compatible lists anyway
        if pl is None:
            abort(400)
        # Reuse an existing draft on the same pricelist instead of minting a
        # new SO number per attempt (keeps abandoned attempts from piling up).
        existing = db.session.scalars(
            db.select(SalesOrder).filter_by(
                customer_id=_active_id(), status="draft")
            .order_by(SalesOrder.created_at.desc())).first()
        if existing:
            return redirect(url_for("portal.order", order_id=existing.id))
        cust = _customer()
        # H2/H3/M9: derive VAT/market the same way staff orders and offers do.
        vat_applicable, vat_rate = order_vat.derive_vat(pl, cust)
        market = (cust.market if cust else None) or pl.market or "local"
        order = SalesOrder(
            customer_id=_active_id(),
            source_pricelist_id=pl.id, currency=pl.currency, market=market,
            vat_applicable=vat_applicable, vat_rate=vat_rate,
            status="draft", order_date=date.today(), created_by=current_user.id)
        db.session.add(order)
        order_vat.assign_number(order, "SO")   # C3: id-derived number + commit
        return redirect(url_for("portal.order", order_id=order.id))
    return render_template("portal/order_new.html", lists=lists)


@bp.route("/order/<int:order_id>")
def order(order_id):
    o = _get_my_order(order_id)
    # Opening the order counts as reading its messages (clears the unread
    # badge and re-arms the one-email-per-unread-batch notification throttle).
    changed = False
    for m in db.session.scalars(
            db.select(Message).filter_by(order_id=o.id, sender_type="staff")):
        if not m.read_by_customer:
            m.read_by_customer = True
            changed = True
    if changed:
        db.session.commit()
    rows = []
    if o.status == "draft":
        # 22 Jul 2026: one order draws from EVERY pricelist assigned to the
        # customer (same currency/VAT); the first list carrying a product wins.
        from services.allocation import combinable_lists
        qty_by_product = {l.product_id: l.quantity for l in o.lines}
        seen = set()
        lists = combinable_lists(o)
        for src in lists:
            tier = _primary_tier(src)
            for line in src.lines:
                p = line.product
                if not p.is_active or p.id in seen:
                    continue
                price = effective_line_price(line, tier.key) if tier else {"amount": None}
                if price["amount"] is None:
                    continue
                seen.add(p.id)
                rows.append({"line": line, "product": p,
                             "price": float(price["amount"]),
                             "qty": qty_by_product.get(p.id, ""),
                             "box": float(line.box_small) if (line.box_small and not src.allow_small_orders) else None,
                             "list_name": src.name if len(lists) > 1 else None})
    return render_template("portal/order.html", order=o, rows=rows)


def _apply_quantities(order, form):
    """Set order lines from the quantity grid (one input per pricelist line).
    22 Jul 2026: the grid spans every pricelist assigned to the customer, so
    inputs are applied across all of them; each line prices from its own
    list's primary tier."""
    from services.allocation import combinable_lists
    existing = {l.product_id: l for l in order.lines}
    violations = []
    for src in combinable_lists(order):
        tier = _primary_tier(src)
        # 22 Jul 2026: box sizes are minimum order quantities. Quantities must
        # be multiples of the Small box unless the list allows small orders
        # (Meat Supermarkets, Sokoni).
        enforce_box = not src.allow_small_orders
        for pline in src.lines:
            raw = form.get(f"qty_{pline.id}")
            if raw is None:
                continue
            try:
                q = float(raw)
            except ValueError:
                q = 0
            ol = existing.get(pline.product_id)
            box = float(pline.box_small or 0)
            if q > 0 and enforce_box and box > 0:
                mult = q / box
                if q + 1e-9 < box or abs(mult - round(mult)) > 1e-6:
                    violations.append(
                        f"{pline.product.article_no}: {q:g} (boxes of {box:g})")
                    continue
            if q > 0:
                eff = effective_line_price(pline, tier.key) if tier else {"amount": None}
                if eff["amount"] is None:
                    continue
                price = Decimal(str(eff["amount"]))
                if ol:
                    ol.quantity = q
                    ol.unit_price = price
                else:
                    order.lines.append(SalesOrderLine(
                        product_id=pline.product_id, description=pline.product.description,
                        article_no=pline.product.article_no,
                        pack_size=pline.pack_size or pline.product.pack_size,
                        tier_label=tier.label if tier else "", quantity=q,
                        unit_price=price,
                        vat_applicable=bool(pline.product.vat_applicable) if pline.product else None,
                        sort_order=pline.sort_order))
            elif ol:
                db.session.delete(ol)
    return violations


@bp.route("/order/<int:order_id>/save", methods=["POST"])
def save(order_id):
    o = _get_my_order(order_id)
    if o.status != "draft":
        abort(400)
    viol = _apply_quantities(o, request.form)
    if viol:
        db.session.rollback()
        flash("These quantities must be full boxes — " + "; ".join(viol[:6])
              + (" …" if len(viol) > 6 else "")
              + ". Order in multiples of the box size.", "danger")
        return redirect(url_for("portal.order", order_id=o.id))
    db.session.flush()

    if request.form.get("action") == "submit":
        if not any((l.quantity or 0) > 0 for l in o.lines):
            db.session.rollback()
            flash("Enter a quantity for at least one item before submitting.", "warning")
            return redirect(url_for("portal.order", order_id=o.id))
        o.delivery_date = _parse_date(request.form.get("delivery_date"))
        o.delivery_address = request.form.get("delivery_address") or o.customer.notes
        o.customer_po = request.form.get("customer_po")
        o.notes = request.form.get("notes")
        file = request.files.get("lpo")
        if file and file.filename:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext not in _LPO_EXT:
                db.session.rollback()
                flash("LPO must be a PDF, image, Word or Excel file.", "danger")
                return redirect(url_for("portal.order", order_id=o.id))
            folder = os.path.join(current_app.config["UPLOAD_DIR"], "lpo")
            os.makedirs(folder, exist_ok=True)
            safe = f"{o.number}_{secure_filename(file.filename)}"
            file.save(os.path.join(folder, safe))
            o.lpo_filename = safe
        o.status = "submitted"
        o.submitted_at = datetime.utcnow()
        log("order_submit", "sales_order", o.id, detail=f"{o.number} submitted by customer")
        # Finance gate: a blocked/uncleared account raises a credit alert to
        # CFO/finance manager the moment the order lands. The customer is not
        # told here; finance decides first.
        from services import credit
        credit.raise_alert(o)
        db.session.commit()
        flash(f"Order {o.number} submitted. We will confirm it shortly.", "success")
        return redirect(url_for("portal.order", order_id=o.id))

    db.session.commit()
    flash("Order updated.", "success")
    return redirect(url_for("portal.order", order_id=o.id))


@bp.route("/order/<int:order_id>/cancel", methods=["POST"])
def cancel(order_id):
    o = _get_my_order(order_id)
    if o.status not in ("draft", "submitted"):
        flash("This order can no longer be cancelled from here. Please contact us.", "warning")
        return redirect(url_for("portal.order", order_id=o.id))
    o.status = "cancelled"
    log("order_cancel", "sales_order", o.id, detail=f"{o.number} cancelled by customer")
    db.session.commit()
    flash(f"Order {o.number} cancelled.", "success")
    return redirect(url_for("portal.home"))


@bp.route("/order/<int:order_id>/backorder/<decision>", methods=["POST"])
def backorder_decision(order_id, decision):
    o = _get_my_order(order_id)
    if o.bo_confirm_state != "proposed":
        abort(400)
    if decision == "confirm":
        o.bo_confirm_state = "confirmed"
        o.status = "placed"
        log("order_backorder", "sales_order", o.id, detail=f"{o.number} back order confirmed by customer")
        flash(f"Thank you. Back order {o.number} confirmed — we will deliver it when back in stock.", "success")
    elif decision == "decline":
        o.bo_confirm_state = "declined"
        o.status = "cancelled"
        log("order_backorder", "sales_order", o.id, detail=f"{o.number} back order declined by customer")
        flash(f"Back order {o.number} declined.", "success")
    else:
        abort(400)
    db.session.commit()
    return redirect(url_for("portal.order", order_id=o.id))


@bp.route("/order/<int:order_id>/rate", methods=["POST"])
def rate_order(order_id):
    """Customer rates a delivered order (1-5 stars) and leaves a comment."""
    o = _get_my_order(order_id)
    if o.status not in ("delivered", "fulfilled"):
        flash("You can rate an order once it is delivered.", "warning")
        return redirect(url_for("portal.order", order_id=o.id))
    try:
        stars = int(request.form.get("rating") or 0)
    except ValueError:
        stars = 0
    if stars < 1 or stars > 5:
        flash("Please choose a rating from 1 to 5 stars.", "warning")
        return redirect(url_for("portal.order", order_id=o.id))
    o.rating = stars
    o.rating_comment = (request.form.get("comment") or "").strip() or None
    o.rated_at = datetime.utcnow()
    # Drop a note into the staff thread so the team sees the feedback.
    stars_txt = "★" * stars + "☆" * (5 - stars)
    body = f"Delivery feedback on {o.number}: {stars_txt} ({stars}/5)."
    if o.rating_comment:
        body += f"\n“{o.rating_comment}”"
    db.session.add(Message(
        customer_id=o.customer_id, sender_type="customer",
        sender_name=getattr(current_user, "full_name", "Customer"),
        body=body, read_by_staff=False, read_by_customer=True))
    log("order_rated", "sales_order", o.id, detail=f"{o.number} rated {stars}/5")
    db.session.commit()
    flash("Thank you for your feedback!", "success")
    return redirect(url_for("portal.order", order_id=o.id))


@bp.route("/order/<int:order_id>/pdf")
def order_pdf(order_id):
    o = _get_my_order(order_id)
    data = exports.order_to_pdf(o)
    disp = "inline" if request.args.get("view") == "1" else "attachment"
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f"{disp}; filename=Order_{o.number}.pdf"})


@bp.route("/order/<int:order_id>/lpo")
def lpo(order_id):
    o = _get_my_order(order_id)
    if not o.lpo_filename:
        abort(404)
    path = os.path.join(current_app.config["UPLOAD_DIR"], "lpo", o.lpo_filename)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True)


@bp.route("/account", methods=["GET", "POST"])
def account():
    from services.security import verify_password, hash_password
    if request.method == "POST":
        cur = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        if not verify_password(cur, current_user.password_hash):
            flash("Current password is incorrect.", "danger")
        elif len(new) < 8:
            flash("New password must be at least 8 characters.", "danger")
        elif new != (request.form.get("confirm_password") or ""):
            flash("Passwords do not match.", "danger")
        else:
            was_forced = bool(getattr(current_user, "must_change_password", False))
            current_user.password_hash = hash_password(new)
            current_user.must_change_password = False
            db.session.commit()
            log("password_change", "user", current_user.id, commit=True)
            flash("Password updated.", "success")
            if was_forced:
                return redirect(url_for("portal.home"))
        return redirect(url_for("portal.account"))
    return render_template("portal/account.html")


@bp.route("/guide")
def guide():
    """Short operations manual for the portal (also sent in the welcome email)."""
    return render_template("portal/guide.html")


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
