"""Sales orders placed by reps for their customers.

Reps build an order by choosing a customer (one assigned to them) and a pricelist
(a generic list or the customer's own), add products and quantities, then place
the order (which locks prices and stamps the exchange rate). Orders move
draft -> placed -> fulfilled, and can be cancelled.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify, abort, Response)
from flask_login import login_required, current_user

from extensions import db
from models import (SalesOrder, SalesOrderLine, Pricelist, PricelistLine,
                    Product, Customer, PricelistTier)
from services.security import (assert_can_see_customer, can_see_customer,
                               can_see_customer_pricelist)
from services.audit import log
from services import currency as cx
from services.pricing import effective_line_price
from services import settings as settings_svc
from services import exports
from services import order_vat

bp = Blueprint("orders", __name__, url_prefix="/orders")


FULFIL_STATUSES = ("submitted", "placed", "in_fulfillment", "pending",
                   "ready_for_dispatch", "out_for_delivery", "dispatched")


def _visible_orders():
    orders = db.session.scalars(
        db.select(SalesOrder).order_by(SalesOrder.created_at.desc())).all()
    if current_user.sees_all_orders:   # order managers, managers, admins
        return orders
    assigned = {c.id for c in current_user.assigned_customers}
    return [o for o in orders if o.customer_id in assigned]



def _can_amend():
    """Who may change an order's content or lifecycle before fulfilment:
    creators (reps and anyone with create_offers_orders) and acceptors
    (order managers, managers, admins). Fulfilment and dispatch view
    pre-acceptance orders read-only (21 Jul 2026)."""
    from services.permissions import has_perm
    return has_perm(current_user, "create_offers_orders") or current_user.can_accept_orders

def _get_order(order_id):
    o = db.session.get(SalesOrder, order_id)
    if o is None:
        abort(404)
    # Order managers (and managers/admins) see every order; reps only their own.
    if not current_user.sees_all_orders:
        assert_can_see_customer(current_user, o.customer)
    return o


@bp.route("/")
@login_required
def index():
    status = request.args.get("status", "")
    visible = _visible_orders()
    works_inbox = current_user.can_fulfill or current_user.can_accept_orders
    # Order managers / fulfilment land on the inbox (active orders) by default.
    if not status and works_inbox and request.args.get("all") != "1":
        inbox = [o for o in visible if o.status in FULFIL_STATUSES]
        orders = inbox
        inbox_view = True
    else:
        orders = [o for o in visible if o.status == status] if status else visible
        inbox_view = False
    counts = {}
    for o in visible:
        counts[o.status] = counts.get(o.status, 0) + 1
    counts["inbox"] = sum(counts.get(s, 0) for s in FULFIL_STATUSES)
    return render_template("orders/index.html", orders=orders, today=date.today(),
                           status=status, counts=counts, inbox_view=inbox_view,
                           can_fulfill=current_user.can_fulfill,
                           works_inbox=works_inbox)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    from services.permissions import has_perm
    if not has_perm(current_user, "create_offers_orders"):
        abort(403)
    from services.allocation import selectable_customers, build_allocation, is_allowed
    customers = selectable_customers(current_user)
    alloc_map, lists = build_allocation(current_user, customers)

    if request.method == "POST":
        customer_id = request.form.get("customer_id", type=int)
        source_id = request.form.get("source_id", type=int)
        if customer_id is None:
            flash("Choose a customer.", "danger")
            return render_template("orders/new.html", customers=customers,
                                   lists=lists, alloc_map=alloc_map), 400
        customer = db.session.get(Customer, customer_id)
        if customer is None:
            abort(404)
        assert_can_see_customer(current_user, customer)
        auto_src = False
        if source_id:
            src = db.session.get(Pricelist, source_id)
            if src is None:
                abort(404)
            if not is_allowed(current_user, customer, src):
                flash("That pricelist is not allocated to this customer.", "danger")
                return render_template("orders/new.html", customers=customers,
                                       lists=lists, alloc_map=alloc_map)
        else:
            # 22 Jul 2026: no pricelist choice needed — the order draws from
            # ALL the customer's assigned lists. A choice is only forced when
            # the lists split by currency or VAT treatment (e.g. a local UGX
            # list next to a USD export list).
            from services.allocation import allowed_pricelists_for
            cand = [p for p in allowed_pricelists_for(customer)
                    if can_see_customer_pricelist(current_user, p)]
            if not cand:
                flash("No pricelist is allocated to this customer yet.", "danger")
                return render_template("orders/new.html", customers=customers,
                                       lists=lists, alloc_map=alloc_map)
            groups = {}
            for p in cand:
                groups.setdefault((p.currency,) + tuple(order_vat.derive_vat(p, customer)),
                                  []).append(p)
            if len(groups) > 1:
                flash(f"{customer.name} carries pricelists with different "
                      f"currencies or VAT treatments (e.g. local and USD "
                      f"export). Pick which one this order uses.", "warning")
                return render_template("orders/new.html", customers=customers,
                                       lists=lists, alloc_map=alloc_map)
            grp = next(iter(groups.values()))
            # the customer's own list leads; the grid spans the rest anyway
            src = next((p for p in grp if p.is_customer), grp[0])
            auto_src = True
        # H4: never let an order be opened for a blocked account.
        if customer.account_status == "blocked" and not current_user.is_admin:
            flash(f"{customer.name}'s account is BLOCKED "
                  f"({customer.account_note or 'no reason given'}). "
                  f"An admin must clear it before an order can be placed.", "danger")
            return render_template("orders/new.html", customers=customers,
                                   lists=lists, alloc_map=alloc_map)
        ccy = src.currency if auto_src else request.form.get("currency", "UGX")
        # VAT and market are derived server-side (H2/H3/M9): market comes from the
        # customer/pricelist, never from the form; VAT follows the pricelist flag.
        vat_applicable, vat_rate = order_vat.derive_vat(src, customer)
        market = customer.market or src.market or "local"

        rate_value, rate_id = None, None
        if ccy == "USD" or src.currency == "USD":
            rate = cx.get_rate("USD")
            if rate is None:
                flash("No valid UGX→USD exchange rate. Ask the pricing person to set one.", "danger")
                return render_template("orders/new.html", customers=customers,
                                       lists=lists, alloc_map=alloc_map)
            rate_value, rate_id = rate.rate, rate.id

        order = SalesOrder(
            customer_id=customer.id, source_pricelist_id=src.id,
            currency=ccy, market=market, vat_applicable=vat_applicable,
            vat_rate=vat_rate,
            exchange_rate_value=rate_value, exchange_rate_id=rate_id,
            order_date=date.today(),
            delivery_date=_parse_date(request.form.get("delivery_date")),
            delivery_address=request.form.get("delivery_address"),
            customer_po=request.form.get("customer_po"),
            payment_terms=customer.payment_terms,
            notes=request.form.get("notes"),
            created_by=current_user.id)
        db.session.add(order)
        order_vat.assign_number(order, "SO")   # C3: safe id-derived number + commit
        log("order_create", "sales_order", order.id,
            detail=f"{order.number} for {customer.name}", commit=True)
        return redirect(url_for("orders.detail", order_id=order.id))

    return render_template("orders/new.html", customers=customers, lists=lists,
                           alloc_map=alloc_map)


@bp.route("/<int:order_id>")
@login_required
def detail(order_id):
    order = _get_order(order_id)
    # 22 Jul 2026: a draft opens straight into the fill-in grid — the same
    # view the customer gets, all items listed with the search bar on top.
    # ?classic=1 reaches the classic detail page (line editor, place button).
    if (order.status == "draft" and _can_amend() and order.source_pricelist
            and request.args.get("classic") != "1"):
        return redirect(url_for("orders.fill", order_id=order.id))
    drivers = []
    if current_user.can_dispatch:
        from models import User
        drivers = db.session.scalars(
            db.select(User).filter_by(role="delivery", is_active=True)
            .order_by(User.full_name)).all()
    # Credit-limit headroom for the accept window (acceptors, pre-acceptance).
    credit_info = None
    if current_user.can_accept_orders and order.status in ("submitted", "placed") \
            and ((order.customer.credit_limit_ugx or 0) > 0
                 or (order.customer.credit_days or 0) > 0):
        from services import credit as credit_svc
        lim = order.customer.credit_limit_ugx or 0
        dlim = order.customer.credit_days or 0
        out = credit_svc.outstanding_ugx(order.customer)
        tot = credit_svc.order_total_ugx(order)
        age = credit_svc.oldest_unpaid_days(order.customer)
        credit_info = {"limit": lim, "out": out, "total": tot,
                       "days": dlim, "age": age,
                       "over": lim > 0 and (out >= lim or out + tot > lim),
                       "overdue": dlim > 0 and age is not None and age > dlim,
                       "released": bool(order.credit_override_at)}
    return render_template("orders/detail.html", order=order, today=date.today(),
                           credit_info=credit_info,
                           can_fulfill=current_user.can_fulfill,
                           can_amend=_can_amend(),
                           is_acceptor=current_user.can_accept_orders, drivers=drivers,
                           can_decide_bo=_can_decide_backorder(current_user))


@bp.route("/<int:order_id>/search-products")
@login_required
def search_products(order_id):
    order = _get_order(order_id)
    q = (request.args.get("q") or "").strip().lower()
    from services.allocation import combinable_lists
    out, seen = [], set()
    lists = combinable_lists(order)
    for src in lists:
        for line in src.lines:
            p = line.product
            if p.id in seen:
                continue
            if q and q not in p.article_no.lower() and q not in (p.description or "").lower():
                continue
            seen.add(p.id)
            tiers = [{"key": t.key, "label": t.label,
                      "price": (float(line.price_for(t.key)) if line.price_for(t.key) is not None else None)}
                     for t in src.tiers]
            out.append({"line_id": line.id, "article_no": p.article_no,
                        "description": p.description
                        + (f"  [{src.name}]" if len(lists) > 1 else ""),
                        "pack_size": line.pack_size or p.pack_size or "",
                        "stock": (p.stock_on_hand or 0), "uom": p.unit_of_measure or "",
                        "currency": src.currency, "tiers": tiers})
            if len(out) >= 40:
                break
        if len(out) >= 40:
            break
    return jsonify(results=out)


@bp.route("/<int:order_id>/fill", methods=["GET", "POST"])
@login_required
def fill(order_id):
    """Staff quantity grid (22 Jul 2026): the whole pricelist with a quantity
    box per product and the search bar pinned on top — the same view the
    customer gets in the portal. Draft orders only; prices come from the
    list's primary selling tier, exactly as portal orders do."""
    order = _get_order(order_id)
    if not _can_amend():
        abort(403)
    if order.status != "draft":
        flash("The fill-in grid works on draft orders; after placing, use the "
              "line editor on the order page.", "warning")
        return redirect(url_for("orders.detail", order_id=order.id))
    src = order.source_pricelist
    if src is None:
        flash("This order has no source pricelist — add lines by search instead.", "warning")
        return redirect(url_for("orders.detail", order_id=order.id))
    from blueprints.portal import _primary_tier, _apply_quantities
    if request.method == "POST":
        viol = _apply_quantities(order, request.form)
        if viol:
            db.session.rollback()
            flash("These quantities must be full boxes — " + "; ".join(viol[:6])
                  + (" …" if len(viol) > 6 else "")
                  + ". Order in multiples of the box size.", "danger")
            return redirect(url_for("orders.fill", order_id=order.id))
        db.session.commit()
        log("order_fill_grid", "sales_order", order.id,
            detail=f"{order.number} quantities saved from the fill grid")
        flash("Quantities saved.", "success")
        if request.form.get("stay") == "1":
            return redirect(url_for("orders.fill", order_id=order.id))
        return redirect(url_for("orders.detail", order_id=order.id, classic=1))
    from services.allocation import combinable_lists
    qty_by_product = {l.product_id: l.quantity for l in order.lines}
    rows, seen = [], set()
    lists = combinable_lists(order)
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
    return render_template("orders/fill.html", order=order, rows=rows)


