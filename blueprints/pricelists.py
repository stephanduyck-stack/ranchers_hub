"""Generic pricelist consultation, inline editing, bulk adjustment, exports."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify, abort, Response)
from flask_login import login_required, current_user

from extensions import db
from models import (Pricelist, PricelistLine, PricelistTier, LinePrice, Product,
                    Category, FixedPriceOverride, AuditLog)
from services.security import (edit_required, assert_can_see_pricelist,
                               manager_required, admin_required)
from services.audit import log
from services.pricing import format_money, effective_line_price
from services import exports
from services import approvals

bp = Blueprint("pricelists", __name__, url_prefix="/pricelists")


def _get_list(list_id, customer=False):
    pl = db.session.get(Pricelist, list_id)
    if pl is None or pl.is_customer != customer:
        abort(404)
    assert_can_see_pricelist(current_user, pl)
    return pl


# Order groups appear in on the Pricelists tab.
GROUP_ORDER = ["Business to Business", "Business to Distributor", "Betar",
               "Ranchers Finest Meat Supermarkets",
               "Export — Business to Business",
               "Export — Business to Distributor", "Export", "Other"]


def derived_group(name):
    """Default group for a list, used when group_name has not been set."""
    n = (name or "")
    low = n.lower()
    if "export" in low:
        # 27 Jul 2026: export lists split the same way as local ones.
        if "business to business" in low:
            return "Export — Business to Business"
        if "distributor" in low:
            return "Export — Business to Distributor"
        return "Export"
    if "business to business" in low:
        return "Business to Business"
    if "business to distributor" in low:
        return "Business to Distributor"
    if "betar" in low:
        return "Betar"
    if "meat" in low:
        return "Ranchers Finest Meat Supermarkets"
    return "Other"


def effective_group(pl):
    return pl.group_name or derived_group(pl.name)


@bp.route("/")
@login_required
def index():
    show_archived = request.args.get("archived") == "1"
    lists = db.session.scalars(
        db.select(Pricelist).filter_by(is_customer=False, archived=show_archived)
        .order_by(Pricelist.market, Pricelist.channel, Pricelist.name)).all()
    n_archived = db.session.scalar(
        db.select(db.func.count(Pricelist.id)).filter_by(is_customer=False, archived=True))

    # group lists, preserving GROUP_ORDER then any extra groups alphabetically
    groups = {}
    for pl in lists:
        groups.setdefault(effective_group(pl), []).append(pl)
    ordered = [(g, groups[g]) for g in GROUP_ORDER if g in groups]
    extras = sorted(g for g in groups if g not in GROUP_ORDER)
    ordered += [(g, groups[g]) for g in extras]

    all_groups = GROUP_ORDER + [g for g in extras if g not in GROUP_ORDER]
    return render_template("pricelists/index.html", grouped=ordered, today=date.today(),
                           show_archived=show_archived, n_archived=n_archived or 0,
                           all_groups=all_groups)


@bp.route("/<int:list_id>/vat", methods=["POST"])
@login_required
def set_vat(list_id):
    # Only the pricing officer (and admin) may switch VAT on/off for a pricelist.
    if not (current_user.is_pricing_officer or current_user.is_admin):
        abort(403)
    pl = db.session.get(Pricelist, list_id)
    if pl is None:
        abort(404)
    assert_can_see_pricelist(current_user, pl)
    pl.vat_applicable = request.form.get("vat_applicable") == "1"
    try:
        rate = float(request.form.get("vat_rate") or pl.vat_rate or 18.0)
        pl.vat_rate = rate
    except ValueError:
        pass
    log("pricelist_vat", "pricelist", pl.id,
        new_value=("VAT %g%%" % pl.vat_rate) if pl.vat_applicable else "Zero-rated",
        detail=f"VAT {'on' if pl.vat_applicable else 'off'} for '{pl.name}'")
    db.session.commit()
    flash(f"VAT {'enabled' if pl.vat_applicable else 'switched off'} for '{pl.name}'.", "success")
    if pl.is_customer:
        return redirect(url_for("customer_pricelists.detail", list_id=pl.id))
    return redirect(url_for("pricelists.detail", list_id=pl.id))


@bp.route("/<int:list_id>/small-orders", methods=["POST"])
@login_required
def set_small_orders(list_id):
    """Pricing officer/admin toggle: does this list allow orders below the box
    sizes? Off = quantities must be full-box multiples (22 Jul 2026)."""
    if not (current_user.is_pricing_officer or current_user.is_admin):
        abort(403)
    pl = db.session.get(Pricelist, list_id)
    if pl is None:
        abort(404)
    assert_can_see_pricelist(current_user, pl)
    pl.allow_small_orders = request.form.get("allow_small_orders") == "1"
    log("pricelist_small_orders", "pricelist", pl.id,
        new_value="small orders allowed" if pl.allow_small_orders else "box quantities only",
        detail=f"'{pl.name}'")
    db.session.commit()
    flash(f"'{pl.name}': {'small orders allowed' if pl.allow_small_orders else 'box quantities only'}.", "success")
    if pl.is_customer:
        return redirect(url_for("customer_pricelists.detail", list_id=pl.id))
    return redirect(url_for("pricelists.detail", list_id=pl.id))


@bp.route("/<int:list_id>/group", methods=["POST"])
@login_required
@manager_required
def set_group(list_id):
    pl = db.session.get(Pricelist, list_id)
    if pl is None or pl.is_customer:
        abort(404)
    pl.group_name = (request.form.get("group_name") or "").strip() or None
    log("pricelist_group", "pricelist", pl.id, new_value=effective_group(pl),
        detail=f"grouped '{pl.name}' under {effective_group(pl)}")
    db.session.commit()
    flash(f"'{pl.name}' moved to group: {effective_group(pl)}.", "success")
    return redirect(url_for("pricelists.detail", list_id=pl.id))


# ---------------------------------------------------------------------------
# Duplicate a generic pricelist for a new period
# ---------------------------------------------------------------------------
@bp.route("/<int:list_id>/duplicate", methods=["GET", "POST"])
@login_required
@manager_required
def duplicate(list_id):
    src = _get_list(list_id, customer=False)
    if request.method == "POST":
        name = (request.form.get("name") or f"{src.name} (new period)").strip()
        eff = _parse_date(request.form.get("effective_date")) or date.today()
        vu = _parse_date(request.form.get("valid_until"))
        pend = approvals.needs_approval(current_user)
        new_pl = Pricelist(
            name=name, channel=src.channel, market=src.market, currency=src.currency,
            vat_applicable=src.vat_applicable, vat_rate=src.vat_rate,
            effective_date=eff, valid_until=vu, notes=src.notes,
            is_customer=False, group_name=effective_group(src),
            base_pricelist_id=src.id, source_file=f"copied from '{src.name}'",
            approval_status="pending" if pend else "approved")
        db.session.add(new_pl)
        db.session.flush()
        tier_map = {}
        for t in src.tiers:
            nt = PricelistTier(pricelist_id=new_pl.id, key=t.key, label=t.label,
                               sort_order=t.sort_order)
            db.session.add(nt)
            db.session.flush()
            tier_map[t.key] = nt
        for line in src.lines:
            nl = PricelistLine(
                pricelist_id=new_pl.id, product_id=line.product_id,
                section=line.section, pack_size=line.pack_size,
                units_per_pack=line.units_per_pack, box_small=line.box_small,
                box_medium=line.box_medium, box_large=line.box_large,
                sort_order=line.sort_order)
            db.session.add(nl)
            db.session.flush()
            for lp in line.prices:
                db.session.add(LinePrice(line_id=nl.id, tier_id=tier_map[lp.tier.key].id,
                                         amount=lp.amount))
        log("pricelist_duplicate", "pricelist", new_pl.id,
            detail=f"'{new_pl.name}' copied from '{src.name}'")
        if pend:
            approvals.request_pricelist(new_pl, current_user)
            db.session.commit()
            flash("New pricelist created and submitted for approval. It stays hidden "
                  "from ordering until approved.", "success")
        else:
            db.session.commit()
            flash("New pricelist created from the copy. Adjust the prices below and they save as you edit.", "success")
        return redirect(url_for("pricelists.detail", list_id=new_pl.id))

    suggested = f"{src.name} — {date.today():%b %Y}"
    return render_template("pricelists/duplicate.html", src=src, suggested=suggested,
                           today=date.today())


# ---------------------------------------------------------------------------
# Archive / restore / delete a pricelist (generic or customer)
# ---------------------------------------------------------------------------
def _back_to(pl):
    if pl.is_customer:
        return url_for("customer_pricelists.index", archived=1 if pl.archived else None)
    return url_for("pricelists.index", archived=1 if pl.archived else None)


@bp.route("/<int:list_id>/archive", methods=["POST"])
@login_required
@manager_required
def archive(list_id):
    pl = db.session.get(Pricelist, list_id)
    if pl is None:
        abort(404)
    # Never let the page become empty: keep at least one active list of this kind.
    active_same_kind = db.session.scalar(
        db.select(db.func.count(Pricelist.id))
        .filter_by(is_customer=pl.is_customer, archived=False))
    if active_same_kind <= 1:
        flash("This is the last active pricelist — archive another one first so the "
              "list is never empty.", "warning")
        return redirect(_back_to(pl))
    pl.archived = True
    log("pricelist_archive", "pricelist", pl.id, detail=f"archived '{pl.name}'")
    db.session.commit()
    flash(f"'{pl.name}' archived. It is hidden from the main list but kept.", "success")
    return redirect(_back_to(pl))


@bp.route("/<int:list_id>/unarchive", methods=["POST"])
@login_required
@manager_required
def unarchive(list_id):
    pl = db.session.get(Pricelist, list_id)
    if pl is None:
        abort(404)
    pl.archived = False
    log("pricelist_unarchive", "pricelist", pl.id, detail=f"restored '{pl.name}'")
    db.session.commit()
    flash(f"'{pl.name}' restored.", "success")
    return redirect(_back_to(pl))


@bp.route("/<int:list_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete(list_id):
    pl = db.session.get(Pricelist, list_id)
    if pl is None:
        abort(404)
    name, was_customer = pl.name, pl.is_customer
    db.session.delete(pl)   # cascades to tiers, lines and prices
    log("pricelist_delete", "pricelist", list_id, detail=f"deleted '{name}'")
    db.session.commit()
    flash(f"'{name}' permanently deleted.", "success")
    if was_customer:
        return redirect(url_for("customer_pricelists.index"))
    return redirect(url_for("pricelists.index"))


@bp.route("/<int:list_id>")
@login_required
def detail(list_id):
    return _render_detail(list_id, customer=False)


def _render_detail(list_id, customer):
    pl = _get_list(list_id, customer=customer)
    display_ccy = request.args.get("ccy", pl.currency)
    q = (request.args.get("q") or "").strip().lower()
    cat = request.args.get("category", "")
    show_inactive = request.args.get("inactive") == "1"

    categories = sorted({l.product.category.full_name
                         for l in pl.lines if l.product.category}, key=str.lower)

    rows = []
    for line in pl.lines:
        p = line.product
        if not show_inactive and not p.is_active:
            continue
        if cat and (not p.category or p.category.full_name != cat):
            continue
        if q and q not in (p.article_no or "").lower() \
           and q not in (p.description or "").lower() \
           and q not in (p.barcode or "").lower():
            continue
        priced = {}
        for t in pl.tiers:
            priced[t.key] = effective_line_price(line, t.key, display_ccy=display_ccy)
        rows.append({"line": line, "product": p, "priced": priced})

    has_box = any(l.box_small or l.box_medium or l.box_large for l in pl.lines)
    can_edit = current_user.may_edit_prices
    tmpl = "customer_pricelists/detail.html" if customer else "pricelists/detail.html"
    return render_template(tmpl, pl=pl, rows=rows, tiers=pl.tiers,
                           has_box=has_box, can_edit=can_edit,
                           categories=categories, display_ccy=display_ccy,
                           q=request.args.get("q", ""), cat=cat,
                           show_inactive=show_inactive, today=date.today())


# ---------------------------------------------------------------------------
# Add an existing product as a new line on a pricelist (generic or customer)
# ---------------------------------------------------------------------------
@bp.route("/<int:list_id>/add-product", methods=["POST"])
@login_required
def add_product_line(list_id):
    pl = db.session.get(Pricelist, list_id)
    if pl is None:
        abort(404)
    assert_can_see_pricelist(current_user, pl)
    if not (current_user.can_manage_all or current_user.may_edit_prices):
        abort(403)

    def _back():
        if pl.is_customer:
            return url_for("customer_pricelists.detail", list_id=pl.id)
        return url_for("pricelists.detail", list_id=pl.id)

    art = (request.form.get("article_no") or "").strip().upper()
    product = db.session.scalar(db.select(Product).filter_by(article_no=art))
    if product is None:
        flash(f"No product with article number '{art}'. Create it first under Products.", "danger")
        return redirect(request.referrer or _back())
    if any(l.product_id == product.id for l in pl.lines):
        flash(f"{product.article_no} is already on '{pl.name}'.", "warning")
        return redirect(_back())

    max_sort = max([l.sort_order for l in pl.lines], default=0)
    line = PricelistLine(pricelist_id=pl.id, product_id=product.id,
                         section="ADDED PRODUCTS", pack_size=product.pack_size,
                         sort_order=max_sort + 1)
    db.session.add(line)
    db.session.flush()
    for t in pl.tiers:
        db.session.add(LinePrice(line_id=line.id, tier_id=t.id, amount=None))
    log("line_add", "pricelist", pl.id,
        detail=f"added {product.article_no} to '{pl.name}'")
    if approvals.needs_approval(current_user):
        approvals.stage_price_request(pl, current_user)
        db.session.commit()
        flash(f"Added {product.article_no} to '{pl.name}'. Set its prices below; "
              f"the change goes for approval before it is live.", "success")
    else:
        db.session.commit()
        flash(f"Added {product.article_no} to '{pl.name}'. Set its prices inline below.", "success")
    return redirect(_back() + f"?q={product.article_no}")


# ---------------------------------------------------------------------------
# Per-pricelist price history (filtered audit trail)
# ---------------------------------------------------------------------------
PRICE_ACTIONS = ("price_change", "bulk_adjust", "override_set", "override_clear")


@bp.route("/<int:list_id>/history")
@login_required
def history(list_id):
    return _history(list_id, customer=False)


@bp.route("/customer/<int:list_id>/history")
@login_required
def history_customer(list_id):
    return _history(list_id, customer=True)


def _history(list_id, customer):
    pl = _get_list(list_id, customer=customer)
    if not (current_user.can_manage_all or current_user.may_edit_prices):
        abort(403)
    line_ids = [l.id for l in pl.lines]
    # include line ids that may have been removed but still in the log for this list
    entries = []
    if line_ids:
        entries = db.session.scalars(
            db.select(AuditLog).where(
                AuditLog.entity_type == "pricelist_line",
                AuditLog.entity_id.in_(line_ids),
                AuditLog.action.in_(PRICE_ACTIONS))
            .order_by(AuditLog.ts.desc()).limit(500)).all()
    # also catch list-level entries (e.g. line_add) referencing this pricelist
    list_entries = db.session.scalars(
        db.select(AuditLog).where(
            AuditLog.entity_type == "pricelist", AuditLog.entity_id == pl.id,
            AuditLog.action.in_(("line_add", "pricelist_group", "pricelist_duplicate",
                                 "customer_pricelist_edit")))
        .order_by(AuditLog.ts.desc()).limit(200)).all()
    art = {l.id: (l.product.article_no, l.product.description) for l in pl.lines}
    rows = []
    for e in entries:
        a = art.get(e.entity_id, ("", e.detail or ""))
        rows.append({"ts": e.ts, "user": e.username, "article": a[0], "desc": a[1],
                     "field": e.field, "old": e.old_value, "new": e.new_value,
                     "action": e.action})
    return render_template("pricelists/history.html", pl=pl, rows=rows,
                           list_entries=list_entries, customer=customer)


# ---------------------------------------------------------------------------
# Inline price edit
# ---------------------------------------------------------------------------
@bp.route("/line/<int:line_id>/price", methods=["POST"])
@login_required
@edit_required
def edit_price(line_id):
    line = db.session.get(PricelistLine, line_id)
    if line is None:
        abort(404)
    assert_can_see_pricelist(current_user, line.pricelist)
    tier_key = request.form.get("tier")
    raw = (request.form.get("value") or "").replace(",", "").strip()
    tier = db.session.scalar(
        db.select(PricelistTier).filter_by(pricelist_id=line.pricelist_id, key=tier_key))
    if tier is None:
        return jsonify(ok=False, error="Unknown tier"), 400
    try:
        new_val = Decimal(raw) if raw != "" else None
    except InvalidOperation:
        return jsonify(ok=False, error="Not a number"), 400

    # Cost floor (QA audit 5 Jul 2026): refuse a price below the product cost.
    # Cost is per kg; the line's pack size decides the price basis.
    from services.cost_guard import below_cost_error
    err = below_cost_error(line.product, new_val, line.pricelist.currency,
                           pack_size=line.pack_size or line.product.pack_size)
    if err:
        return jsonify(ok=False, error=err), 400

    lp = db.session.scalar(
        db.select(LinePrice).filter_by(line_id=line.id, tier_id=tier.id))
    old_val = lp.amount if lp else None

    if approvals.needs_approval(current_user):
        # Stage the change; the live price stays until an approver signs off.
        if lp is None:
            lp = LinePrice(line_id=line.id, tier_id=tier.id, amount=None,
                           pending_amount=new_val)
            line.prices.append(lp)
        else:
            lp.pending_amount = new_val
        db.session.flush()
        approvals.stage_price_request(line.pricelist, current_user)
        log("price_change_pending", "pricelist_line", line.id, field=f"{tier.label}",
            old_value=old_val, new_value=new_val,
            detail=f"PENDING {line.product.article_no} on '{line.pricelist.name}'")
        db.session.commit()
        return jsonify(ok=True, pending=True,
                       formatted=format_money(new_val, line.pricelist.currency),
                       raw=(float(new_val) if new_val is not None else None),
                       message="Submitted for approval")

    if lp is None:
        lp = LinePrice(line_id=line.id, tier_id=tier.id, amount=new_val)
        db.session.add(lp)
    else:
        lp.amount = new_val

    log("price_change", "pricelist_line", line.id, field=f"{tier.label}",
        old_value=old_val, new_value=new_val,
        detail=f"{line.product.article_no} on '{line.pricelist.name}'")
    db.session.commit()
    return jsonify(ok=True,
                   formatted=format_money(new_val, line.pricelist.currency),
                   raw=(float(new_val) if new_val is not None else None))


# ---------------------------------------------------------------------------
# Bulk adjustment (preview + commit)
# ---------------------------------------------------------------------------
@bp.route("/<int:list_id>/bulk", methods=["GET", "POST"])
@login_required
@edit_required
def bulk(list_id):
    pl = _get_list(list_id, customer=False)
    return _bulk_handler(pl, "pricelists.detail")


@bp.route("/customer/<int:list_id>/bulk", methods=["GET", "POST"])
@login_required
@edit_required
def bulk_customer(list_id):
    pl = _get_list(list_id, customer=True)
    return _bulk_handler(pl, "customer_pricelists.detail")


def _bulk_handler(pl, back_endpoint):
    categories = sorted({l.product.category.full_name
                         for l in pl.lines if l.product.category}, key=str.lower)
    preview = None
    form = {}
    if request.method == "POST":
        form = request.form.to_dict()
        scope_cat = request.form.get("category", "")
        tier_key = request.form.get("tier", "__all__")
        mode = request.form.get("mode", "pct")  # pct | fixed
        try:
            amount = Decimal(request.form.get("amount", "0"))
        except InvalidOperation:
            amount = Decimal(0)
        commit = request.form.get("commit") == "1"

        changes = []
        for line in pl.lines:
            if scope_cat and (not line.product.category
                              or line.product.category.full_name != scope_cat):
                continue
            for lp in line.prices:
                if tier_key != "__all__" and lp.tier.key != tier_key:
                    continue
                if lp.amount is None:
                    continue
                old = Decimal(str(lp.amount))
                if mode == "pct":
                    new = old * (Decimal(1) + amount / Decimal(100))
                else:
                    new = old + amount
                new = new.quantize(Decimal("0.0001"))
                if new != old:
                    changes.append((line, lp, old, new))

        if commit:
            # Cost floor (QA audit 5 Jul 2026): a bulk decrease must not push
            # any line below its product cost. Refuse the whole commit and
            # name the violations so the user fixes data, not guesses.
            from services.cost_guard import below_cost_error
            violations = [e for e in (
                below_cost_error(line.product, new, pl.currency,
                                 pack_size=line.pack_size or line.product.pack_size)
                for line, lp, old, new in changes) if e]
            if violations:
                for e in violations[:5]:
                    flash(e, "danger")
                if len(violations) > 5:
                    flash(f"...and {len(violations) - 5} more below-cost lines.", "danger")
                return redirect(request.url)
            pend = approvals.needs_approval(current_user)
            for line, lp, old, new in changes:
                if pend:
                    lp.pending_amount = new
                else:
                    lp.amount = new
                log("bulk_adjust_pending" if pend else "bulk_adjust",
                    "pricelist_line", line.id, field=lp.tier.label,
                    old_value=old, new_value=new,
                    detail=f"bulk {mode} {amount} on '{pl.name}'")
            if pend:
                approvals.stage_price_request(pl, current_user)
                db.session.commit()
                flash(f"Submitted {len(changes)} price change(s) for approval.", "success")
            else:
                db.session.commit()
                flash(f"Applied to {len(changes)} price(s). Logged to history.", "success")
            return redirect(url_for(back_endpoint, list_id=pl.id))

        preview = [{
            "art": line.product.article_no,
            "desc": line.product.description,
            "tier": lp.tier.label,
            "old": format_money(old, pl.currency),
            "new": format_money(new, pl.currency),
        } for line, lp, old, new in changes[:300]]
        preview_count = len(changes)
        return render_template("pricelists/bulk.html", pl=pl, categories=categories,
                               tiers=pl.tiers, preview=preview,
                               preview_count=preview_count, form=form,
                               back_endpoint=back_endpoint)

    return render_template("pricelists/bulk.html", pl=pl, categories=categories,
                           tiers=pl.tiers, preview=None, form=form,
                           back_endpoint=back_endpoint)


# ---------------------------------------------------------------------------
# Fixed-price override (pricing person only)
# ---------------------------------------------------------------------------
@bp.route("/line/<int:line_id>/override", methods=["POST"])
@login_required
@edit_required
def set_override(line_id):
    line = db.session.get(PricelistLine, line_id)
    if line is None:
        abort(404)
    assert_can_see_pricelist(current_user, line.pricelist)
    action = request.form.get("action", "set")
    if action == "clear":
        if line.override:
            log("override_clear", "pricelist_line", line.id,
                old_value=f"{line.override.currency} {line.override.amount}",
                detail=f"{line.product.article_no}")
            db.session.delete(line.override)
            db.session.commit()
        flash("Fixed price cleared. Logged to history.", "success")
        return redirect(request.referrer or url_for("pricelists.detail", list_id=line.pricelist_id))

    ccy = request.form.get("currency", "USD")
    try:
        amount = Decimal(request.form.get("amount", "0"))
    except InvalidOperation:
        flash("Invalid amount.", "danger")
        return redirect(request.referrer or url_for("pricelists.detail", list_id=line.pricelist_id))
    vf = _parse_date(request.form.get("valid_from"))
    vu = _parse_date(request.form.get("valid_until"))
    note = request.form.get("note")

    if line.override:
        ov = line.override
        old = f"{ov.currency} {ov.amount}"
        ov.currency, ov.amount, ov.valid_from, ov.valid_until, ov.note = \
            ccy, amount, vf or date.today(), vu, note
    else:
        old = None
        ov = FixedPriceOverride(line_id=line.id, currency=ccy, amount=amount,
                                valid_from=vf or date.today(), valid_until=vu,
                                note=note, created_by=current_user.id)
        db.session.add(ov)
    log("override_set", "pricelist_line", line.id, old_value=old,
        new_value=f"{ccy} {amount}", detail=f"{line.product.article_no} fixed price")
    db.session.commit()
    flash("Fixed price pinned. Logged to history.", "success")
    return redirect(request.referrer or url_for("pricelists.detail", list_id=line.pricelist_id))


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------
@bp.route("/<int:list_id>/export.<fmt>")
@login_required
def export_generic(list_id, fmt):
    return _export(list_id, fmt, customer=False)


@bp.route("/customer/<int:list_id>/export.<fmt>")
@login_required
def export_customer(list_id, fmt):
    return _export(list_id, fmt, customer=True)


def _export(list_id, fmt, customer):
    pl = _get_list(list_id, customer=customer)
    safe = "".join(c if c.isalnum() else "_" for c in pl.name)[:50]
    if fmt == "xlsx":
        data = exports.pricelist_to_excel(pl)
        log("export", "pricelist", pl.id, detail=f"Excel export of '{pl.name}'", commit=True)
        return Response(data,
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename={safe}.xlsx"})
    if fmt == "pdf":
        data = exports.pricelist_to_pdf(pl)
        log("export", "pricelist", pl.id, detail=f"PDF export of '{pl.name}'", commit=True)
        disp = "inline" if request.args.get("view") == "1" else "attachment"
        return Response(data, mimetype="application/pdf",
                        headers={"Content-Disposition": f"{disp}; filename={safe}.pdf"})
    abort(404)


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
