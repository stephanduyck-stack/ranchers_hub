"""Admin: users & rights, customer-rep assignment, categories, global settings,
audit log, and the UI flow to upload and map a new Excel pricelist."""
import os
from datetime import datetime, date

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort, current_app, Response, session)
from flask_login import login_required, current_user

from extensions import db
from models import (User, Customer, Category, Setting, AuditLog, ImportReport,
                    Pricelist, CustomerCategory)
from services.security import admin_required, hash_password
from services.audit import log
from services import settings as settings_svc

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.before_request
@login_required
def _guard():
    if not current_user.is_admin:
        abort(403)


# ---------------------------------------------------------------------------
# Users & edit rights
# ---------------------------------------------------------------------------
def _sales_managers():
    return db.session.scalars(
        db.select(User).filter_by(role="sales_manager").order_by(User.full_name)).all()


@bp.route("/users")
def users():
    """Users split into three tabs: internal staff, customer portal logins,
    and distributor portal logins (split on the linked customer's segment)."""
    all_users = db.session.scalars(db.select(User).order_by(User.full_name)).all()
    internal, customer_logins, distributor_logins = [], [], []
    for u in all_users:
        if u.role != "customer":
            internal.append(u)
        elif u.customer and (u.customer.segment or "customer") == "distributor":
            distributor_logins.append(u)
        else:
            customer_logins.append(u)
    tab = request.args.get("tab", "internal")
    if tab not in ("internal", "customers", "distributors"):
        tab = "internal"
    return render_template("admin/users.html", internal=internal,
                           customer_logins=customer_logins,
                           distributor_logins=distributor_logins, tab=tab)


@bp.route("/users/new", methods=["GET", "POST"])
def user_new():
    """New-user form, context-aware per users tab. kind=internal offers staff
    roles only; kind=customer/distributor locks the role to a portal login,
    filters the account picker to the matching segment, and derives the
    username from the account name."""
    kind = (request.form.get("kind") if request.method == "POST"
            else request.args.get("kind")) or "internal"
    if kind not in ("internal", "customer", "distributor"):
        kind = "internal"
    cust_q = db.select(Customer).filter_by(archived=False).order_by(Customer.name)
    if kind in ("customer", "distributor"):
        rows = db.session.scalars(cust_q).all()
        customers = [c for c in rows
                     if ((c.segment or "customer") == "distributor") == (kind == "distributor")]
    else:
        customers = db.session.scalars(cust_q).all()

    def form(msg=None, level="danger"):
        if msg:
            flash(msg, level)
        return render_template("admin/user_edit.html", user=None, kind=kind,
                               customers=customers, managers=_sales_managers())

    if request.method == "POST":
        pw = request.form.get("password") or ""
        if len(pw) < 8:
            return form("Password must be at least 8 characters.")
        link_cid = request.form.get("link_customer_id")
        if kind in ("customer", "distributor"):
            # Portal login: role locked, username derived from the account name.
            from blueprints.customers import portal_username
            if not link_cid:
                return form(f"Choose the {kind} this login belongs to.")
            c = db.session.get(Customer, int(link_cid))
            if c is None:
                return form(f"Choose the {kind} this login belongs to.")
            seg = c.segment or "customer"
            if (seg == "distributor") != (kind == "distributor"):
                return form(f"'{c.name}' is not a {kind}.")
            role = "customer"
            username = portal_username(c.name)
            full_name = (request.form.get("full_name") or c.contact_name
                         or c.name).strip()
            email = request.form.get("email") or c.email
        else:
            username = (request.form.get("username") or "").strip()
            if not username:
                return form("Username is required.")
            if db.session.scalar(db.select(User).filter_by(username=username)):
                return form("That username is taken.")
            role = request.form.get("role", "rep")
            if role == "customer":
                return form("Portal logins are created from the Customer or "
                            "Distributor tab, or automatically when the "
                            "customer is created.")
            full_name = (request.form.get("full_name") or username).strip()
            email = request.form.get("email")
        u = User(username=username,
                 full_name=full_name,
                 email=email,
                 role=role,
                 can_edit=bool(request.form.get("can_edit")) if kind == "internal" else False,
                 is_active=bool(request.form.get("is_active")),
                 customer_id=(int(link_cid) if role == "customer" and link_cid else None),
                 password_hash=hash_password(pw))
        mgr = request.form.get("manager_id")
        u.manager_id = int(mgr) if (role == "rep" and mgr) else None
        cust_ids = request.form.getlist("customers")
        u.assigned_customers = db.session.scalars(
            db.select(Customer).filter(Customer.id.in_(cust_ids))).all() \
            if (cust_ids and role != "customer") else []
        out_ids = request.form.getlist("portal_customers")
        u.portal_customers = db.session.scalars(
            db.select(Customer).filter(Customer.id.in_(out_ids))).all() \
            if (u.role == "customer" and out_ids) else []
        if u.role == "customer":
            u.must_change_password = True
        db.session.add(u)
        log("user_create", "user", None, detail=f"{username} ({u.role}, edit={u.can_edit})")
        db.session.commit()
        if u.role == "customer":
            flash(f"Login created: username '{username}'. The user sets their "
                  "own password at first sign-in.", "success")
            return redirect(url_for("admin.users",
                                    tab="distributors" if kind == "distributor" else "customers"))
        flash("User created.", "success")
        return redirect(url_for("admin.users"))
    return form()