@bp.route("/<int:order_id>/line/add", methods=["POST"])
@login_required
def add_line(order_id):
    order = _get_order(order_id)
    if not _can_amend():
        abort(403)
    if not order.is_lines_amendable:
        flash("This order can no longer be amended.", "warning")
        return redirect(url_for("orders.detail", order_id=order.id))
    line_id = request.form.get("line_id", type=int)
    src_line = db.session.get(PricelistLine, line_id) if line_id else None
    if src_line is None or src_line.pricelist_id != order.source_pricelist_id:
        abort(400)
    tier_key = request.form.get("tier")
    try:
        qty = float(request.form.get("quantity", "1") or 1)
        discount = float(request.form.get("discount_pct", "0") or 0)
    except ValueError:
        flash("Quantity and discount must be numbers.", "danger")
        return redirect(url_for("orders.detail", order_id=order.id))

    # H10: reject a non-positive quantity, clamp discount into 0..100.
    if qty <= 0:
        flash("Quantity must be greater than zero.", "danger")
        return redirect(url_for("orders.detail", order_id=order.id))
    discount = min(100.0, max(0.0, discount))

    product = src_line.product
    tier = db.session.scalar(
        db.select(PricelistTier).filter_by(pricelist_id=order.source_pricelist_id, key=tier_key))
    eff = effective_line_price(src_line, tier_key)
    if eff["amount"] is None:
        flash("That tier has no price.", "danger")
        return redirect(url_for("orders.detail", order_id=order.id))

    def _to_order_ccy(amount, src_ccy):
        if src_ccy == order.currency:
            return Decimal(str(amount))
        # M6: a missing rate must not 500 — flash the same message the form uses.
        return cx.convert(amount, src_ccy, order.currency,
                          rate_value=order.exchange_rate_value)

    src_ccy = eff["currency"]
    max_sort = max([l.sort_order for l in order.lines], default=0)
    vat_snap = bool(product.vat_applicable) if product else None

    # M10: cap the promo quantity within this order. Only the units still under
    # the promo cap get the promo price; the excess is charged the normal price.
    from services import promos as promo_svc
    promo = promo_svc.active_promo_for(src_line, tier_key) if not eff["is_fixed"] else None
    try:
        if promo is not None:
            allowed = promo_svc.promo_qty_allowed(promo, qty)
            promo_up = _to_order_ccy(eff["amount"], src_ccy)
            if allowed >= qty or allowed <= 0:
                # whole line one price (all promo, or none left -> normal)
                if allowed <= 0:
                    # cap exhausted: charge the normal tier price, not the promo one
                    base = src_line.price_for(tier_key)
                    unit_price = (_to_order_ccy(base, src_ccy) if base is not None
                                  else promo_up)
                else:
                    unit_price = promo_up
                db.session.add(SalesOrderLine(
                    order_id=order.id, product_id=product.id, description=product.description,
                    article_no=product.article_no, pack_size=src_line.pack_size or product.pack_size,
                    tier_label=tier.label if tier else "", quantity=qty, unit_price=unit_price,
                    discount_pct=discount, is_fixed=eff["is_fixed"],
                    fixed_note=eff["note"] if eff["is_fixed"] else None,
                    vat_applicable=vat_snap, sort_order=max_sort + 1))
            else:
                # split: `allowed` at promo, remainder at normal price
                base = src_line.price_for(tier_key)
                normal_up = (_to_order_ccy(base, src_ccy) if base is not None else promo_up)
                excess = qty - allowed
                db.session.add(SalesOrderLine(
                    order_id=order.id, product_id=product.id, description=product.description,
                    article_no=product.article_no, pack_size=src_line.pack_size or product.pack_size,
                    tier_label=(tier.label if tier else "") + " (promo)", quantity=allowed,
                    unit_price=promo_up, discount_pct=discount, is_fixed=False,
                    fixed_note=eff["note"], vat_applicable=vat_snap, sort_order=max_sort + 1))
                db.session.add(SalesOrderLine(
                    order_id=order.id, product_id=product.id, description=product.description,
                    article_no=product.article_no, pack_size=src_line.pack_size or product.pack_size,
                    tier_label=tier.label if tier else "", quantity=excess,
                    unit_price=normal_up, discount_pct=discount, is_fixed=False,
                    fixed_note=None, vat_applicable=vat_snap, sort_order=max_sort + 2))
        else:
            unit_price = _to_order_ccy(eff["amount"], src_ccy)
            db.session.add(SalesOrderLine(
                order_id=order.id, product_id=product.id, description=product.description,
                article_no=product.article_no, pack_size=src_line.pack_size or product.pack_size,
                tier_label=tier.label if tier else "", quantity=qty, unit_price=unit_price,
                discount_pct=discount, is_fixed=eff["is_fixed"],
                fixed_note=eff["note"] if eff["is_fixed"] else None,
                vat_applicable=vat_snap, sort_order=max_sort + 1))
    except cx.NoValidRate:
        db.session.rollback()
        flash("No valid exchange rate for this currency. Ask the pricing person to set one.", "danger")
        return redirect(url_for("orders.detail", order_id=order.id))

    log("order_edit", "sales_order", order.id, detail=f"added {product.article_no} x{qty}")
    db.session.commit()
    flash("Line added.", "success")
    return redirect(url_for("orders.detail", order_id=order.id))


