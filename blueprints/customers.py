"""Customer management. Reps see only their assigned customers."""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort)
from flask_login import login_required, current_user

from datetime import date

from extensions import db
from models import (Customer, User, Pricelist, CustomerCategory, SalesOrder,
                    Offer, Message, Invoice)
from services.security import (manager_required, admin_required,
                               assert_can_see_customer, can_see_customer,
                               can_allocate_pricelists, hash_password)
from services.audit import log

def _temp_password():
    """Random 10-character temporary password, unambiguous alphabet (no 0/O,
    1/l/I). Shown once to the creator; the user replaces it at first sign-in."""
    import secrets
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


def portal_username(name):
    """Derive the portal username from the account name: lowercase, dots for
    spaces and symbols, numeric suffix on collisions. 'Cafe Javas' becomes
    cafe.javas. Shared by auto-provisioning and the admin New-user form."""
    import re
    base = re.sub(r"[^a-z0-9]+", ".", (name or "").lower()).strip(".") or "customer"
    base = base[:56]
    username, n = base, 1
    while db.session.scalar(db.select(User).filter_by(username=username)):
        n += 1
        username = f"{base}{n}"
    return username


def _provision_portal_login(c):
    """Auto-create the portal login for a freshly created customer or
    distributor: username derived from the name, random temporary password,
    forced change at first sign-in. Returns (user, temp_password), or
    (None, None) when the customer already carries a login. The password is
    returned in clear exactly once so the creator sees it; only the hash is
    stored. Caller commits."""
    existing = db.session.scalar(
        db.select(User).filter_by(customer_id=c.id, role="customer"))
    if existing:
        return None, None
    username = portal_username(c.name)
    temp_pw = _temp_password()
    u = User(username=username,
             full_name=(c.contact_name or c.name or username).strip(),
             email=c.email,
             role="customer",
             can_edit=False,
             is_active=True,
             customer_id=c.id,
             must_change_password=True,
             password_hash=hash_password(temp_pw))
    db.session.add(u)
    log("user_create", "user", None,
        detail=f"portal login {username} auto-created for {c.name}")
    return u, temp_pw


def _portal_user_for(c):
    """The portal login linked to this customer, or None."""
    return db.session.scalar(
        db.select(User).filter_by(customer_id=c.id, role="customer"))


def _reset_portal_password(u):
    """Issue a fresh temporary password and re-arm the forced change.
    Returns the new password in clear (shown/printed once)."""
    temp_pw = _temp_password()
    u.password_hash = hash_password(temp_pw)
    u.must_change_password = True
    log("portal_pw_reset", "user", u.id,
        detail=f"temporary password reset for {u.username}")
    return temp_pw


def _send_welcome_email(c, u, temp_pw=None):
    """Best-effort welcome email: username plus a signed 72-hour activation
    link where the customer sets their own password. No password travels by
    mail — spam filters flag credential mails, and the link is safer anyway.
    (temp_pw is accepted for call-site compatibility; the printed welcome
    sheet is the channel that carries a temporary password.)
    Returns (ok, reason). Never raises; call AFTER commit so an SMTP failure
    cannot roll back the customer."""
    from services import comms
    from services import settings as settings_svc
    from services.security import make_activation_token
    if u is None:
        return (False, "no login created")
    if not (c.email or "").strip():
        return (False, "no email address on file")
    company = settings_svc.get("company_name") or "Ranchers Finest"
    root = request.url_root.rstrip("/")
    activate_url = root + url_for("auth.activate", token=make_activation_token(u))
    who = (c.contact_name or c.name or "").strip()
    body = (
        f"Dear {who},\n"
        f"\n"
        f"Welcome to {company}. Your customer portal account is ready.\n"
        f"\n"
        f"Username: {u.username}\n"
        f"\n"
        f"Activate your account and choose your own password here\n"
        f"(the link works for 72 hours):\n"
        f"{activate_url}\n"
        f"\n"
        f"For later sign-ins, the portal lives at {root}/login\n"
        f"\n"
        f"How the portal works\n"
        f"1. My Pricelist — your agreed prices, always current.\n"
        f"2. New Order — pick products and quantities, then submit. You\n"
        f"   receive an order number and we confirm it.\n"
        f"3. Orders — follow each order from confirmation to delivery and\n"
        f"   download the order PDF.\n"
        f"4. Messages — questions or changes on an order go here. We reply\n"
        f"   in the portal.\n"
        f"5. Account — change your password any time.\n"
        f"\n"
        f"The full guide is on the Help page inside the portal.\n"
        f"Need a hand? Reply to this email or contact your sales\n"
        f"representative.\n"
        f"\n"
        f"{company}\n"
    )
    from services.email_templates import portal_welcome_html
    import os
    from flask import current_app
    html = portal_welcome_html(company, f"{root}/login", activate_url,
                               u.username, who)
    logo = os.path.join(current_app.static_folder, "img", "ranchers-logo.png")
    ok, reason = comms.send_email(c.email,
                                  f"Welcome to the {company} Customer Portal",
                                  body, html=html,
                                  inline_images={"rflogo": logo})
    log("welcome_email", "user", u.id,
        detail=f"welcome email to {c.email}: {'sent' if ok else reason}",
        commit=True)
    return ok, reason


DEFAULT_CUSTOMER_CATEGORIES = [
    "Supermarket", "Hotel", "Restaurant", "Café", "Butchery", "Caterer",
    "Fast Food / QSR", "School / Institution", "Hospital", "Wholesaler",
    "Embassy / NGO", "Other",
]


def ensure_customer_categories():
    if db.session.scalar(db.select(db.func.count(CustomerCategory.id))) == 0:
        for i, name in enumerate(DEFAULT_CUSTOMER_CATEGORIES):
            db.session.add(CustomerCategory(name=name, sort_order=i))
        db.session.commit()


def _categories():
    ensure_customer_categories()
    return db.session.scalars(
        db.select(CustomerCategory).order_by(CustomerCategory.sort_order, CustomerCategory.name)).all()


def _generic_lists():
    return db.session.scalars(
        db.select(Pricelist).filter_by(is_customer=False, archived=False)
        .order_by(Pricelist.group_name, Pricelist.name)).all()


def _grouped_generic():
    """Generic pricelists grouped by their display group, for the customer form."""
    from blueprints.pricelists import effective_group, GROUP_ORDER
    groups = {}
    for p in _generic_lists():
        groups.setdefault(effective_group(p), []).append(p)
    extras = sorted(g for g in groups if g not in GROUP_ORDER)
    return [(g, groups[g]) for g in GROUP_ORDER if g in groups] + \
           [(g, groups[g]) for g in extras]