@bp.route("/users/<int:user_id>", methods=["GET", "POST"])
def user_edit(user_id):
    u = db.session.get(User, user_id)
    if u is None:
        abort(404)
    customers = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    if request.method == "POST":
        old = f"role={u.role}, edit={u.can_edit}, active={u.is_active}"
        u.full_name = (request.form.get("full_name") or u.full_name).strip()
        u.email = request.form.get("email")
        u.role = request.form.get("role", u.role)
        u.can_edit = bool(request.form.get("can_edit"))
        u.is_active = bool(request.form.get("is_active"))
        link_cid = request.form.get("link_customer_id")
        u.customer_id = int(link_cid) if u.role == "customer" and link_cid else None
        mgr = request.form.get("manager_id")
        u.manager_id = int(mgr) if (u.role == "rep" and mgr) else None
        new_pw = request.form.get("password")
        if new_pw:
            if len(new_pw) < 8:
                flash("Password must be at least 8 characters.", "danger")
                return render_template("admin/user_edit.html", user=u, customers=customers, managers=_sales_managers())
            u.password_hash = hash_password(new_pw)
        cust_ids = request.form.getlist("customers")
        u.assigned_customers = db.session.scalars(
            db.select(Customer).filter(Customer.id.in_(cust_ids))).all() if cust_ids else []
        out_ids = request.form.getlist("portal_customers")
        u.portal_customers = db.session.scalars(
            db.select(Customer).filter(Customer.id.in_(out_ids))).all() \
            if (u.role == "customer" and out_ids) else []
        log("rights_change", "user", u.id, old_value=old,
            new_value=f"role={u.role}, edit={u.can_edit}, active={u.is_active}")
        db.session.commit()
        flash("User updated.", "success")
        return redirect(url_for("admin.users"))
    return render_template("admin/user_edit.html", user=u, customers=customers, managers=_sales_managers())


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
@bp.route("/permissions", methods=["GET", "POST"])
def permissions():
    from services import permissions as perms
    if request.method == "POST":
        perms.save_matrix(request.form)
        log("permissions_change", "setting", None, detail="role permissions updated")
        flash("Permissions saved. They take effect immediately.", "success")
        return redirect(url_for("admin.permissions"))
    return render_template("admin/permissions.html", matrix=perms.current_matrix(),
                           caps=perms.CAPS, reports=perms.REPORTS, roles=perms.ROLES)


def _detach_user_references(user_id):
    """Null every nullable foreign key that points at this user so the row can
    be deleted with foreign_keys=ON (QA audit C1, 3 Jul 2026).

    History rows keep their meaning: audit_log stores the username string next
    to the id, and documents keep their own numbers and dates. Association
    tables (customer_reps, portal_customer_link) already cascade. rep_target
    rows belong to the rep alone, so they are deleted with the rep. Discovered
    dynamically from the model metadata so a new user reference added later is
    handled without editing this function."""
    from sqlalchemy import update, delete as sa_delete
    for table in db.metadata.tables.values():
        for col in table.columns:
            for fk in col.foreign_keys:
                if fk.column.table.name != "user":
                    continue
                if fk.ondelete == "CASCADE":
                    break  # association rows go with the user
                if col.nullable:
                    db.session.execute(
                        update(table).where(col == user_id).values({col.name: None}))
                elif table.name == "rep_target":
                    db.session.execute(sa_delete(table).where(col == user_id))
                # A future NOT NULL reference without ondelete falls through to
                # the IntegrityError handler in user_delete, which rolls back.


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
def user_delete(user_id):
    u = db.session.get(User, user_id)
    if u is None:
        abort(404)
    if u.id == current_user.id:
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for("admin.user_edit", user_id=u.id))
    if u.role == "admin":
        admins = db.session.scalar(db.select(db.func.count(User.id)).filter_by(role="admin"))
        if admins <= 1:
            flash("Cannot delete the only administrator.", "danger")
            return redirect(url_for("admin.user_edit", user_id=u.id))
    name = u.full_name or u.username
    try:
        _detach_user_references(u.id)
        u.assigned_customers = []
        db.session.delete(u)
        log("user_delete", "user", user_id, detail=f"deleted user {name}")
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("User delete failed for user id %s", user_id)
        flash(f"Could not delete {name}: other records still reference this "
              "account. Deactivate the account instead, or contact support.",
              "danger")
        return redirect(url_for("admin.user_edit", user_id=user_id))
    flash(f"User {name} deleted.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/categories", methods=["GET", "POST"])