@bp.route("/<int:order_id>/line/<int:line_id>/remove", methods=["POST"])
@login_required
def remove_line(order_id, line_id):
    order = _get_order(order_id)
    if not _can_amend():
        abort(403)
    if not order.is_lines_amendable:
        flash("This order can no longer be amended.", "warning")
        return redirect(url_for("orders.detail", order_id=order.id))
    line = db.session.get(SalesOrderLine, line_id)
    if line is None or line.order_id != order.id:
        abort(404)
    db.session.delete(line)
    log("order_edit", "sales_order", order.id, detail=f"removed {line.article_no}")
    db.session.commit()
    return redirect(url_for("orders.detail", order_id=order.id))


@bp.route("/<int:order_id>/line/<int:line_id>/qty", methods=["POST"])
@login_required
def update_line_qty(order_id, line_id):
    order = _get_order(order_id)
    if not _can_amend():
        abort(403)
    if not order.is_lines_amendable:
        flash("This order can no longer be amended.", "warning")
        return redirect(url_for("orders.detail", order_id=order.id))
    line = db.session.get(SalesOrderLine, line_id)
    if line is None or line.order_id != order.id:
        abort(404)
    try:
        qty = float(request.form.get("quantity", ""))
    except ValueError:
        flash("Quantity must be a number.", "danger")
        return redirect(url_for("orders.detail", order_id=order.id))
    # 22 Jul 2026: box sizes are minimum order quantities (unless the list
    # allows small orders) — the classic editor obeys the same rule.
    if qty > 0 and line.product_id:
        from services.allocation import combinable_lists
        for _src in combinable_lists(order):
            _pl = next((x for x in _src.lines if x.product_id == line.product_id), None)
            if _pl is None:
                continue
            _box = float(_pl.box_small or 0)
            if not _src.allow_small_orders and _box > 0:
                _m = qty / _box
                if qty + 1e-9 < _box or abs(_m - round(_m)) > 1e-6:
                    flash(f"{line.article_no}: order in full boxes of {_box:g} "
                          f"(minimum order quantity).", "danger")
                    return redirect(url_for("orders.detail", order_id=order.id))
            break
    if qty <= 0:
        db.session.delete(line)
        log("order_edit", "sales_order", order.id, detail=f"removed {line.article_no} (qty 0)")
    else:
        old = line.quantity
        line.quantity = qty
        log("order_edit", "sales_order", order.id,
            detail=f"{line.article_no} qty {old} -> {qty}")
    db.session.commit()
    flash("Order updated.", "success")
    return redirect(url_for("orders.detail", order_id=order.id))


@bp.route("/<int:order_id>/details", methods=["POST"])
@login_required
def update_details(order_id):
    order = _get_order(order_id)
    if not _can_amend():
        abort(403)
    # M12: do not rewrite delivery date/address/PO once the order is past amendment.
    if not order.is_amendable:
        flash("This order can no longer be amended.", "warning")
        return redirect(url_for("orders.detail", order_id=order.id))
    order.delivery_date = _parse_date(request.form.get("delivery_date"))
    order.delivery_address = request.form.get("delivery_address")
    order.customer_po = request.form.get("customer_po")
    # Payment terms are set on the customer and inherited; never editable per order.
    order.payment_terms = order.customer.payment_terms if order.customer else order.payment_terms
    order.notes = request.form.get("notes")
    db.session.commit()
    flash("Order details saved.", "success")
    return redirect(url_for("orders.detail", order_id=order.id))