def _customer_lists(exclude_customer_id=None):
    """All tailor-made (customer) pricelists, for allocating to other customers."""
    q = db.select(Pricelist).filter_by(is_customer=True, archived=False)
    rows = db.session.scalars(q.order_by(Pricelist.name)).all()
    if exclude_customer_id is not None:
        rows = [p for p in rows if p.customer_id != exclude_customer_id]
    return rows


def _apply_allocation(customer, form):
    ids = form.getlist("pricelists")
    customer.allowed_pricelists = (
        db.session.scalars(db.select(Pricelist).filter(Pricelist.id.in_(ids))).all()
        if ids else [])

bp = Blueprint("customers", __name__, url_prefix="/customers")


def _reps():
    return db.session.scalars(
        db.select(User).filter_by(is_active=True).order_by(User.full_name)).all()


def _save_fields(c, form, force_segment=None):
    c.name = (form.get("name") or c.name or "").strip()
    c.contact_name = form.get("contact_name")
    c.email = form.get("email")
    c.phone = form.get("phone")
    c.market = form.get("market", c.market or "local")
    c.default_currency = form.get("default_currency", c.default_currency or "UGX")
    c.segment = force_segment or form.get("segment", c.segment or "customer")
    if "proposed_payment_terms" in form:
        c.proposed_payment_terms = form.get("proposed_payment_terms")
    # Approved credit terms and account status are set only by pricing officer / admin.
    if can_allocate_pricelists(current_user) and "payment_terms" in form:
        c.payment_terms = form.get("payment_terms")
    if can_allocate_pricelists(current_user) and "account_status" in form:
        st = form.get("account_status")
        c.account_status = st if st in ("ok", "on_hold", "blocked") else "ok"
        c.account_note = form.get("account_note")
    # Back-order preference: the hidden marker travels with the form so an
    # unticked checkbox (absent from POST data) still means an explicit "no".
    if "backorders_marker" in form:
        c.backorders_allowed = form.get("backorders_allowed") == "1"
    # Credit terms at onboarding (22 Jul 2026): amount and days boxes on the
    # new-customer form, effective from day one. At CREATION the onboarding
    # user sets them; on later edits only CEO/CFO/finance manager (the
    # finance card on the profile stays their lever).
    if "credit_limit_amount" in form and (
            c.id is None or getattr(current_user, "can_clear_credit", False)):
        raw = (form.get("credit_limit_amount") or "").replace(",", "").strip()
        try:
            val = int(float(raw)) if raw else 0
        except ValueError:
            val = 0
        c.credit_limit_ugx = val if val > 0 else None
        rawd = (form.get("credit_limit_days") or "").strip()
        try:
            days = int(rawd) if rawd else 0
        except ValueError:
            days = 0
        c.credit_days = days if days > 0 else None
    c.category_id = form.get("category_id", type=int) or None
    c.area = form.get("area")
    c.address = form.get("address")
    def _f(name):
        v = (form.get(name) or "").strip()
        try:
            return float(v) if v else None
        except ValueError:
            return None
    c.latitude = _f("latitude")
    c.longitude = _f("longitude")
    c.notes = form.get("notes")
    # named contacts
    c.procurement_name = form.get("procurement_name")
    c.procurement_phone = form.get("procurement_phone")
    c.procurement_email = form.get("procurement_email")
    c.chef_name = form.get("chef_name")
    c.chef_phone = form.get("chef_phone")
    c.chef_email = form.get("chef_email")
    c.other_contact_name = form.get("other_contact_name")
    c.other_contact_phone = form.get("other_contact_phone")
    c.other_contact_email = form.get("other_contact_email")
    c.tax_id = form.get("tax_id")
    # delivery acceptance
    c.delivery_days = ",".join(form.getlist("delivery_days")) or None
    c.delivery_time_from = form.get("delivery_time_from") or None
    c.delivery_time_to = form.get("delivery_time_to") or None
    c.delivery_notes = form.get("delivery_notes") or None
    rep_ids = form.getlist("reps")
    c.reps = db.session.scalars(db.select(User).filter(User.id.in_(rep_ids))).all() if rep_ids else []
    # Only the pricing officer (and admin) may change pricelist allocation.
    if can_allocate_pricelists(current_user):
        _apply_allocation(c, form)


def _active_customer_ids(months=6):
    """Customer ids that bought within the last `months` (history + live)."""
    from datetime import date
    today = date.today()
    y, m = today.year, today.month - (months - 1)
    while m <= 0:
        m += 12
        y -= 1
    cutoff = date(y, m, 1)
    ids = set()
    for cid in db.session.scalars(db.select(Invoice.customer_id).where(
            Invoice.customer_id.isnot(None), Invoice.invoice_date >= cutoff,
            Invoice.payment_status != "Reversed", Invoice.untaxed > 0).distinct()):
        ids.add(cid)
    for cid in db.session.scalars(db.select(SalesOrder.customer_id).where(
            SalesOrder.customer_id.isnot(None), SalesOrder.order_date >= cutoff,
            SalesOrder.status.in_(("placed", "in_fulfillment", "pending",
                                   "ready_for_dispatch", "out_for_delivery",
                                   "dispatched", "delivered", "fulfilled"))).distinct()):
        ids.add(cid)
    return ids