def categories():
    cats = db.session.scalars(db.select(Category).order_by(Category.sort_order, Category.name)).all()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        parent_id = request.form.get("parent_id")
        if name:
            db.session.add(Category(name=name,
                                    parent_id=int(parent_id) if parent_id else None,
                                    sort_order=int(request.form.get("sort_order") or 0)))
            log("category_create", "category", None, detail=name)
            db.session.commit()
            flash("Category added.", "success")
        return redirect(url_for("admin.categories"))
    return render_template("admin/categories.html", cats=cats)


@bp.route("/categories/<int:cat_id>/delete", methods=["POST"])
def category_delete(cat_id):
    c = db.session.get(Category, cat_id)
    if c is None:
        abort(404)
    if c.products:
        flash("Cannot delete a category that still has products.", "danger")
    else:
        db.session.delete(c)
        log("category_delete", "category", cat_id, detail=c.name)
        db.session.commit()
        flash("Category deleted.", "success")
    return redirect(url_for("admin.categories"))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@bp.route("/customer-categories", methods=["GET", "POST"])
def customer_categories():
    from blueprints.customers import ensure_customer_categories
    ensure_customer_categories()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if name and not db.session.scalar(db.select(CustomerCategory).filter_by(name=name)):
            mx = db.session.scalar(db.select(db.func.max(CustomerCategory.sort_order))) or 0
            db.session.add(CustomerCategory(name=name, sort_order=mx + 1))
            log("customer_category_create", "customer_category", None, detail=name)
            db.session.commit()
            flash("Category added.", "success")
        return redirect(url_for("admin.customer_categories"))
    cats = db.session.scalars(
        db.select(CustomerCategory).order_by(CustomerCategory.sort_order, CustomerCategory.name)).all()
    counts = {c.id: db.session.scalar(db.select(db.func.count(Customer.id)).filter_by(category_id=c.id))
              for c in cats}
    return render_template("admin/customer_categories.html", cats=cats, counts=counts)