@bp.route("/<int:order_id>/place", methods=["POST"])
@login_required
def place(order_id):
    order = _get_order(order_id)
    if not _can_amend():
        abort(403)
    if order.status != "draft":
        abort(400)
    if not order.lines:
        flash("Add at least one line before placing the order.", "warning")
        return redirect(url_for("orders.detail", order_id=order.id))
    # H4: a blocked account cannot have an order placed for it.
    cust = order.customer
    if cust and cust.account_status == "blocked" and not current_user.is_admin:
        flash(f"{cust.name}'s account is BLOCKED "
              f"({cust.account_note or 'no reason given'}). "
              f"An admin must clear it before this order is placed.", "danger")
        return redirect(url_for("orders.detail", order_id=order.id))
    order.status = "placed"
    order.placed_at = datetime.utcnow()
    log("order_place", "sales_order", order.id,
        detail=f"{order.number} placed; rate {order.exchange_rate_value or 'n/a'}")
    # Finance gate: an account without finance credit clearance raises a
    # credit alert to CFO/finance manager (blocked accounts were refused above).
    from services import credit
    credit.raise_alert(order)
    db.session.commit()
    flash(f"Order {order.number} placed and locked.", "success")
    return redirect(url_for("orders.detail", order_id=order.id))


def _require_fulfiller():
    if not current_user.can_fulfill:
        abort(403)


def _require_acceptor():
    if not current_user.can_accept_orders:
        abort(403)


def _fulfilment_blocked(order):
    """H4: fulfilment may only run on an accepted, credit-checked order for a
    customer that is not blocked. Returns a flash message when blocked, else None."""
    cust = order.customer
    if cust and cust.account_status == "blocked" and not current_user.is_admin:
        return (f"{cust.name}'s account is BLOCKED "
                f"({cust.account_note or 'no reason given'}). "
                f"An admin must clear it before fulfilment can proceed.")
    if order.accepted_at is None or not order.credit_checked:
        return ("This order must be accepted and credit-checked before it can be "
                "fulfilled. Ask an order manager to accept it first.")
    return None


# orders.confirm removed 3 Jul 2026 (QA audit M2): superseded by stock_review,
# the accept flow, which handles both submitted and placed orders.

LPO_VIEWABLE = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif")


@bp.route("/<int:order_id>/lpo")
@login_required
def lpo(order_id):
    import os
    from flask import current_app, send_file
    order = _get_order(order_id)
    if not order.lpo_filename:
        abort(404)
    path = os.path.join(current_app.config["UPLOAD_DIR"], "lpo", order.lpo_filename)
    if not os.path.exists(path):
        abort(404)
    ext = os.path.splitext(order.lpo_filename)[1].lower()
    # ?view=1 opens viewable files (PDF/images) inline in the browser; others download.
    inline = request.args.get("view") == "1" and ext in LPO_VIEWABLE
    return send_file(path, as_attachment=not inline)


@bp.route("/<int:order_id>/lpo/upload", methods=["POST"])
@login_required
def upload_lpo(order_id):
    """Staff attach the customer's LPO to the order (photo or file)."""
    import os
    from flask import current_app
    from werkzeug.utils import secure_filename
    order = _get_order(order_id)
    if order.status == "cancelled":
        abort(400)
    file = request.files.get("lpo")
    if not file or not file.filename:
        flash("Choose a photo or file for the LPO.", "warning")
        return redirect(url_for("orders.detail", order_id=order.id))
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic",
                   ".doc", ".docx", ".xls", ".xlsx"):
        flash("The LPO must be a PDF, image, Word or Excel file.", "danger")
        return redirect(url_for("orders.detail", order_id=order.id))
    folder = os.path.join(current_app.config["UPLOAD_DIR"], "lpo")
    os.makedirs(folder, exist_ok=True)
    name = f"lpo_{order.id}_{datetime.utcnow():%Y%m%d%H%M%S}{ext}"
    file.save(os.path.join(folder, secure_filename(name)))
    order.lpo_filename = name
    log("order_lpo", "sales_order", order.id, detail=f"{order.number} LPO attached")
    db.session.commit()
    flash("LPO attached to the order.", "success")
    return redirect(url_for("orders.detail", order_id=order.id))


@bp.route("/<int:order_id>/delivery-note.pdf")
@login_required
def delivery_note(order_id):
    from io import BytesIO
    from flask import send_file
    order = _get_order(order_id)
    pdf = exports.delivery_note_to_pdf(order)
    inline = request.args.get("view") == "1"
    return send_file(BytesIO(pdf), mimetype="application/pdf",
                     as_attachment=not inline,
                     download_name=f"{order.dnote_number or order.number}.pdf")


@bp.route("/<int:order_id>/picking-slip")
@login_required
def picking_slip(order_id):
    """Warehouse picking slip: available from acceptance onward to acceptors
    and fulfilment. Empty Picked and Verified columns for pen-and-paper."""
    from io import BytesIO
    from flask import send_file
    if not (current_user.can_fulfill or current_user.can_accept_orders):
        abort(403)
    order = _get_order(order_id)
    if order.accepted_at is None and order.status not in (
            "in_fulfillment", "pending", "ready_for_dispatch",
            "out_for_delivery", "dispatched", "delivered", "fulfilled"):
        flash("The picking slip exists once the order is accepted.", "warning")
        return redirect(url_for("orders.detail", order_id=order.id))
    pdf = exports.picking_slip_to_pdf(order)
    inline = request.args.get("view") == "1"
    return send_file(BytesIO(pdf), mimetype="application/pdf",
                     as_attachment=not inline,
                     download_name=f"PICK_{order.number}.pdf")


@bp.route("/<int:order_id>/invoice")
@login_required
def invoice_print(order_id):
    """The fiscal invoice behind a completed order, printable by acceptors and
    fulfilment without opening the accounting module."""
    if not (current_user.can_fulfill or current_user.can_accept_orders):
        abort(403)
    order = _get_order(order_id)
    from services.sale_posting import invoice_for_order
    inv = invoice_for_order(order)
    if inv is None:
        flash("No invoice exists for this order (internal transfer, or not "
              "yet completed).", "warning")
        return redirect(url_for("orders.detail", order_id=order.id))
    return render_template("accounting/invoice_view.html", invoice=inv,
                           can_credit=False)


@bp.route("/<int:order_id>/pod")
@login_required
def pod(order_id):
    import os
    from flask import current_app, send_from_directory
    order = _get_order(order_id)
    if not order.pod_filename:
        abort(404)
    folder = os.path.join(current_app.config["UPLOAD_DIR"], "pod")
    return send_from_directory(folder, order.pod_filename)


# orders.start_fulfillment removed 3 Jul 2026 (QA audit M2): superseded by
# stock_review, which moves orders to in_fulfillment and seeds fulfilled_qty.