@bp.route("/")
@login_required
def index():
    from datetime import timedelta
    cat = request.args.get("category", type=int)
    show_archived = request.args.get("archived") == "1"
    # Filter on when the record became a customer: since=mtd (this month) or
    # since=<days>. The dashboard's "New customers" tile links here with it.
    since = request.args.get("since")
    since_date = since_label = None
    if since == "mtd":
        today = date.today()
        since_date = date(today.year, today.month, 1)
        since_label = "new this month"
    elif since:
        try:
            days = int(since)
            since_date = date.today() - timedelta(days=days)
            since_label = f"new in the last {days} days"
        except ValueError:
            pass
    # New customers usually have no purchases yet, so the default 'active'
    # pill would hide them — default to 'all' when the since filter is on.
    status = request.args.get("status", "all" if since_date else "active")
    if status not in ("active", "inactive", "all"):
        status = "active"
    customers = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    if not (current_user.can_manage_all or current_user.is_order_manager):
        customers = [c for c in customers if can_see_customer(current_user, c)]
    customers = [c for c in customers if (c.segment or "customer") != "distributor"]
    n_archived = sum(1 for c in customers if c.archived)
    customers = [c for c in customers if bool(c.archived) == show_archived]
    if cat:
        customers = [c for c in customers if c.category_id == cat]
    rep_id = request.args.get("rep", type=int)
    if rep_id:
        customers = [c for c in customers
                     if any(r.id == rep_id for r in c.reps)]
    if since_date:
        customers = [c for c in customers
                     if c.created_at and c.created_at.date() >= since_date]
        customers.sort(key=lambda c: c.created_at, reverse=True)

    active_ids = _active_customer_ids(6)
    n_active = sum(1 for c in customers if c.id in active_ids)
    n_inactive = len(customers) - n_active
    if status == "active":
        customers = [c for c in customers if c.id in active_ids]
    elif status == "inactive":
        customers = [c for c in customers if c.id not in active_ids]

    rep_users = db.session.scalars(
        db.select(User).filter(User.is_active.is_(True),
                               User.role.in_(("rep", "sales_manager", "telesales")))
        .order_by(User.full_name)).all()
    return render_template("customers/index.html", customers=customers, cat=cat,
                           categories=_categories(), show_archived=show_archived,
                           n_archived=n_archived, status=status,
                           n_active=n_active, n_inactive=n_inactive,
                           active_ids=active_ids, since=since,
                           since_date=since_date, since_label=since_label,
                           rep_id=rep_id, rep_users=rep_users)


def _filtered_for_export():
    """Apply the export filters (status/rep/category/segment) to the customers
    the current user may see. Returns (customers, active_ids, meta)."""
    status = request.args.get("status", "all")
    rep_id = request.args.get("rep", type=int)
    cat = request.args.get("category", type=int)
    segment = request.args.get("segment", "customer")
    months = request.args.get("months", default=6, type=int)

    rows = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    if not (current_user.can_manage_all or current_user.is_order_manager):
        rows = [c for c in rows if can_see_customer(current_user, c)]
    if request.args.get("archived") != "1":
        rows = [c for c in rows if not c.archived]
    if segment in ("customer", "distributor"):
        rows = [c for c in rows if (c.segment or "customer") == segment]
    if cat:
        rows = [c for c in rows if c.category_id == cat]
    if rep_id:
        rows = [c for c in rows if any(r.id == rep_id for r in c.reps)]

    active_ids = _active_customer_ids(months)
    if status == "active":
        rows = [c for c in rows if c.id in active_ids]
    elif status == "inactive":
        rows = [c for c in rows if c.id not in active_ids]
    return rows, active_ids, {"status": status, "rep": rep_id, "category": cat,
                              "segment": segment, "months": months}


@bp.route("/export")
@login_required
def export_form():
    from services.customer_export import COLUMNS, DEFAULT_COLS
    reps = db.session.scalars(
        db.select(User).filter_by(role="rep").order_by(User.full_name)).all()
    return render_template("customers/export.html", reps=reps, categories=_categories(),
                           columns=COLUMNS, default_cols=DEFAULT_COLS)