@bp.route("/customer-categories/<int:cat_id>/delete", methods=["POST"])
def customer_category_delete(cat_id):
    c = db.session.get(CustomerCategory, cat_id)
    if c is None:
        abort(404)
    in_use = db.session.scalar(db.select(db.func.count(Customer.id)).filter_by(category_id=c.id))
    if in_use:
        flash("Cannot delete — customers are still in this category.", "danger")
    else:
        db.session.delete(c)
        log("customer_category_delete", "customer_category", cat_id, detail=c.name)
        db.session.commit()
        flash("Category deleted.", "success")
    return redirect(url_for("admin.customer_categories"))


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        for key in ("company_name", "app_name", "vat_rate", "base_currency",
                    "usd_round", "ugx_round", "offer_validity_days"):
            if key in request.form:
                settings_svc.set_value(key, request.form.get(key))
        if "theme" in request.form:
            theme = request.form.get("theme")
            settings_svc.set_value("theme", theme if theme in ("classic", "artisanal") else "classic")
        # SLA targets entered as hours + minutes, stored as decimal hours.
        def _hm(prefix):
            try:
                h = int(request.form.get(prefix + "_h") or 0)
            except ValueError:
                h = 0
            try:
                m = int(request.form.get(prefix + "_m") or 0)
            except ValueError:
                m = 0
            return round(h + m / 60.0, 4)
        if "sla_dispatch_h" in request.form or "sla_dispatch_m" in request.form:
            settings_svc.set_value("sla_dispatch_hours", _hm("sla_dispatch"))
        if "sla_delivery_h" in request.form or "sla_delivery_m" in request.form:
            settings_svc.set_value("sla_delivery_hours", _hm("sla_delivery"))
        # optional login image upload
        file = request.files.get("login_image")
        if file and file.filename:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext in (".png", ".jpg", ".jpeg", ".webp"):
                folder = os.path.join(current_app.config["UPLOAD_DIR"], "branding")
                os.makedirs(folder, exist_ok=True)
                from werkzeug.utils import secure_filename
                name = f"login_{datetime.utcnow():%Y%m%d%H%M%S}{ext}"
                file.save(os.path.join(folder, secure_filename(name)))
                settings_svc.set_value("login_image", name)
            else:
                flash("Login image must be PNG, JPG or WEBP.", "danger")
        if request.form.get("clear_login_image"):
            settings_svc.set_value("login_image", "")
        log("settings_change", "setting", None, detail="global settings updated")
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings"))
    values = {k: settings_svc.get(k) for k in
              ("company_name", "app_name", "vat_rate", "base_currency", "usd_round",
               "ugx_round", "offer_validity_days")}
    values["login_image"] = settings_svc.get("login_image", "")
    values["theme"] = settings_svc.get("theme", "classic")
    for name in ("sla_dispatch", "sla_delivery"):
        total = settings_svc.get_float(name + "_hours", 0)
        values[name + "_h"] = int(total)
        values[name + "_m"] = int(round((total - int(total)) * 60))
    return render_template("admin/settings.html", values=values)


COMMS_KEYS = ("smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from",
              "smtp_reply_to",
              "sms_provider", "sms_api_key", "sms_username", "sms_sender")


@bp.route("/features", methods=["GET", "POST"])
def features():
    from services import features as feat
    if request.method == "POST":
        for name, _l, _d in feat.FEATURES:
            feat.set_feature(name, request.form.get(f"feat:{name}") == "on")
        for key in COMMS_KEYS:
            if key in request.form:
                settings_svc.set_value(key, request.form.get(key))
        log("features_change", "setting", None, detail="feature flags / comms updated")
        db.session.commit()
        flash("Features saved.", "success")
        return redirect(url_for("admin.features"))
    flags = feat.all_features()
    comms = {k: settings_svc.get(k, "") for k in COMMS_KEYS}
    return render_template("admin/features.html", features=feat.FEATURES,
                           flags=flags, comms=comms)


@bp.route("/theme-preview")
def theme_preview():
    """Showcase of the artisanal look. Forces the artisanal stylesheet on so an
    admin can preview it without switching the live theme for everyone."""
    return render_template("admin/theme_preview.html", theme="artisanal",
                           current_theme=settings_svc.get("theme", "classic"))


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
@bp.route("/audit")
def audit():
    action = request.args.get("action", "")
    user_id = request.args.get("user", "")
    stmt = db.select(AuditLog).order_by(AuditLog.ts.desc())
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if user_id:
        stmt = stmt.where(AuditLog.user_id == int(user_id))
    entries = db.session.scalars(stmt.limit(500)).all()
    actions = db.session.scalars(db.select(AuditLog.action).distinct()).all()
    users = db.session.scalars(db.select(User).order_by(User.full_name)).all()
    return render_template("admin/audit.html", entries=entries, actions=actions,
                           users=users, sel_action=action, sel_user=user_id)