@bp.route("/<int:order_id>/stock-review", methods=["POST"])
@login_required
def stock_review(order_id):
    """Order manager ACCEPTS the order: confirms the account is not blocked and
    credit was checked, marks each line in/out of stock, optionally notifies the
    customer, decides the back-order route for out-of-stock items, then moves the
    in-stock items into fulfilment."""
    from models import Message
    _require_acceptor()
    order = _get_order(order_id)
    if order.status not in ("submitted", "placed", "in_fulfillment", "pending"):
        abort(400)

    cust = order.customer
    # Finance gate (20 Jul 2026): the order manager no longer self-declares
    # the credit check. Acceptance requires the finance credit clearance tick
    # on the customer profile and an unblocked account. A gated order raises
    # a credit alert to CFO/finance manager (once) and stays waiting.
    from services import credit
    ok, reason = credit.gate(cust, order=order)
    # Over-limit refuses for EVERYONE — only a CEO/CFO/finance-manager
    # release from the Credit alerts queue lets the order through. The
    # admin bypass applies to manual blocks/clearance only.
    if not ok and (reason == "over_limit" or not current_user.is_admin):
        credit.raise_alert(order)
        db.session.commit()
        if reason == "over_limit":
            why = (f"insufficient credit — outstanding UGX "
                   f"{credit.outstanding_ugx(cust):,}, limit UGX "
                   f"{(cust.credit_limit_ugx or 0):,}, this order UGX "
                   f"{credit.order_total_ugx(order):,}")
        elif reason == "blocked":
            why = "account BLOCKED"
        else:
            why = "no finance credit clearance on the customer profile"
        flash(f"Cannot accept {order.number}: {why} "
              f"({cust.account_note or 'no note'}). Finance has been alerted "
              f"and decides in the Credit alerts queue.", "danger")
        return redirect(url_for("orders.detail", order_id=order.id))

    oos_lines = []
    for l in order.lines:
        out = request.form.get(f"oos_{l.id}") == "1"
        if out:
            l.availability = "out_of_stock"
            l.expected_restock = _parse_date(request.form.get(f"exp_{l.id}"))
            l.fulfilled_qty = 0
            oos_lines.append(l)
        else:
            if l.availability == "out_of_stock":
                l.availability = "available"
                l.expected_restock = None
            if l.fulfilled_qty is None:
                l.fulfilled_qty = l.quantity

    notify = request.form.get("notify_customer") == "1"
    note = (request.form.get("notify_message") or "").strip()
    if notify and oos_lines:
        items = []
        for l in oos_lines:
            when = (f", expected back {l.expected_restock:%d %b %Y}"
                    if l.expected_restock else "")
            items.append(f"• {l.article_no} {l.description}{when}")
            l.customer_notified_at = datetime.utcnow()
        body = (f"Update on your order {order.number}: the following item(s) are "
                f"currently out of stock:\n" + "\n".join(items))
        if note:
            body += f"\n\n{note}"
        body += ("\n\nThe items we have in stock are being prepared for delivery now. "
                 "We will follow up on the balance.")
        m = Message(
            customer_id=order.customer_id, sender_type="staff",
            sender_user_id=current_user.id,
            sender_name=getattr(current_user, "full_name", "Sales team"),
            body=body, order_id=order.id,
            read_by_customer=False, read_by_staff=True)
        db.session.add(m)

    # accept -> fulfilment
    order.status = "in_fulfillment"
    order.accepted_at = datetime.utcnow()
    order.accepted_by_id = current_user.id
    order.credit_checked = True
    if order.fulfilment_started_at is None:
        order.fulfilment_started_at = datetime.utcnow()

    # back-order route for out-of-stock items
    bo = None
    bo_action = request.form.get("oos_action", "none")
    if not (cust and cust.backorders_allowed):
        bo_action = "none"   # profile says no back orders, ever
    if oos_lines and bo_action in ("create_now", "ask_customer"):
        state = "confirmed" if bo_action == "create_now" else "proposed"
        bo = _build_backorder(order, confirm_state=state)
        if bo is not None and bo_action == "ask_customer":
            m = Message(
                customer_id=order.customer_id, sender_type="staff",
                sender_user_id=current_user.id,
                sender_name=getattr(current_user, "full_name", "Sales team"),
                body=(f"We have raised a proposed back order {bo.number} for the "
                      f"out-of-stock items on {order.number}. Please confirm in your "
                      f"portal whether you want us to deliver these when back in stock, "
                      f"or decline."))
            db.session.add(m)

    log("order_accept", "sales_order", order.id,
        detail=(f"{order.number} accepted by {current_user.full_name}; "
                f"{len(oos_lines)} out of stock; back order: {bo_action}"))
    db.session.commit()

    msg = "Order accepted and in fulfilment."
    if oos_lines:
        msg = (f"Order accepted. {len(oos_lines)} item(s) out of stock"
               + (", customer notified" if notify else "") + ".")
        if bo is not None:
            msg += (f" Back order {bo.number} "
                    + ("created." if bo_action == "create_now" else "proposed to the customer."))
    flash(msg, "success")
    # 21 Jul 2026: acceptance hands the store a paper picking slip.
    return redirect(url_for("orders.detail", order_id=order.id, print="pickslip"))


@bp.route("/<int:order_id>/hold", methods=["POST"])
@login_required
def hold(order_id):
    _require_fulfiller()
    order = _get_order(order_id)
    if order.status not in ("in_fulfillment", "placed"):
        abort(400)
    order.status = "pending"
    log("order_fulfilment", "sales_order", order.id,
        detail=f"{order.number} put on hold (pending stock)")
    db.session.commit()
    flash(f"Order {order.number} marked pending — waiting on stock.", "success")
    return redirect(url_for("orders.detail", order_id=order.id))


@bp.route("/<int:order_id>/resume", methods=["POST"])
@login_required
def resume(order_id):
    _require_fulfiller()
    order = _get_order(order_id)
    if order.status != "pending":
        abort(400)
    order.status = "in_fulfillment"
    log("order_fulfilment", "sales_order", order.id, detail=f"{order.number} resumed")
    db.session.commit()
    flash(f"Order {order.number} resumed.", "success")
    return redirect(url_for("orders.detail", order_id=order.id))


@bp.route("/<int:order_id>/line/<int:line_id>/availability", methods=["POST"])
@login_required
def set_availability(order_id, line_id):
    _require_fulfiller()
    order = _get_order(order_id)
    if order.status not in ("in_fulfillment", "pending"):
        abort(400)   # 21 Jul 2026: no availability marking before acceptance
    line = db.session.get(SalesOrderLine, line_id)
    if line is None or line.order_id != order.id:
        abort(404)
    value = request.form.get("availability", "available")
    if value not in ("available", "out_of_stock"):
        value = "available"
    old = line.availability
    line.availability = value
    log("order_line_stock", "sales_order", order.id, field=line.article_no,
        old_value=old, new_value=value, detail=f"{order.number} stock mark")
    db.session.commit()
    flash(f"{line.article_no} marked {'out of stock' if value=='out_of_stock' else 'available'}.", "success")
    return redirect(url_for("orders.detail", order_id=order.id))