@bp.route("/export.xlsx")
@login_required
def export_xlsx():
    from flask import send_file
    from services.customer_export import build_workbook, DEFAULT_COLS
    rows, active_ids, meta = _filtered_for_export()
    cols = request.args.getlist("col") or DEFAULT_COLS
    sort = request.args.get("sort", "name")
    if sort == "rep":
        rows.sort(key=lambda c: (", ".join(r.full_name for r in c.reps).lower(), c.name.lower()))
    elif sort == "status":
        rows.sort(key=lambda c: (c.id not in active_ids, c.name.lower()))
    bio = build_workbook(rows, cols, active_ids)
    label = meta["status"]
    fname = f"customers_{label}_{date.today():%Y%m%d}.xlsx"
    return send_file(bio, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/distributors")
@login_required
def distributors():
    from datetime import timedelta
    show_archived = request.args.get("archived") == "1"
    rows = db.session.scalars(
        db.select(Customer).filter_by(segment="distributor").order_by(Customer.name)).all()
    if not (current_user.can_manage_all or current_user.is_order_manager):
        rows = [c for c in rows if can_see_customer(current_user, c)]
    n_archived = sum(1 for c in rows if c.archived)
    rows = [c for c in rows if bool(c.archived) == show_archived]
    rep_id = request.args.get("rep", type=int)
    if rep_id:
        rows = [c for c in rows if any(r.id == rep_id for r in c.reps)]
    since = request.args.get("since")
    since_date = None
    if since == "mtd":
        today = date.today()
        since_date = date(today.year, today.month, 1)
    elif since:
        try:
            since_date = date.today() - timedelta(days=int(since))
        except ValueError:
            pass
    if since_date:
        rows = [c for c in rows
                if c.created_at and c.created_at.date() >= since_date]
        rows.sort(key=lambda c: c.created_at, reverse=True)
    rep_users = db.session.scalars(
        db.select(User).filter(User.is_active.is_(True),
                               User.role.in_(("rep", "sales_manager", "telesales")))
        .order_by(User.full_name)).all()
    return render_template("customers/distributors.html", distributors=rows,
                           show_archived=show_archived, n_archived=n_archived,
                           rep_id=rep_id, rep_users=rep_users, since=since)


@bp.route("/<int:customer_id>/archive", methods=["POST"])
@login_required
@manager_required
def archive(customer_id):
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    c.archived = True
    log("customer_archive", "customer", c.id, detail=f"archived {c.name}")
    db.session.commit()
    flash(f"{c.name} archived. The record is kept and can be restored.", "success")
    return redirect(url_for("customers.distributors" if c.segment == "distributor" else "customers.index"))


@bp.route("/<int:customer_id>/unarchive", methods=["POST"])
@login_required
@manager_required
def unarchive(customer_id):
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    c.archived = False
    log("customer_unarchive", "customer", c.id, detail=f"restored {c.name}")
    db.session.commit()
    flash(f"{c.name} restored.", "success")
    return redirect(url_for("customers.info", customer_id=c.id))


@bp.route("/<int:customer_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete(customer_id):
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    noun = "distributor" if c.segment == "distributor" else "customer"
    back = url_for("customers.distributors" if c.segment == "distributor" else "customers.index")
    # Guard: never delete a record with trading history — archive instead.
    if db.session.scalar(db.select(db.func.count(SalesOrder.id)).filter_by(customer_id=c.id)) \
       or db.session.scalar(db.select(db.func.count(Offer.id)).filter_by(customer_id=c.id)):
        flash(f"This {noun} has orders or offers on record — archive it instead so the "
              f"history is kept.", "danger")
        return redirect(url_for("customers.info", customer_id=c.id))
    if db.session.scalar(db.select(db.func.count(User.id)).filter_by(customer_id=c.id)):
        flash("Remove this customer's portal login first (Admin → Users), then delete.", "danger")
        return redirect(url_for("customers.info", customer_id=c.id))
    name = c.name
    # clean up dependent records that have no history value
    db.session.query(Message).filter_by(customer_id=c.id).delete(synchronize_session=False)
    for pl in db.session.scalars(db.select(Pricelist).filter_by(customer_id=c.id, is_customer=True)).all():
        db.session.delete(pl)
    c.reps = []
    c.allowed_pricelists = []
    db.session.delete(c)
    log("customer_delete", "customer", customer_id, detail=f"deleted {noun} {name}")
    db.session.commit()
    flash(f"{name} permanently deleted.", "success")
    return redirect(back)


@bp.route("/<int:customer_id>")
@login_required
def detail(customer_id):
    """Customer performance report (Stephan, 30 Jul 2026): the landing page
    per customer is a CEO-style report — sales by week and by month, product
    mix in kg by week and month, top performers and lapsed products. Contact
    details, portal access and account admin moved one level down (info)."""
    import re as _re
    from collections import defaultdict
    from datetime import timedelta
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    assert_can_see_customer(current_user, c)
    from models import Invoice, InvoiceLine, SalesHistory, Product

    today = date.today()
    monday = today - timedelta(days=today.weekday())
    weeks = [monday - timedelta(weeks=k) for k in range(11, -1, -1)]   # 12 weeks
    week_lo = weeks[0]
    wk8 = weeks[-8:]                                                   # matrix window
    cur_idx = today.year * 12 + today.month
    months = list(range(cur_idx - 11, cur_idx + 1))                    # 12 months

    def _i2d(idx):
        return date((idx - 1) // 12, (idx - 1) % 12 + 1, 1)

    # Kilograms per Odoo quantity unit: quantities in the invoice lines are
    # PACKS (integer counts of 500 g packs etc.), so the pack size string
    # converts to kg ("500 gr" -> 0.5, "4 x 250 Gr" -> 1.0, "+/- 2,5 kg" ->
    # 2.5 after stripping the prefix). Loose per-kg products parse to 1.
    # Unlinked or unparseable products count 1 qty = 1 kg and flag the page
    # as approximate.
    from services.inventory_costing import parse_pack_weight_kg
    prods = db.session.scalars(db.select(Product)).all()

    def _pwt(p):
        t = _re.sub(r"^[^0-9]*", "", str(p.pack_size or ""))
        w = parse_pack_weight_kg(t)
        if w:
            return w
        return 1.0 if (p.unit_of_measure or "").strip().lower() == "kg" else None

    pw = {p.id: _pwt(p) for p in prods}
    pname = {p.id: p.description for p in prods}
    approx = [False]

    def _kg(pid, qty):
        q = float(qty or 0)
        w = pw.get(pid)
        if w is None:
            if q:
                approx[0] = True
            return q
        return q * w

    invs = db.session.scalars(
        db.select(Invoice).where(Invoice.customer_id == c.id,
                                 Invoice.payment_status != "Reversed",
                                 Invoice.invoice_date.isnot(None))).all()
    last_purchase = max((i.invoice_date for i in invs), default=None)

    wk_rev, wk_n = defaultdict(float), defaultdict(int)
    mo_rev = defaultdict(float)
    for i in invs:
        v = float(i.untaxed or 0)
        mo_rev[i.invoice_date.year * 12 + i.invoice_date.month] += v
        ws = i.invoice_date - timedelta(days=i.invoice_date.weekday())
        if ws >= week_lo:
            wk_rev[ws] += v
            wk_n[ws] += 1

    # Product grain. Invoice lines are dated (running record since Jul 2026)
    # and own months after the monthly history pivot; sales_history owns its
    # own months — same ownership rule as the CEO dashboard, nothing double
    # counts.
    hist_cutover = db.session.scalar(
        db.select(db.func.max(SalesHistory.year * 12 + SalesHistory.month))) or 0
    line_rows = db.session.execute(
        db.select(Invoice.invoice_date, InvoiceLine.product_id,
                  InvoiceLine.product_name, InvoiceLine.amount,
                  InvoiceLine.quantity)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(Invoice.customer_id == c.id,
               Invoice.payment_status != "Reversed",
               Invoice.invoice_date.isnot(None))).all()

    win90 = today - timedelta(days=89)
    pweek = defaultdict(lambda: defaultdict(float))    # label -> week -> kg
    pmonth = defaultdict(lambda: defaultdict(float))   # label -> midx -> kg
    p90v, p90q = defaultdict(float), defaultdict(float)
    wk_kg = defaultdict(float)
    last_bought = {}
    for d, pid, pnm, amt, qty in line_rows:
        label = pname.get(pid) or pnm or "—"
        kg = _kg(pid, qty)
        idx = d.year * 12 + d.month
        ws = d - timedelta(days=d.weekday())
        if ws >= week_lo:
            pweek[label][ws] += kg
            wk_kg[ws] += kg
        if idx > hist_cutover and months[0] <= idx <= months[-1]:
            pmonth[label][idx] += kg
        if d >= win90:
            p90v[label] += float(amt or 0)
            p90q[label] += kg
        if label not in last_bought or d > last_bought[label]:
            last_bought[label] = d
    for s in db.session.scalars(db.select(SalesHistory).where(
            SalesHistory.customer_id == c.id, SalesHistory.month.isnot(None))):
        idx = s.year * 12 + s.month
        label = pname.get(s.product_id) or s.product or "—"
        if idx <= hist_cutover and months[0] <= idx <= months[-1]:
            pmonth[label][idx] += _kg(s.product_id, s.quantity)
        # month-grain fallback for the last-bought date (end of that month)
        if float(s.quantity or 0) > 0:
            nxt = _i2d(idx + 1)
            approx_last = nxt - timedelta(days=1)
            if label not in last_bought or approx_last > last_bought[label]:
                last_bought[label] = approx_last

    # tables — latest period first (Stephan, 30 Jul 2026)
    weeks_rows = [{"start": w, "rev": wk_rev.get(w, 0.0),
                   "kg": wk_kg.get(w, 0.0), "n": wk_n.get(w, 0)}
                  for w in reversed(weeks)]
    mo_kg = {m: sum(v.get(m, 0.0) for v in pmonth.values()) for m in months}
    months_rows = [{"label": _i2d(m).strftime("%B %Y"),
                    "rev": mo_rev.get(m, 0.0), "kg": mo_kg.get(m, 0.0)}
                   for m in reversed(months)]
    win6_rev = sum(r["rev"] for r in months_rows)
    win6_kg = sum(r["kg"] for r in months_rows)
    all_time_rev = sum(mo_rev.values())

    top_products = sorted(((k, v, p90q.get(k, 0.0)) for k, v in p90v.items()),
                          key=lambda kv: kv[1], reverse=True)[:10]

    # Lapsed: bought inside the 6-month window but nothing in the last 6 weeks.
    lapse_cut = today - timedelta(days=42)
    lapsed = []
    for label, per_m in pmonth.items():
        tot = sum(per_m.values())
        lb = last_bought.get(label)
        if tot > 0 and lb and lb < lapse_cut:
            lapsed.append({"name": label, "last": lb, "kg": tot})
    lapsed.sort(key=lambda r: r["kg"], reverse=True)
    lapsed = lapsed[:15]

    def _matrix(per, cols, cap=25):
        rows = sorted(per.items(),
                      key=lambda kv: sum(kv[1].values()), reverse=True)
        out = [{"name": k, "cells": [v.get(cc, 0.0) for cc in cols],
                "total": sum(v.get(cc, 0.0) for cc in cols)} for k, v in rows]
        out = [r for r in out if r["total"]]
        return out[:cap], max(0, len(out) - cap)

    # Matrix columns run oldest -> latest left to right (Stephan, 30 Jul
    # 2026); the week/month TABLES keep latest first top-down.
    matrix_week, mw_more = _matrix(pweek, wk8)
    matrix_month, mm_more = _matrix(pmonth, months)

    # Full invoice list at the bottom of the report, latest first, clickable
    # through to the invoice lines (Stephan, 30 Jul 2026). Capped at 100 on
    # screen; the count shows how many exist in total.
    inv_all_count = db.session.scalar(
        db.select(db.func.count(Invoice.id)).where(Invoice.customer_id == c.id)) or 0
    inv_list = db.session.scalars(
        db.select(Invoice).where(Invoice.customer_id == c.id)
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
        .limit(100)).all()

    from services.credit import outstanding_ugx, oldest_unpaid_days
    return render_template(
        "customers/report.html", customer=c,
        inv_list=inv_list, inv_all_count=inv_all_count,
        weeks=weeks, weeks_rows=weeks_rows, months_rows=months_rows,
        week_labels=[w.strftime("%d %b") for w in weeks],
        week_vals=[round(wk_rev.get(w, 0.0)) for w in weeks],
        win6_rev=win6_rev, win6_kg=win6_kg, all_time_rev=all_time_rev,
        mtd=mo_rev.get(cur_idx, 0.0), last_month=mo_rev.get(cur_idx - 1, 0.0),
        top_products=top_products, lapsed=lapsed,
        wk8=wk8, matrix_week=matrix_week, mw_more=mw_more,
        month_cols=[_i2d(m).strftime("%b %y") for m in months],
        matrix_month=matrix_month, mm_more=mm_more,
        last_purchase=last_purchase, approx_kg=approx[0],
        credit_outstanding=outstanding_ugx(c),
        credit_oldest_days=oldest_unpaid_days(c))


@bp.route("/<int:customer_id>/info")
@login_required
def info(customer_id):
    """Customer details & settings: contacts, portal access, credit admin,
    pricelists, CRM — everything the performance report links down to."""
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    assert_can_see_customer(current_user, c)
    from blueprints.crm import VISIT_OUTCOMES, CALL_OUTCOMES
    from services.permissions import has_perm
    from models import Deal, SalesHistory, Invoice, Product

    # Historical invoiced sales (2024-2026), if this customer is matched
    hist = db.session.scalars(
        db.select(SalesHistory).filter_by(customer_id=c.id)).all()
    pmap = {p.id: p.description for p in db.session.scalars(db.select(Product))}
    hist_years, hist_top, hist_returns = {}, {}, 0.0
    for h in hist:
        y = hist_years.setdefault(h.year, {"rev": 0.0, "qty": 0.0})
        y["rev"] += float(h.revenue or 0)
        y["qty"] += float(h.quantity or 0)
        lbl = pmap.get(h.product_id)        # catalogue products only in the mix
        if lbl:
            t = hist_top.setdefault(lbl, {"rev": 0.0, "qty": 0.0})
            t["rev"] += float(h.revenue or 0)
            t["qty"] += float(h.quantity or 0)
        if h.is_return:
            hist_returns += float(h.revenue or 0)
    hist_years = dict(sorted(hist_years.items()))
    # 27 Jul 2026: quantities travel with the values everywhere.
    hist_top = sorted(((k, v["rev"], v["qty"]) for k, v in hist_top.items()),
                      key=lambda kv: kv[1], reverse=True)[:8]

    # invoice history (dated) + outstanding for this customer
    invs = db.session.scalars(
        db.select(Invoice).filter_by(customer_id=c.id)
        .order_by(Invoice.invoice_date.desc())).all()
    inv_recent = invs[:15]
    inv_count = len(invs)
    inv_outstanding = sum(
        float(i.total or 0) for i in invs
        if float(i.total or 0) > 0
        and i.payment_status in ("Not Paid", "Partially Paid", "In Payment"))

    # Portal access: linked login plus the welcome-email / password-reset trail
    from models import AuditLog
    portal_user = _portal_user_for(c)
    portal_trail = []
    if portal_user:
        portal_trail = db.session.scalars(
            db.select(AuditLog)
            .where(AuditLog.entity_type == "user",
                   AuditLog.entity_id == portal_user.id,
                   AuditLog.action.in_(("welcome_email", "portal_pw_reset")))
            .order_by(AuditLog.ts.desc()).limit(5)).all()

    return render_template("customers/detail.html", customer=c,
                           visit_outcomes=VISIT_OUTCOMES, call_outcomes=CALL_OUTCOMES,
                           deal_stages=Deal.STAGES,
                           can_log=has_perm(current_user, "log_activity"),
                           can_allocate=can_allocate_pricelists(current_user),
                           credit_outstanding=__import__("services.credit", fromlist=["outstanding_ugx"]).outstanding_ugx(c),
                           credit_oldest_days=__import__("services.credit", fromlist=["oldest_unpaid_days"]).oldest_unpaid_days(c),
                           hist_years=hist_years, hist_top=hist_top,
                           hist_returns=hist_returns, inv_recent=inv_recent,
                           inv_count=inv_count, inv_outstanding=inv_outstanding,
                           portal_user=portal_user, portal_trail=portal_trail)


@bp.route("/invoice/<int:inv_id>")
@login_required
def invoice_detail(inv_id):
    """One imported invoice or credit note, full header detail.

    The Odoo export carries headers only (no line items), so this shows
    everything the import has: dates, amounts, VAT, status, salesperson,
    EFRIS. Access follows the customer: whoever may see the customer may
    see their documents; unmatched documents need manage rights."""
    from models import Invoice
    inv = db.session.get(Invoice, inv_id)
    if inv is None:
        abort(404)
    if inv.customer is not None:
        assert_can_see_customer(current_user, inv.customer)
    elif not current_user.can_manage_all:
        abort(403)
    is_credit = (inv.number or "").upper().startswith("RINV") or \
        float(inv.total or 0) < 0
    vat = None
    if inv.total is not None and inv.untaxed is not None:
        vat = float(inv.total) - float(inv.untaxed)
    # Same customer, around the same date — quick context for the viewer.
    related = []
    if inv.customer_id:
        related = db.session.scalars(
            db.select(Invoice).where(Invoice.customer_id == inv.customer_id,
                                     Invoice.id != inv.id)
            .order_by(Invoice.invoice_date.desc()).limit(10)).all()
    return render_template("customers/invoice_detail.html", inv=inv,
                           is_credit=is_credit, vat=vat, related=related)


ONBOARD_ROLES = ("rep", "telesales", "manager", "order_manager", "admin")


def _can_onboard(user):
    return getattr(user, "role", None) in ONBOARD_ROLES


@bp.route("/onboard", methods=["GET", "POST"])
@login_required
def onboard():
    """A rep registers a new customer. It lands as 'pending' for the pricing
    officer to allocate a pricelist and approve credit terms."""
    if not _can_onboard(current_user):
        abort(403)
    if request.method == "POST":
        c = Customer()
        seg = "distributor" if request.form.get("segment") == "distributor" else "customer"
        _save_fields(c, request.form, force_segment=seg)
        c.onboarding_status = "pending"
        c.credit_approved = False
        c.created_by_id = current_user.id
        # the creating rep covers it unless they ticked others
        if not c.reps:
            c.reps = [current_user]
        db.session.add(c)
        db.session.flush()
        login, temp_pw = _provision_portal_login(c)
        log("customer_onboard", "customer", None,
            detail=f"{c.name} registered by {current_user.full_name} (pending allocation)")
        db.session.commit()
        extra = ""
        if login:
            sent, reason = _send_welcome_email(c, login, temp_pw)
            if sent:
                extra = (f" Portal login '{login.username}' created and the "
                         f"welcome email with the login details was sent to "
                         f"{c.email}.")
            else:
                extra = (f" Portal login: username '{login.username}', "
                         f"temporary password '{temp_pw}'. Shown once only — "
                         f"pass both to the customer yourself (email not "
                         f"sent: {reason}). They set their own password at "
                         "first sign-in.")
        flash("Customer registered. The Pricing Officer will allocate a pricelist and "
              f"approve the credit terms before ordering.{extra}", "success")
        return redirect(url_for("customers.info", customer_id=c.id))
    return render_template("customers/edit.html", customer=None, reps=_reps(),
                           pricelist_groups=_grouped_generic(), categories=_categories(),
                           customer_lists=_customer_lists(), can_allocate=False,
                           onboarding=True, is_distributor=False)


@bp.route("/onboarding")
@login_required
def onboarding_queue():
    """Customers awaiting pricelist allocation / credit approval."""
    rows = db.session.scalars(
        db.select(Customer).filter_by(onboarding_status="pending", archived=False)
        .order_by(Customer.created_at.desc())).all()
    if not (current_user.can_manage_all or can_allocate_pricelists(current_user)):
        rows = [c for c in rows if can_see_customer(current_user, c)]
    return render_template("customers/onboarding.html", rows=rows)


@bp.route("/<int:customer_id>/approve", methods=["POST"])
@login_required
def approve_onboarding(customer_id):
    if not can_allocate_pricelists(current_user):
        abort(403)
    from services.allocation import allowed_pricelists_for
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    if not allowed_pricelists_for(c):
        flash("Allocate at least one pricelist before approving.", "warning")
        return redirect(url_for("customers.edit", customer_id=c.id))
    if not (c.payment_terms or "").strip():
        flash("Set the approved credit terms before approving.", "warning")
        return redirect(url_for("customers.edit", customer_id=c.id))
    c.onboarding_status = "approved"
    c.credit_approved = True
    log("customer_approve", "customer", c.id,
        detail=f"{c.name} approved (terms: {c.payment_terms})")
    db.session.commit()
    flash(f"{c.name} approved and ready to order.", "success")
    return redirect(url_for("customers.info", customer_id=c.id))


def _require_credit_role():
    if not getattr(current_user, "can_clear_credit", False):
        abort(403)


@bp.route("/<int:customer_id>/credit-clearance", methods=["POST"])
@login_required
def credit_clearance(customer_id):
    """CEO/CFO/finance manager tick or untick the finance credit clearance
    on the customer profile. The tick gates order acceptance."""
    from datetime import datetime
    _require_credit_role()
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    if request.form.get("action") == "clear":
        c.credit_cleared = True
        c.credit_cleared_by_id = current_user.id
        c.credit_cleared_at = datetime.utcnow()
        log("credit_clear", "customer", c.id,
            detail=f"{c.name} credit-cleared by {current_user.full_name}")
        msg = f"{c.name} cleared for order fulfilment."
        # Clearing from the profile also settles any open credit alerts (once
        # the account is not blocked) and tells the order managers their
        # waiting orders are ready to accept.
        if c.account_status != "blocked":
            from models import CreditAlert
            from services import credit
            open_alerts = db.session.scalars(
                db.select(CreditAlert).filter_by(customer_id=c.id, status="open")
                .filter(CreditAlert.reason != "over_limit")).all()
            if open_alerts:
                for a in open_alerts:
                    a.status = "unblocked"
                    a.decided_by_id = current_user.id
                    a.decided_at = datetime.utcnow()
                    a.note = "Cleared from the customer profile"
                credit.notify_order_managers(
                    c, [a.order for a in open_alerts if a.order], current_user)
                msg += (f" {len(open_alerts)} waiting order(s) released; "
                        f"order managers notified.")
        flash(msg, "success")
    else:
        c.credit_cleared = False
        c.credit_cleared_by_id = current_user.id
        c.credit_cleared_at = datetime.utcnow()
        log("credit_unclear", "customer", c.id,
            detail=f"{c.name} clearance removed by {current_user.full_name}")
        flash(f"Clearance removed. New orders for {c.name} wait for finance.",
              "warning")
    db.session.commit()
    return redirect(url_for("customers.info", customer_id=c.id))


@bp.route("/<int:customer_id>/credit-limit", methods=["POST"])
@login_required
def credit_limit(customer_id):
    """CEO/CFO/finance manager set or clear the credit limit (whole UGX).
    A set limit arms the automatic block against the outstanding balance."""
    _require_credit_role()
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    raw = (request.form.get("credit_limit") or "").replace(",", "").strip()
    try:
        val = int(float(raw)) if raw else 0
    except ValueError:
        flash("Enter the credit limit as a number in UGX.", "warning")
        return redirect(url_for("customers.info", customer_id=c.id))
    c.credit_limit_ugx = val if val > 0 else None
    rawd = (request.form.get("credit_days") or "").strip()
    try:
        days = int(rawd) if rawd else 0
    except ValueError:
        days = 0
    c.credit_days = days if days > 0 else None
    log("credit_limit", "customer", c.id,
        detail=f"{c.name} credit limits set by {current_user.full_name}: "
               f"UGX {val:,} / {days} days")
    db.session.commit()
    flash(f"Credit limits saved for {c.name}: "
          f"{'UGX {:,}'.format(val) if val > 0 else 'no value limit'}, "
          f"{str(days) + ' days' if days > 0 else 'no days limit'}.", "success")
    return redirect(url_for("customers.info", customer_id=c.id))


@bp.route("/credit-alerts")
@login_required
def credit_alerts():
    """Finance queue: orders held because the account is blocked or not
    credit-cleared. CFO / finance manager / CEO decide here."""
    from models import CreditAlert
    _require_credit_role()
    from services import credit
    open_alerts = db.session.scalars(
        db.select(CreditAlert).filter_by(status="open")
        .order_by(CreditAlert.created_at.asc())).all()
    decided = db.session.scalars(
        db.select(CreditAlert).filter(CreditAlert.status != "open")
        .order_by(CreditAlert.decided_at.desc()).limit(30)).all()
    return render_template("customers/credit_alerts.html",
                           open_alerts=open_alerts, decided=decided,
                           outstanding=credit.outstanding_ugx)


@bp.route("/credit-alerts/<int:alert_id>/decide", methods=["POST"])
@login_required
def credit_alert_decide(alert_id):
    """Unblock the account (back to ok + cleared) or keep it blocked (the
    customer gets a portal message, the reps get an email)."""
    from models import CreditAlert
    from services import credit
    _require_credit_role()
    a = db.session.get(CreditAlert, alert_id)
    if a is None:
        abort(404)
    if a.status != "open":
        flash("This alert was already decided.", "warning")
        return redirect(url_for("customers.credit_alerts"))
    decision = request.form.get("decision")
    if decision not in ("unblock", "keep_blocked", "release_order"):
        abort(400)
    if decision == "release_order" and a.reason not in ("over_limit", "overdue"):
        abort(400)
    outcome = credit.decide(a, current_user, decision,
                            note=request.form.get("note"))
    db.session.commit()
    if outcome == "released":
        flash(f"Order {a.order.number if a.order else ''} released past the "
              f"credit limit. The order managers were told to accept it.",
              "success")
    elif outcome == "unblocked":
        flash(f"{a.customer.name} unblocked and cleared. "
              f"Order {a.order.number if a.order else ''} can now be accepted.",
              "success")
    else:
        flash(f"{a.customer.name} stays blocked. The customer and the rep were "
              f"informed the order is on hold.", "warning")
    return redirect(url_for("customers.credit_alerts"))


@bp.route("/<int:customer_id>/portal/resend-email", methods=["POST"])
@login_required
@manager_required
def portal_resend_email(customer_id):
    """Reset the temporary password and resend the welcome email."""
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    u = _portal_user_for(c)
    if u is None:
        flash("No portal login exists for this account yet.", "warning")
        return redirect(url_for("customers.info", customer_id=c.id))
    temp_pw = _reset_portal_password(u)
    db.session.commit()
    sent, reason = _send_welcome_email(c, u)
    if sent:
        flash(f"Fresh welcome email sent to {c.email} with a new activation "
              "link. Earlier links no longer work.", "success")
    else:
        flash(f"Password was reset but the email was not sent ({reason}). "
              f"Temporary password for '{u.username}': '{temp_pw}'. "
              "Shown once only.", "warning")
    return redirect(url_for("customers.info", customer_id=c.id))


@bp.route("/<int:customer_id>/portal/welcome.pdf", methods=["POST"])
@login_required
@manager_required
def portal_welcome_pdf(customer_id):
    """Reset the temporary password and download the welcome sheet PDF with
    the fresh credentials and the short portal guide."""
    from flask import Response
    from services import exports
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    u = _portal_user_for(c)
    if u is None:
        flash("No portal login exists for this account yet.", "warning")
        return redirect(url_for("customers.info", customer_id=c.id))
    temp_pw = _reset_portal_password(u)
    db.session.commit()
    portal_url = request.url_root.rstrip("/") + "/login"
    data = exports.portal_welcome_pdf(c, u, temp_pw, portal_url)
    from werkzeug.utils import secure_filename
    fname = secure_filename(f"Portal_Access_{c.name}.pdf") or "Portal_Access.pdf"
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@bp.route("/<int:customer_id>/portal/create", methods=["POST"])
@login_required
@manager_required
def portal_create(customer_id):
    """Create the portal login for an account that predates auto-provisioning."""
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    if _portal_user_for(c):
        flash("This account already has a portal login.", "warning")
        return redirect(url_for("customers.info", customer_id=c.id))
    login, temp_pw = _provision_portal_login(c)
    db.session.commit()
    sent, reason = _send_welcome_email(c, login, temp_pw)
    if sent:
        flash(f"Portal login '{login.username}' created and the welcome email "
              f"was sent to {c.email}.", "success")
    else:
        flash(f"Portal login created: username '{login.username}', temporary "
              f"password '{temp_pw}'. Shown once only (email not sent: "
              f"{reason}).", "warning")
    return redirect(url_for("customers.info", customer_id=c.id))


@bp.route("/new", methods=["GET", "POST"])
@login_required
@manager_required
def new():
    if request.method == "POST":
        c = Customer()
        _save_fields(c, request.form)
        db.session.add(c)
        db.session.flush()
        login, temp_pw = _provision_portal_login(c)
        log("customer_create", "customer", None, detail=c.name)
        db.session.commit()
        if login:
            sent, reason = _send_welcome_email(c, login, temp_pw)
            if sent:
                flash(f"Customer created. Portal login '{login.username}' "
                      f"created and the welcome email with the login details "
                      f"was sent to {c.email}.", "success")
            else:
                flash(f"Customer created. Portal login: username "
                      f"'{login.username}', temporary password '{temp_pw}'. "
                      f"Shown once only — pass both to the customer yourself "
                      f"(email not sent: {reason}). They set their own "
                      "password at first sign-in.", "warning")
        else:
            flash("Customer created.", "success")
        return redirect(url_for("customers.info", customer_id=c.id))
    return render_template("customers/edit.html", customer=None, reps=_reps(),
                           pricelist_groups=_grouped_generic(), categories=_categories(),
                           customer_lists=_customer_lists(),
                           can_allocate=can_allocate_pricelists(current_user),
                           is_distributor=False)


@bp.route("/distributors/new", methods=["GET", "POST"])
@login_required
@manager_required
def distributor_new():
    if request.method == "POST":
        c = Customer()
        _save_fields(c, request.form, force_segment="distributor")
        db.session.add(c)
        db.session.flush()
        login, temp_pw = _provision_portal_login(c)
        log("customer_create", "customer", None, detail=f"distributor {c.name}")
        db.session.commit()
        if login:
            sent, reason = _send_welcome_email(c, login, temp_pw)
            if sent:
                flash(f"Distributor created. Portal login '{login.username}' "
                      f"created and the welcome email with the login details "
                      f"was sent to {c.email}.", "success")
            else:
                flash(f"Distributor created. Portal login: username "
                      f"'{login.username}', temporary password '{temp_pw}'. "
                      f"Shown once only — pass both to the distributor "
                      f"yourself (email not sent: {reason}). They set their "
                      "own password at first sign-in.", "warning")
        else:
            flash("Distributor created.", "success")
        return redirect(url_for("customers.info", customer_id=c.id))
    return render_template("customers/edit.html", customer=None, reps=_reps(),
                           pricelist_groups=_grouped_generic(), categories=_categories(),
                           customer_lists=_customer_lists(),
                           can_allocate=can_allocate_pricelists(current_user),
                           is_distributor=True)


def _can_edit_customer():
    """Customer profiles are edited by catalogue managers (managers, admins,
    pricing officer per the permission matrix) and, since 21 Jul 2026, order
    managers — they own the ordering relationship day to day."""
    from services.permissions import has_perm
    return has_perm(current_user, "manage_catalogue") or current_user.can_accept_orders


@bp.route("/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
def edit(customer_id):
    if not _can_edit_customer():
        abort(403)
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    if request.method == "POST":
        _save_fields(c, request.form)
        log("customer_edit", "customer", c.id, detail=c.name)
        db.session.commit()
        flash("Saved.", "success")
        return redirect(url_for("customers.info", customer_id=c.id))
    return render_template("customers/edit.html", customer=c, reps=_reps(),
                           pricelist_groups=_grouped_generic(), categories=_categories(),
                           customer_lists=_customer_lists(exclude_customer_id=c.id),
                           can_allocate=can_allocate_pricelists(current_user),
                           is_distributor=(c.segment == "distributor"))


# ---------------------------------------------------------------------------
# Daily finance uploads (27 Jul 2026). Finance clerk / finance manager / CFO
# drop the three daily Odoo exports here — itemized invoices, payments,
# customer receivables — in any order, any combination. The file type is
# auto-detected from the header row and every import is idempotent, so
# re-uploading the same file changes nothing. The receivables file is the one
# that resets credit balances (snapshot + newer unpaid invoices).
# ---------------------------------------------------------------------------
@bp.route("/finance-uploads", methods=["GET", "POST"])
@login_required
def finance_uploads():
    import os
    from datetime import datetime
    from flask import current_app
    from werkzeug.utils import secure_filename
    from services.permissions import has_perm
    from models import ImportReport, CustomerPayment

    if not has_perm(current_user, "import_finance_files"):
        abort(403)

    if request.method == "POST":
        from services import sales_import as si
        files = [f for f in request.files.getlist("files") if f and f.filename]
        if not files:
            flash("Choose one or more .xlsx files.", "danger")
            return redirect(url_for("customers.finance_uploads"))
        os.makedirs(current_app.config["UPLOAD_DIR"], exist_ok=True)
        for file in files:
            if not file.filename.lower().endswith((".xlsx", ".xlsm")):
                flash(f"{file.filename}: not an .xlsx file, skipped.", "danger")
                continue
            safe = secure_filename(file.filename)
            path = os.path.join(current_app.config["UPLOAD_DIR"],
                                f"{datetime.utcnow():%Y%m%d%H%M%S}_{safe}")
            file.save(path)
            try:
                r = si.import_auto(path)
            except Exception as e:  # noqa: BLE001
                db.session.rollback()
                flash(f"{file.filename}: import failed — {e}", "danger")
                continue
            summary = (r.get("message")
                       or (f"{r['read']} rows: {r['inserted']} new, "
                           f"{r['updated']} updated."))
            db.session.add(ImportReport(
                source=f"finance upload ({r['layout']}): {file.filename}",
                rows_ok=r.get("read", 0),
                rows_failed=len(r.get("unmatched_names", []) or []),
                detail="\n".join((r.get("unmatched_names") or [])[:100]) or None))
            log("import", "finance_upload", None,
                detail=f"{r['layout']}: {file.filename} — {summary}",
                commit=True)
            flash(f"{file.filename}: {summary}", "success")
        return redirect(url_for("customers.finance_uploads"))

    inv_count = db.session.scalar(db.select(db.func.count(Invoice.id))) or 0
    inv_last = db.session.scalar(db.select(db.func.max(Invoice.invoice_date)))
    pay_count = db.session.scalar(db.select(db.func.count(CustomerPayment.id))) or 0
    pay_last = db.session.scalar(db.select(db.func.max(CustomerPayment.payment_date)))
    snap_date = db.session.scalar(db.select(db.func.max(Customer.odoo_receivable_at)))
    snap_count = db.session.scalar(
        db.select(db.func.count(Customer.id))
        .where(Customer.odoo_receivable.isnot(None))) or 0
    recent = db.session.scalars(
        db.select(ImportReport).order_by(ImportReport.ts.desc()).limit(10)).all()
    return render_template("customers/finance_uploads.html",
                           inv_count=inv_count, inv_last=inv_last,
                           pay_count=pay_count, pay_last=pay_last,
                           snap_date=snap_date, snap_count=snap_count,
                           recent=recent)