@bp.route("/audit/export.csv")
def audit_export():
    entries = db.session.scalars(db.select(AuditLog).order_by(AuditLog.ts.desc())).all()
    rows = ["timestamp,user,action,entity_type,entity_id,field,old_value,new_value,detail"]
    for e in entries:
        def esc(v):
            v = "" if v is None else str(v)
            return '"' + v.replace('"', '""') + '"'
        rows.append(",".join(esc(x) for x in
                    [e.ts, e.username, e.action, e.entity_type, e.entity_id,
                     e.field, e.old_value, e.new_value, e.detail]))
    return Response("\n".join(rows), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=audit_log.csv"})


@bp.route("/import-reports")
def import_reports():
    reports = db.session.scalars(
        db.select(ImportReport).order_by(ImportReport.ts.desc())).all()
    return render_template("admin/import_reports.html", reports=reports)


# ---------------------------------------------------------------------------
# Daily sales / financial data import — consolidated 27 Jul 2026 into the
# finance "Daily uploads" screen (customers.finance_uploads), which
# auto-detects every export this screen handled (invoices, credit notes,
# itemized, product pivot) plus payments and the receivables snapshot.
# The endpoint stays as a redirect so old links keep working.
# ---------------------------------------------------------------------------
@bp.route("/sales-import", methods=["GET", "POST"])
def sales_import():
    return redirect(url_for("customers.finance_uploads"))


# ---------------------------------------------------------------------------
# Upload & map a new pricelist (no code changes needed)
# ---------------------------------------------------------------------------
@bp.route("/upload", methods=["GET", "POST"])
def upload():
    import importer
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename.lower().endswith((".xlsx", ".xlsm")):
            flash("Please choose an .xlsx file.", "danger")
            return redirect(url_for("admin.upload"))
        os.makedirs(current_app.config["UPLOAD_DIR"], exist_ok=True)
        from werkzeug.utils import secure_filename
        safe_name = secure_filename(file.filename)
        path = os.path.join(current_app.config["UPLOAD_DIR"],
                            f"{datetime.utcnow():%Y%m%d%H%M%S}_{safe_name}")
        file.save(path)
        session["upload_path"] = path
        sheets = importer.list_sheets(path)
        return render_template("admin/upload_pick.html", path=path,
                               filename=file.filename, sheets=sheets)
    return render_template("admin/upload.html")


@bp.route("/upload/preview", methods=["POST"])
def upload_preview():
    import importer
    path = session.get("upload_path")
    sheet = request.form.get("sheet")
    if not path or not os.path.exists(path) or not sheet:
        flash("Upload session expired. Please upload again.", "warning")
        return redirect(url_for("admin.upload"))
    preview = importer.preview_sheet(path, sheet, max_rows=15)
    ncols = preview["ncols"]
    col_letters = [(i, importer.col_letter(i)) for i in range(ncols)]
    customers = db.session.scalars(db.select(Customer).order_by(Customer.name)).all()
    return render_template("admin/upload_map.html", path=path, sheet=sheet,
                           preview=preview, col_letters=col_letters,
                           customers=customers)


@bp.route("/upload/commit", methods=["POST"])
def upload_commit():
    import importer
    path = session.get("upload_path")
    sheet = request.form.get("sheet")
    if not path or not os.path.exists(path):
        flash("Upload session expired. Please upload again.", "warning")
        return redirect(url_for("admin.upload"))

    def col(name):
        v = request.form.get(name, "")
        return int(v) if v not in ("", "none", None) else None

    tier_cols, tier_labels = [], []
    for i in range(8):
        c = request.form.get(f"tier_col_{i}", "")
        lbl = request.form.get(f"tier_label_{i}", "").strip()
        if c not in ("", "none") and lbl:
            tier_cols.append(int(c))
            tier_labels.append(lbl)

    mapping = {
        "header_row": int(request.form.get("header_row", "1")),
        "data_start": int(request.form.get("data_start", "2")),
        "art_col": col("art_col"),
        "barcode_col": col("barcode_col"),
        "desc_col": col("desc_col"),
        "pack_col": col("pack_col"),
        "box_small_col": col("box_small_col"),
        "box_medium_col": col("box_medium_col"),
        "box_large_col": col("box_large_col"),
        "tier_cols": tier_cols,
        "tier_labels": tier_labels,
    }
    meta = {
        "name": (request.form.get("name") or sheet).strip(),
        "channel": request.form.get("channel", "mixed"),
        "market": request.form.get("market", "local"),
        "currency": request.form.get("currency", "UGX"),
        "vat_applicable": request.form.get("market", "local") == "local",
        "is_customer": request.form.get("is_customer") == "1",
        "customer_id": request.form.get("customer_id") or None,
    }
    try:
        pl, report = importer.import_mapped_sheet(path, sheet, mapping, meta,
                                                  source_label=os.path.basename(path))
        db.session.commit()
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        flash(f"Import failed: {e}", "danger")
        return redirect(url_for("admin.upload"))

    log("import", "pricelist", pl.id,
        detail=f"uploaded '{pl.name}' ({report['ok']} rows, {report['failed']} failed)",
        commit=True)
    flash(f"Imported '{pl.name}': {report['ok']} rows, {report['failed']} skipped. "
          f"See import reports for detail.", "success")
    if pl.is_customer:
        return redirect(url_for("customer_pricelists.detail", list_id=pl.id))
    return redirect(url_for("pricelists.detail", list_id=pl.id))