@bp.route("/<int:order_id>/fulfilled-qty", methods=["POST"])
@login_required
def save_fulfilled(order_id):
    _require_fulfiller()
    order = _get_order(order_id)
    if order.status not in ("placed", "in_fulfillment", "pending"):
        abort(400)
    changed = 0
    for l in order.lines:
        raw = request.form.get(f"qty_{l.id}")
        if raw is None or raw == "":
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        if val < 0:
            val = 0
        # M7: never record more delivered than was ordered (over-delivery would
        # over-deduct stock and inflate the line total).
        val = min(val, l.quantity or 0)
        if l.fulfilled_qty != val:
            old = l.fulfilled_qty if l.fulfilled_qty is not None else l.quantity
            l.fulfilled_qty = val
            log("order_qty", "sales_order", order.id, field=l.article_no,
                old_value=old, new_value=val, detail=f"{order.number} delivered qty")
            changed += 1
    # Stock guard at entry (21 Jul 2026): the Fulfilled quantity is capped by
    # the stock on hand, in the product's own unit (kg items are stocked in
    # kg, piece items in pieces — the stock unit IS the selling unit). The
    # save is refused with the offending lines named, so the officer corrects
    # immediately instead of failing later at Complete.
    from services.features import feature_on
    if feature_on("stock_guard"):
        need = {}
        for l in order.lines:
            if l.product_id and (l.fulfilled_qty or 0) > 0 and l.availability != "out_of_stock":
                need.setdefault(l.product, 0)
                need[l.product] += l.fulfilled_qty or 0
        short = [(p, q) for p, q in need.items()
                 if q > (p.stock_on_hand or 0) + 1e-9]
        if short:
            items = "; ".join(
                f"{p.article_no} {p.description}: entered {q:g}, on hand "
                f"{(p.stock_on_hand or 0):g} {p.unit_of_measure or ''}".rstrip()
                for p, q in short)
            db.session.rollback()
            flash(f"Not saved — more than the stock on hand: {items}. "
                  f"Enter at most what the store holds, or mark the line out "
                  f"of stock; only the store manager can add stock.", "danger")
            return redirect(url_for("orders.detail", order_id=order.id))
    db.session.commit()
    flash(f"Saved delivered quantities ({changed} change(s)).", "success")
    return redirect(url_for("orders.detail", order_id=order.id))


@bp.route("/<int:order_id>/assign", methods=["POST"])
@login_required
def assign_driver(order_id):
    """Dispatch officer / order manager assigns a driver to a ready order."""
    order = _get_order(order_id)
    if not current_user.can_dispatch:
        abort(403)
    if order.status not in ("ready_for_dispatch", "out_for_delivery"):
        abort(400)
    from models import User
    driver = db.session.get(User, int(request.form.get("driver_id") or 0))
    if driver is None or driver.role != "delivery":
        flash("Choose a driver.", "warning")
        return redirect(url_for("orders.detail", order_id=order.id))
    order.assigned_driver_id = driver.id
    order.assigned_at = datetime.utcnow()
    order.driver_accepted_at = None
    log("order_assign", "sales_order", order.id,
        detail=f"{order.number} assigned to {driver.full_name}")
    db.session.commit()
    flash(f"Order {order.number} assigned to {driver.full_name}.", "success")
    return redirect(url_for("orders.detail", order_id=order.id))


@bp.route("/<int:order_id>/complete", methods=["POST"])
@login_required
def complete(order_id):
    """Fulfilment officer finishes picking: records delivered quantities, creates
    the delivery note and moves the order to ready-for-dispatch."""
    _require_fulfiller()
    order = _get_order(order_id)
    if order.status not in ("in_fulfillment", "pending"):
        abort(400)
    blocked = _fulfilment_blocked(order)
    if blocked:
        flash(blocked, "danger")
        return redirect(url_for("orders.detail", order_id=order.id))
    not_delivered = 0
    for l in order.lines:
        if l.availability == "out_of_stock":
            l.availability = "not_delivered"
            l.fulfilled_qty = 0
            not_delivered += 1
        elif l.fulfilled_qty is None:
            l.fulfilled_qty = l.quantity
    # Stock guard (21 Jul 2026): fulfilment cannot ship more than the store
    # holds. Only the store manager changes stock quantities; the fulfilment
    # officer's options on a short line are reducing the To deliver quantity
    # (shortfall goes to back order) or marking the line out of stock. The
    # guard pauses from Admin > Features while opening balances are loaded.
    from services.features import feature_on
    if feature_on("stock_guard") and not order.stock_deducted:
        need = {}
        for l in order.lines:
            if l.product_id and (l.fulfilled_qty or 0) > 0:
                need.setdefault(l.product, 0)
                need[l.product] += l.fulfilled_qty or 0
        short = [(p, q, p.stock_on_hand or 0) for p, q in need.items()
                 if q > (p.stock_on_hand or 0) + 1e-9]
        if short:
            items = "; ".join(f"{p.article_no} {p.description}: picking {q:g}, "
                              f"on hand {soh:g}" for p, q, soh in short)
            db.session.rollback()
            flash(f"Cannot complete {order.number} — more than the stock on "
                  f"hand: {items}. Reduce the Fulfilled quantity or mark the "
                  f"line out of stock; only the store manager can add stock.",
                  "danger")
            return redirect(url_for("orders.detail", order_id=order.id))
    # Back-order policy comes from the customer profile (21 Jul 2026):
    # allowed means shortfalls raise a CONFIRMED back order automatically;
    # not allowed means none is ever created and the balance is dropped.
    bo_allowed = bool(order.customer and order.customer.backorders_allowed)

    # Safeguard: nothing was in stock to deliver. Don't let the order sit as an
    # active/dispatched order with an empty delivery — cancel it and (unless the
    # officer opts out) raise a proposed back order for the full quantity.
    any_delivered = any((l.fulfilled_qty or 0) > 0 for l in order.lines)
    if not any_delivered:
        order.status = "cancelled"
        order.fulfilled_at = datetime.utcnow()
        bo = _build_backorder(order, confirm_state="confirmed") if bo_allowed else None
        m = None
        if bo is not None:
            from models import Message
            m = Message(
                customer_id=order.customer_id, sender_type="staff",
                sender_user_id=current_user.id,
                sender_name=getattr(current_user, "full_name", "Sales team"),
                body=(f"Update on your order {order.number}: the items were out of "
                      f"stock, so nothing could be delivered. We raised back order "
                      f"{bo.number} for the full quantity and will deliver as soon "
                      f"as the items are back in stock."),
                order_id=bo.id, read_by_customer=False, read_by_staff=True)
            db.session.add(m)
        log("order_fulfill", "sales_order", order.id,
            detail=f"{order.number} nothing in stock -> cancelled"
                   + (f", back order {bo.number}" if bo else ""))
        db.session.commit()
        msg = f"Nothing was in stock for {order.number}, so it was cancelled."
        if bo is not None:
            msg += (f" Back order {bo.number} raised for the full quantity and the "
                    f"customer alerted.")
        flash(msg, "warning")
        return redirect(url_for("orders.detail", order_id=order.id))

    order.status = "ready_for_dispatch"
    order.fulfilled_at = datetime.utcnow()
    if not order.dnote_number:
        order.dnote_number = "DN-" + (order.number or str(order.id))
        order.dnote_at = datetime.utcnow()
    # Take the delivered quantities off stock (once per order). H7: flip the
    # stock_deducted flag atomically in SQL so two concurrent completes cannot
    # both deduct. Only the process that wins the UPDATE (rowcount == 1) deducts.
    from services import stock as stock_svc
    invoice = None
    res = db.session.execute(
        db.update(SalesOrder)
        .where(SalesOrder.id == order.id, SalesOrder.stock_deducted.is_(False))
        .values(stock_deducted=True))
    if res.rowcount == 1:
        order.stock_deducted = True   # keep the in-session object consistent
        stock_svc.deduct_order(order, user_id=current_user.id, already_flagged=True)
        # Accounting: what happens next depends on WHO ordered.
        #   * A real customer -> fiscal invoice + journal + COGS (Phase 3).
        #   * One of our OWN SHOPS (customer.internal_location_id set) -> a
        #     stock TRANSFER (Phase 7): no invoice, no revenue, no VAT — the
        #     goods only change location. The sale posts later from the
        #     shop's daily takings.
        if order.customer and order.customer.internal_location_id:
            from services import shop_ops
            try:
                transfer = shop_ops.transfer_for_order(order, user_id=current_user.id)
            except shop_ops.ShopError as e:
                db.session.rollback()
                flash(f"Fulfilment stopped — transfer could not be posted: {e}",
                      "danger")
                return redirect(url_for("orders.detail", order_id=order.id))
            invoice = None
            flash(f"Internal shop order: stock transfer {transfer.transfer_no} "
                  f"posted to {transfer.to_location.name} — no invoice, the "
                  "sale happens at the shop till.", "info")
        elif feature_on("invoice_after_delivery"):
            # Invoice-after-delivery (31 Jul 2026): nothing is invoiced at
            # fulfilment. The truck leaves on the delivery note alone; the
            # invoicing clerk bills the quantities the customer accepted on
            # the signed note, from the invoicing queue, after delivery.
            invoice = None
        else:
            # A posting failure stops the completion — an order must never
            # ship with no invoice behind it. Fiscalization to URA happens
            # AFTER commit and never blocks (see below).
            from services import sale_posting
            try:
                invoice = sale_posting.post_sale(order, user_id=current_user.id)
            except sale_posting.SalePostingError as e:
                db.session.rollback()
                flash(f"Fulfilment stopped — invoice could not be posted: {e}", "danger")
                return redirect(url_for("orders.detail", order_id=order.id))
    log("order_fulfill", "sales_order", order.id,
        detail=f"{order.number} fulfilled, {order.dnote_number} ({not_delivered} not delivered)"
               + (f", invoice {invoice.invoice_no}" if invoice else ""))

    # Automatically raise a back order for any undelivered balance — short
    # quantities or items found physically out of stock during fulfilment —
    # when the customer profile allows back orders. The customer consented on
    # the profile, so it is created CONFIRMED (no per-order ask). Customers
    # whose profile says no never get one; the balance is dropped.
    make_bo = bool(order.outstanding_items()) and bo_allowed
    bo = _build_backorder(order, confirm_state="confirmed") if make_bo else None
    bo_msg = None
    if bo is not None:
        from models import Message
        bo_msg = Message(
            customer_id=order.customer_id, sender_type="staff",
            sender_user_id=current_user.id,
            sender_name=getattr(current_user, "full_name", "Sales team"),
            body=(f"Update on your order {order.number}: some items could not be "
                  f"delivered in full, so we raised back order {bo.number} for the "
                  f"balance. We will deliver it as soon as the items are back in "
                  f"stock."),
            read_by_customer=False, read_by_staff=True)
        db.session.add(bo_msg)
    db.session.commit()

    # Fiscalize AFTER commit: the sale is safe in the books whatever URA says.
    inv_note = ""
    deferred = (invoice is None and feature_on("invoice_after_delivery")
                and not (order.customer and order.customer.internal_location_id))
    if invoice is not None:
        from services import efris
        if efris.try_fiscalize(invoice):
            inv_note = (f" Invoice {invoice.invoice_no} posted and fiscalized "
                        f"(FDN {invoice.efris_fdn}).")
        else:
            inv_note = (f" Invoice {invoice.invoice_no} posted; fiscalization "
                        f"pending — queued for retry.")
    elif deferred:
        inv_note = (" No invoice yet: the invoicing clerk bills the accepted "
                    "quantities from the signed delivery note after delivery.")
    print_mode = "dnote" if deferred else "pack"

    if bo is not None:
        flash(f"Fulfilment complete. Delivery note {order.dnote_number} ready. "
              f"Back order {bo.number} raised and the customer alerted — confirm it "
              f"on its page once the customer agrees (or they can confirm in the portal)."
              + inv_note, "success")
        return redirect(url_for("orders.detail", order_id=order.id, print=print_mode))
    flash(f"Fulfilment complete. Delivery note {order.dnote_number} ready for dispatch."
          + (f" {not_delivered} item(s) not delivered." if not_delivered else "")
          + inv_note, "success")
    # 21 Jul 2026: completion prints the delivery note (and the invoice when
    # the sale was invoiced at fulfilment).
    return redirect(url_for("orders.detail", order_id=order.id, print=print_mode))


def _can_decide_backorder(user):
    """Who may confirm/decline a proposed back order on the customer's behalf:
    order managers, telesales and reps (and managers/admins)."""
    return bool(getattr(user, "can_accept_orders", False)
                or getattr(user, "is_telesales", False)
                or getattr(user, "is_rep", False)
                or getattr(user, "can_manage_all", False))


@bp.route("/<int:order_id>/backorder/<decision>", methods=["POST"])
@login_required
def staff_backorder_decision(order_id, decision):
    """Staff confirm/decline a proposed back order (e.g. the customer agreed
    verbally to the rep). Mirrors the customer's portal decision."""
    order = _get_order(order_id)
    if not _can_decide_backorder(current_user):
        abort(403)
    if order.bo_confirm_state != "proposed":
        abort(400)
    who = getattr(current_user, "full_name", "staff")
    if decision == "confirm":
        order.bo_confirm_state = "confirmed"
        order.status = "placed"
        log("order_backorder", "sales_order", order.id,
            detail=f"{order.number} back order confirmed by {who}")
        db.session.commit()
        flash(f"Back order {order.number} confirmed. It is now in the order queue.", "success")
    elif decision == "decline":
        order.bo_confirm_state = "declined"
        order.status = "cancelled"
        log("order_backorder", "sales_order", order.id,
            detail=f"{order.number} back order declined by {who}")
        db.session.commit()
        flash(f"Back order {order.number} declined and closed.", "success")
    else:
        abort(400)
    return redirect(url_for("orders.detail", order_id=order.id))


@bp.route("/<int:order_id>/feedback-ack", methods=["POST"])
@login_required
def feedback_ack(order_id):
    """Mark a customer's delivery feedback as reviewed so it drops off the desk
    (it stays in the feedback report)."""
    order = _get_order(order_id)
    if not (current_user.can_accept_orders or current_user.can_manage_all):
        abort(403)
    if order.rating:
        order.feedback_ack = True
        order.feedback_ack_at = datetime.utcnow()
        log("feedback_ack", "sales_order", order.id,
            detail=f"{order.number} feedback reviewed by {current_user.full_name}")
        db.session.commit()
        flash(f"Feedback on {order.number} marked reviewed.", "success")
    return redirect(request.form.get("next") or url_for("dashboard.home"))


@bp.route("/<int:order_id>/cancel", methods=["POST"])
@login_required
def cancel(order_id):
    order = _get_order(order_id)
    if not _can_amend():
        abort(403)
    # C2: a delivered order must not be cancellable (it would vanish from revenue
    # while its stock deduction stands).
    if order.status in ("fulfilled", "delivered", "cancelled"):
        abort(400)
    # Phase 3: an order with a posted fiscal invoice cannot be cancelled by a
    # status flip — the correction path is a fiscalized credit note, which
    # reverses the journal, restocks the valued inventory, and tells URA.
    from services.sale_posting import invoice_for_order
    _inv = invoice_for_order(order)
    if _inv and _inv.status != "credited":
        flash(f"Order {order.number} carries fiscal invoice {_inv.invoice_no}. "
              "Cancel is blocked: raise a credit note on the invoice instead "
              "(Accounting → Invoices).", "danger")
        return redirect(url_for("orders.detail", order_id=order.id))
    # Phase 7: an internal order whose stock already transferred to a shop
    # cannot be cancelled by a status flip — bring the goods back with a
    # return transfer (shop -> plant) first.
    from models import AccTransfer as _T
    _trf = db.session.scalar(db.select(_T).where(_T.order_id == order.id))
    if _trf:
        flash(f"Order {order.number} already transferred stock to "
              f"{_trf.to_location.name} ({_trf.transfer_no}). Post a return "
              "transfer (Accounting → Shops) before cancelling.", "danger")
        return redirect(url_for("orders.detail", order_id=order.id))
    # C2: if stock was already deducted for this order, add the delivered
    # quantities back before cancelling, and clear the flag.
    if order.stock_deducted:
        from services import stock as stock_svc
        for line in order.lines:
            if not line.product_id:
                continue
            qty = line.delivered_qty or 0
            if qty <= 0:
                continue
            stock_svc.apply_movement(
                line.product, +qty, "return", user_id=current_user.id,
                note=f"Cancel of order {order.number}", order_id=order.id)
        order.stock_deducted = False
    order.status = "cancelled"
    log("order_cancel", "sales_order", order.id, detail=f"{order.number} cancelled")
    db.session.commit()
    flash(f"Order {order.number} cancelled.", "success")
    return redirect(url_for("orders.detail", order_id=order.id))


def _build_backorder(order, confirm_state="confirmed"):
    """Create a back order for the outstanding balance. Returns the new order or
    None. confirm_state 'proposed' means the customer must confirm it before it
    is worked; 'confirmed' means it is live straight away."""
    if order.backorder is not None:
        return order.backorder
    outstanding = order.outstanding_items()
    if not outstanding:
        return None
    bo = SalesOrder(
        customer_id=order.customer_id,
        source_pricelist_id=order.source_pricelist_id, currency=order.currency,
        market=order.market, vat_applicable=order.vat_applicable, vat_rate=order.vat_rate,
        exchange_rate_value=order.exchange_rate_value, exchange_rate_id=order.exchange_rate_id,
        status=("placed" if confirm_state == "confirmed" else "submitted"),
        order_date=date.today(),
        delivery_address=order.delivery_address, customer_po=order.customer_po,
        payment_terms=order.payment_terms,
        notes=f"Back order of {order.number} (undelivered balance).",
        created_by=current_user.id, backorder_of_id=order.id,
        bo_confirm_state=confirm_state)
    db.session.add(bo)
    db.session.flush()
    bo.number = order_vat.derive_number("SO", bo.id)   # C3: id-derived number
    for src, qty in outstanding:
        db.session.add(SalesOrderLine(
            order_id=bo.id, product_id=src.product_id, description=src.description,
            article_no=src.article_no, pack_size=src.pack_size, tier_label=src.tier_label,
            quantity=qty, unit_price=src.unit_price, discount_pct=src.discount_pct,
            is_fixed=src.is_fixed, fixed_note=src.fixed_note,
            availability="available", sort_order=src.sort_order))
    log("order_backorder", "sales_order", order.id,
        detail=f"{bo.number} created as back order of {order.number}")
    return bo


@bp.route("/<int:order_id>/backorder", methods=["POST"])
@login_required
def create_backorder(order_id):
    _require_acceptor()
    order = _get_order(order_id)
    if not (order.customer and order.customer.backorders_allowed):
        flash(f"{order.customer.name} does not accept back orders "
              f"(set on the customer profile).", "warning")
        return redirect(url_for("orders.detail", order_id=order.id))
    if order.backorder is not None:
        flash(f"A back order already exists: {order.backorder.number}.", "warning")
        return redirect(url_for("orders.detail", order_id=order.backorder.id))
    outstanding = order.outstanding_items()
    if not outstanding:
        flash("Nothing outstanding — this order was delivered in full.", "warning")
        return redirect(url_for("orders.detail", order_id=order.id))

    bo = SalesOrder(
        customer_id=order.customer_id,
        source_pricelist_id=order.source_pricelist_id, currency=order.currency,
        market=order.market, vat_applicable=order.vat_applicable, vat_rate=order.vat_rate,
        exchange_rate_value=order.exchange_rate_value, exchange_rate_id=order.exchange_rate_id,
        status="placed", order_date=date.today(),
        delivery_address=order.delivery_address, customer_po=order.customer_po,
        payment_terms=order.payment_terms,
        notes=f"Back order of {order.number} (undelivered balance).",
        created_by=current_user.id, backorder_of_id=order.id)
    db.session.add(bo)
    db.session.flush()
    bo.number = order_vat.derive_number("SO", bo.id)   # C3: id-derived number
    for src, qty in outstanding:
        db.session.add(SalesOrderLine(
            order_id=bo.id, product_id=src.product_id, description=src.description,
            article_no=src.article_no, pack_size=src.pack_size, tier_label=src.tier_label,
            quantity=qty, unit_price=src.unit_price, discount_pct=src.discount_pct,
            is_fixed=src.is_fixed, fixed_note=src.fixed_note,
            availability="available", sort_order=src.sort_order))
    log("order_backorder", "sales_order", order.id,
        detail=f"{bo.number} created as back order of {order.number}")
    db.session.commit()
    flash(f"Back order {bo.number} created for the undelivered items. It is ready to fulfil.", "success")
    return redirect(url_for("orders.detail", order_id=bo.id))


@bp.route("/<int:order_id>/export.<fmt>")
@login_required
def export(order_id, fmt):
    order = _get_order(order_id)
    safe = order.number.replace("/", "_")
    if fmt == "pdf":
        data = exports.order_to_pdf(order)
        log("export", "sales_order", order.id, detail="PDF export", commit=True)
        disp = "inline" if request.args.get("view") == "1" else "attachment"
        return Response(data, mimetype="application/pdf",
                        headers={"Content-Disposition": f"{disp}; filename=Order_{safe}.pdf"})
    if fmt == "xlsx":
        data = exports.order_to_excel(order)
        log("export", "sales_order", order.id, detail="Excel export", commit=True)
        return Response(data,
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename=Order_{safe}.xlsx"})
    abort(404)


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
