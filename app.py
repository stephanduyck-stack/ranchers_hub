"""Ranchers Finest pricing application — Flask entry point.

Run (development):   flask --app app run
Run (production):    gunicorn app:app
"""
import os
from datetime import datetime, timedelta

from flask import Flask, redirect, url_for, render_template, session, request, jsonify
from flask_login import current_user

from config import Config
from extensions import db, login_manager, csrf
from services import settings as settings_svc
from services.pricing import format_money


def create_app(config_object=Config):
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(config_object)

    # SECRET_KEY safety. In production a missing or placeholder key lets anyone
    # forge session cookies, so fail hard. In debug/dev, generate a throwaway
    # random key so the app stays runnable without setting an env var.
    _PLACEHOLDER = "change-this-secret-key-in-production"
    _key = app.config.get("SECRET_KEY")
    if not _key or _key == _PLACEHOLDER:
        if app.debug or os.environ.get("FLASK_ENV") == "development" \
                or os.environ.get("FLASK_DEBUG") == "1":
            app.config["SECRET_KEY"] = os.urandom(32)
            app.logger.warning(
                "SECRET_KEY not set; using a random development key. "
                "Sessions will not survive a restart. Set SECRET_KEY for production.")
        else:
            raise RuntimeError(
                "SECRET_KEY is not set (or is the committed placeholder). "
                "Set a strong SECRET_KEY environment variable before starting "
                "in production.")

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    import models  # noqa: F401  (register models with SQLAlchemy)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(models.User, int(user_id))

    with app.app_context():
        _ensure_runtime_schema()

    register_blueprints(app)
    register_template_helpers(app)
    register_session_guard(app)
    register_portal_guard(app)
    register_errorhandlers(app)

    # Message notification sweep: emails customers whose portal messages sit
    # unread past the delay (services/notify.py). Piggybacks on traffic, at
    # most once a minute; notify_sweep.py covers quiet hours via cron.
    from services import notify
    notify.register_sweep(app)

    @app.after_request
    def set_security_headers(resp):
        # Stop browsers guessing content types on served files, in particular
        # user uploads (LPO, POD, promo and branding images). QA audit L1.
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        return resp

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            if getattr(current_user, "is_customer_user", False):
                return redirect(url_for("portal.home"))
            if getattr(current_user, "is_driver", False):
                return redirect(url_for("driver.home"))
            return redirect(url_for("dashboard.home"))
        return redirect(url_for("auth.login"))

    return app


def _ensure_runtime_schema():
    """Lightweight auto-migration: add columns introduced after a DB was first
    created, so existing installs upgrade in place without a manual migration."""
    try:
        _run_migrations()
    except Exception:
        # Migrations are best-effort, but log the failure so a locked DB or
        # bad ALTER does not disappear silently (it used to swallow create_all
        # too, then the app served 500s on missing columns with no log line).
        from flask import current_app
        current_app.logger.exception("Runtime schema migration failed")

    # Create any tables added in later versions (e.g. sales orders, prod_*,
    # acc_*). create_all only creates missing tables; it never alters existing
    # ones. Kept OUTSIDE the swallow above so a genuine table-creation failure
    # raises. bind_key=None targets ONLY the default database: the 'costing'
    # bind is a read-only mirror whose tables must never be created here, and
    # the app must boot even when that file is absent.
    db.create_all(bind_key=None)

    # Seed/refresh the chart of accounts (idempotent: inserts missing codes,
    # never edits or deletes existing accounts).
    from services import coa
    coa.seed_chart()
    # Seed the stock locations (plant + own shops). Idempotent by name.
    from services import shop_ops
    shop_ops.ensure_locations()

    # Install the accounting immutability triggers and verify them.
    # QA audit 5 Jul 2026 C1: the trigger SQL previously lived only in the
    # already-migrated database; a fresh install, restore, or clone booted
    # with ZERO of them and the append-only ledger was enforced by nothing.
    _install_acc_triggers()

    # Hub domain cutover (3 Aug 2026): runs after create_all so it also
    # applies on a fresh database.
    _set_hub_domain_settings()


def _set_hub_domain_settings():
    """One-time cutover (Stephan, 3 Aug 2026): the app now lives at
    https://hub.ranchersfinest.net and all outbound email sends from
    thehub@ranchersfinest.net. Sets app_base_url and smtp_from once,
    keyed on a marker setting, so a later manual edit on Admin > Features
    is never overwritten on boot."""
    from models import Setting
    try:
        if db.session.get(Setting, "hub_domain_cutover_2026_08") is not None:
            return
        settings_svc.set_value("app_base_url", "https://hub.ranchersfinest.net")
        settings_svc.set_value("smtp_from", "thehub@ranchersfinest.net")
        settings_svc.set_value("hub_domain_cutover_2026_08", "done")
        db.session.commit()
        from flask import current_app
        current_app.logger.warning(
            "Hub domain cutover applied: app_base_url="
            "https://hub.ranchersfinest.net, smtp_from=thehub@ranchersfinest.net")
    except Exception:
        db.session.rollback()
        from flask import current_app
        current_app.logger.exception(
            "Hub domain cutover skipped (will retry next boot)")


# The 25 database triggers that make posted accounting rows append-only.
# Defined across migrations/acc_001..006. The boot self-check refuses to
# serve accounting when any of these is missing.
EXPECTED_ACC_TRIGGERS = frozenset({
    # acc_001 — ledger
    "acc_line_no_update", "acc_line_no_delete", "acc_line_no_insert_posted",
    "acc_line_shape", "acc_entry_post_check", "acc_entry_no_edit_posted",
    "acc_entry_no_delete",
    # acc_002/003 — valued inventory
    "acc_inv_mv_no_update", "acc_inv_mv_no_delete",
    # acc_003 — fiscal invoices
    "acc_invoice_freeze_money", "acc_invoice_no_delete",
    "acc_invoice_line_no_update", "acc_invoice_line_no_delete",
    # acc_004 — purchases
    "acc_purchase_freeze_money", "acc_purchase_no_delete",
    "acc_purchase_line_no_update", "acc_purchase_line_no_delete",
    # acc_005 — cash and bank
    "acc_receipt_freeze", "acc_receipt_no_delete",
    "acc_payment_freeze", "acc_payment_no_delete",
    # acc_006 — own shops
    "acc_transfer_no_delete", "acc_transfer_line_no_update",
    "acc_shop_sale_freeze", "acc_shop_sale_no_delete",
})


def _install_acc_triggers():
    """Apply the accounting integrity triggers at every boot (idempotent),
    then verify. Two dialects carry the layer: SQLite (migrations/acc_0*.sql,
    the original semantics) and PostgreSQL (migrations/pg/acc_pg_triggers.sql,
    the PL/pgSQL port of 12 July 2026). Any other backend has no integrity
    layer, so accounting is refused rather than served without its
    append-only guarantee. ACC_DB_INTEGRITY gates the accounting blueprint's
    before_request.
    """
    from flask import current_app
    import glob as _glob
    from sqlalchemy import text

    dialect = db.engine.dialect.name
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")

    if dialect == "sqlite":
        files = sorted(_glob.glob(os.path.join(base, "acc_0*.sql")))
        if not files:
            raise RuntimeError(
                "migrations/acc_0*.sql not found; cannot install the accounting "
                "integrity triggers. Refusing to start without them.")
        # executescript handles the BEGIN..END trigger bodies that a naive
        # split-on-semicolon would mangle. The files are idempotent
        # (IF NOT EXISTS / DROP-then-CREATE), so this is safe on every boot.
        raw = db.engine.raw_connection()
        try:
            cur = raw.cursor()
            for path in files:
                with open(path, encoding="utf-8") as fh:
                    cur.executescript(fh.read())
            raw.commit()
        finally:
            raw.close()
        have = {r[0] for r in db.session.execute(text(
            "SELECT name FROM sqlite_master WHERE type='trigger'"))}
    elif dialect == "postgresql":
        path = os.path.join(base, "pg", "acc_pg_triggers.sql")
        if not os.path.exists(path):
            raise RuntimeError(
                "migrations/pg/acc_pg_triggers.sql not found; cannot install "
                "the accounting integrity triggers. Refusing to start without "
                "them.")
        with open(path, encoding="utf-8") as fh:
            script = fh.read()
        # Raw cursor with NO parameters: the script contains literal '%'
        # (RAISE EXCEPTION '%'), which psycopg2 would treat as a placeholder
        # if any parameter object were passed. Dollar-quoted bodies survive.
        raw = db.engine.raw_connection()
        try:
            cur = raw.cursor()
            cur.execute(script)
            raw.commit()
        finally:
            raw.close()
        have = {r[0] for r in db.session.execute(text(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))}
    else:
        current_app.config["ACC_DB_INTEGRITY"] = False
        current_app.logger.error(
            "Accounting integrity triggers exist for SQLite and PostgreSQL "
            "only, and this database is %s. Accounting routes are DISABLED "
            "until the triggers are ported to this backend.", dialect)
        return

    missing = EXPECTED_ACC_TRIGGERS - have
    if missing:
        raise RuntimeError(
            "Accounting integrity self-check FAILED. Missing triggers: "
            + ", ".join(sorted(missing))
            + ". Refusing to start: the ledger would not be append-only.")
    current_app.config["ACC_DB_INTEGRITY"] = True


class _DialectConn:
    """Adapter for the migration ladder below: the ALTER statements are
    written in SQLite flavour; on PostgreSQL this rewrites the three
    incompatibilities ("user" is reserved and needs quoting, booleans take
    FALSE not 0, and the DATETIME type is spelled TIMESTAMP)."""

    def __init__(self, conn):
        self._conn = conn
        self._pg = db.engine.dialect.name == "postgresql"

    def execute(self, clause):
        from sqlalchemy import text as _text
        if self._pg:
            import re as _re
            s = str(clause)
            s = s.replace("ALTER TABLE user ", 'ALTER TABLE "user" ')
            s = s.replace("BOOLEAN NOT NULL DEFAULT 0",
                          "BOOLEAN NOT NULL DEFAULT FALSE")
            s = s.replace("BOOLEAN DEFAULT 0", "BOOLEAN DEFAULT FALSE")
            s = s.replace("BOOLEAN NOT NULL DEFAULT 1",
                          "BOOLEAN NOT NULL DEFAULT TRUE")
            s = s.replace("BOOLEAN DEFAULT 1", "BOOLEAN DEFAULT TRUE")
            s = _re.sub(r"\bDATETIME\b", "TIMESTAMP", s)
            clause = _text(s)
        return self._conn.execute(clause)


def _run_migrations():
    """Add columns introduced after a DB was first created (in-place upgrade)."""
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    if "pricelist" not in insp.get_table_names():
        return  # fresh DB; create_all (in caller) builds the current schema
    cols = {c["name"] for c in insp.get_columns("pricelist")}
    with db.engine.begin() as _raw_conn:
            conn = _DialectConn(_raw_conn)
            if "archived" not in cols:
                conn.execute(text(
                    "ALTER TABLE pricelist ADD COLUMN archived BOOLEAN DEFAULT 0"))
            if "group_name" not in cols:
                conn.execute(text(
                    "ALTER TABLE pricelist ADD COLUMN group_name VARCHAR(96)"))
            if "sales_order_line" in insp.get_table_names():
                lcols = {c["name"] for c in insp.get_columns("sales_order_line")}
                if "availability" not in lcols:
                    conn.execute(text("ALTER TABLE sales_order_line ADD COLUMN "
                                      "availability VARCHAR(16) DEFAULT 'available'"))
                if "fulfilled_qty" not in lcols:
                    conn.execute(text("ALTER TABLE sales_order_line ADD COLUMN "
                                      "fulfilled_qty FLOAT"))
                if "expected_restock" not in lcols:
                    conn.execute(text("ALTER TABLE sales_order_line ADD COLUMN "
                                      "expected_restock DATE"))
                if "customer_notified_at" not in lcols:
                    conn.execute(text("ALTER TABLE sales_order_line ADD COLUMN "
                                      "customer_notified_at DATETIME"))
            if "sales_order" in insp.get_table_names():
                ocols = {c["name"] for c in insp.get_columns("sales_order")}
                if "backorder_of_id" not in ocols:
                    conn.execute(text("ALTER TABLE sales_order ADD COLUMN "
                                      "backorder_of_id INTEGER"))
            if "offer" in insp.get_table_names():
                fcols = {c["name"] for c in insp.get_columns("offer")}
                if "converted_order_id" not in fcols:
                    conn.execute(text("ALTER TABLE offer ADD COLUMN "
                                      "converted_order_id INTEGER"))
            if "customer" in insp.get_table_names():
                ccols = {c["name"] for c in insp.get_columns("customer")}
                if "segment" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN "
                                      "segment VARCHAR(16) DEFAULT 'customer'"))
                if "category_id" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN category_id INTEGER"))
                if "area" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN area VARCHAR(128)"))
                if "payment_terms" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN payment_terms VARCHAR(64)"))
                if "address" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN address TEXT"))
                if "latitude" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN latitude FLOAT"))
                if "longitude" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN longitude FLOAT"))
                if "onboarding_status" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN onboarding_status VARCHAR(12) DEFAULT 'approved'"))
                if "proposed_payment_terms" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN proposed_payment_terms VARCHAR(64)"))
                if "credit_approved" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN credit_approved BOOLEAN DEFAULT 0"))
                if "created_by_id" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN created_by_id INTEGER"))
                if "account_status" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN account_status VARCHAR(12) DEFAULT 'ok'"))
                if "account_note" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN account_note VARCHAR(255)"))
                if "archived" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN archived BOOLEAN DEFAULT 0"))
                for _col, _type in (
                    ("procurement_name", "VARCHAR(128)"), ("procurement_phone", "VARCHAR(64)"),
                    ("procurement_email", "VARCHAR(128)"), ("chef_name", "VARCHAR(128)"),
                    ("chef_phone", "VARCHAR(64)"), ("chef_email", "VARCHAR(128)"),
                    ("other_contact_name", "VARCHAR(128)"), ("other_contact_phone", "VARCHAR(64)"),
                    ("other_contact_email", "VARCHAR(128)"), ("tax_id", "VARCHAR(40)"),
                    ("delivery_days", "VARCHAR(64)"),
                    ("delivery_time_from", "VARCHAR(8)"), ("delivery_time_to", "VARCHAR(8)"),
                    ("delivery_notes", "VARCHAR(255)")):
                    if _col not in ccols:
                        conn.execute(text(f"ALTER TABLE customer ADD COLUMN {_col} {_type}"))
                # Finance credit clearance (20 Jul 2026). On first migration,
                # accounts currently 'ok' start cleared so orders keep
                # flowing; on-hold/blocked accounts start uncleared.
                if "credit_cleared" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN credit_cleared BOOLEAN DEFAULT 0"))
                    conn.execute(text("ALTER TABLE customer ADD COLUMN credit_cleared_by_id INTEGER"))
                    conn.execute(text("ALTER TABLE customer ADD COLUMN credit_cleared_at DATETIME"))
                    conn.execute(text("UPDATE customer SET credit_cleared=1 WHERE account_status='ok'"))
                # Back-order preference (21 Jul 2026): existing customers keep
                # today's behaviour (back orders raised) until edited.
                if "backorders_allowed" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN backorders_allowed BOOLEAN DEFAULT 1"))
                    conn.execute(text("UPDATE customer SET backorders_allowed=1"))
                # Credit limit engine (21 Jul 2026): no limit set anywhere at
                # first — finance arms customers one by one.
                if "credit_limit_ugx" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN credit_limit_ugx INTEGER"))
                if "credit_days" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN credit_days INTEGER"))
                if "odoo_receivable" not in ccols:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN odoo_receivable NUMERIC(18,2)"))
                    conn.execute(text("ALTER TABLE customer ADD COLUMN odoo_receivable_at DATE"))
            if "sales_order" in insp.get_table_names():
                socols2 = {c["name"] for c in insp.get_columns("sales_order")}
                if "credit_override_by_id" not in socols2:
                    conn.execute(text("ALTER TABLE sales_order ADD COLUMN credit_override_by_id INTEGER"))
                    conn.execute(text("ALTER TABLE sales_order ADD COLUMN credit_override_at DATETIME"))
                if "momo_ref" not in socols2:
                    conn.execute(text("ALTER TABLE sales_order ADD COLUMN momo_ref VARCHAR(40)"))
                    conn.execute(text("ALTER TABLE sales_order ADD COLUMN momo_ref_at DATETIME"))
            if "sales_order_line" in insp.get_table_names():
                solcols = {c["name"] for c in insp.get_columns("sales_order_line")}
                if "tax_category" not in solcols:
                    conn.execute(text("ALTER TABLE sales_order_line ADD COLUMN tax_category VARCHAR(8)"))
                if "accepted_qty" not in solcols:
                    conn.execute(text("ALTER TABLE sales_order_line ADD COLUMN accepted_qty FLOAT"))
                if "variance_reason" not in solcols:
                    conn.execute(text("ALTER TABLE sales_order_line ADD COLUMN variance_reason VARCHAR(24)"))
            if "pricelist" in insp.get_table_names():
                pcols2 = {c["name"] for c in insp.get_columns("pricelist")}
                if "allow_small_orders" not in pcols2:
                    conn.execute(text("ALTER TABLE pricelist ADD COLUMN allow_small_orders BOOLEAN DEFAULT 0"))
                    conn.execute(text("UPDATE pricelist SET allow_small_orders=1 "
                                      "WHERE name LIKE '%Meat Supermarkets%' OR name LIKE '%Sokoni%'"))
            if "line_price" in insp.get_table_names():
                lpcols = {c["name"] for c in insp.get_columns("line_price")}
                if "pending_amount" not in lpcols:
                    conn.execute(text("ALTER TABLE line_price ADD COLUMN pending_amount NUMERIC(16,4)"))
            if "pricelist" in insp.get_table_names():
                plcols = {c["name"] for c in insp.get_columns("pricelist")}
                if "approval_status" not in plcols:
                    conn.execute(text("ALTER TABLE pricelist ADD COLUMN approval_status VARCHAR(12) DEFAULT 'approved'"))
            if "price_approval" in insp.get_table_names():
                pacols = {c["name"] for c in insp.get_columns("price_approval")}
                if "promo_id" not in pacols:
                    conn.execute(text("ALTER TABLE price_approval ADD COLUMN promo_id INTEGER"))
            if "user" in insp.get_table_names():
                ucols = {c["name"] for c in insp.get_columns("user")}
                if "customer_id" not in ucols:
                    conn.execute(text("ALTER TABLE user ADD COLUMN customer_id INTEGER"))
                if "manager_id" not in ucols:
                    conn.execute(text("ALTER TABLE user ADD COLUMN manager_id INTEGER"))
                if "must_change_password" not in ucols:
                    conn.execute(text("ALTER TABLE user ADD COLUMN "
                                      "must_change_password BOOLEAN NOT NULL DEFAULT 0"))
            if "message" in insp.get_table_names():
                mcols = {c["name"] for c in insp.get_columns("message")}
                if "emailed_at" not in mcols:
                    conn.execute(text("ALTER TABLE message ADD COLUMN emailed_at DATETIME"))
            if "sales_order" in insp.get_table_names():
                ocols2 = {c["name"] for c in insp.get_columns("sales_order")}
                if "lpo_filename" not in ocols2:
                    conn.execute(text("ALTER TABLE sales_order ADD COLUMN lpo_filename VARCHAR(255)"))
                if "submitted_at" not in ocols2:
                    conn.execute(text("ALTER TABLE sales_order ADD COLUMN submitted_at DATETIME"))
                if "dispatched_at" not in ocols2:
                    conn.execute(text("ALTER TABLE sales_order ADD COLUMN dispatched_at DATETIME"))
                if "fulfilment_started_at" not in ocols2:
                    conn.execute(text("ALTER TABLE sales_order ADD COLUMN fulfilment_started_at DATETIME"))
                for col, ddl in [
                    ("delivery_time", "delivery_time VARCHAR(8)"),
                    # Invoice-after-delivery columns (31 Jul build): present
                    # in models but missing from this ladder until 2 Aug —
                    # a fresh or production database crashed on any
                    # SalesOrder query without them.
                    ("accepted_recorded_at", "accepted_recorded_at DATETIME"),
                    ("accepted_recorded_by_id", "accepted_recorded_by_id INTEGER"),
                    ("dnote_filing_no", "dnote_filing_no VARCHAR(20)"),
                    ("dnote_filed_at", "dnote_filed_at DATETIME"),
                    ("dnote_filed_by_id", "dnote_filed_by_id INTEGER"),
                    ("accepted_at", "accepted_at DATETIME"),
                    ("accepted_by_id", "accepted_by_id INTEGER"),
                    ("credit_checked", "credit_checked BOOLEAN DEFAULT 0"),
                    ("delivered_at", "delivered_at DATETIME"),
                    ("dnote_number", "dnote_number VARCHAR(32)"),
                    ("dnote_at", "dnote_at DATETIME"),
                    ("assigned_driver_id", "assigned_driver_id INTEGER"),
                    ("assigned_at", "assigned_at DATETIME"),
                    ("driver_accepted_at", "driver_accepted_at DATETIME"),
                    ("pod_filename", "pod_filename VARCHAR(255)"),
                    ("bo_confirm_state", "bo_confirm_state VARCHAR(12)"),
                    ("stock_deducted", "stock_deducted BOOLEAN DEFAULT 0"),
                    ("rating", "rating INTEGER"),
                    ("rating_comment", "rating_comment TEXT"),
                    ("rated_at", "rated_at DATETIME"),
                    ("feedback_ack", "feedback_ack BOOLEAN DEFAULT 0"),
                    ("feedback_ack_at", "feedback_ack_at DATETIME"),
                ]:
                    if col not in ocols2:
                        conn.execute(text(f"ALTER TABLE sales_order ADD COLUMN {ddl}"))
            if "activity" in insp.get_table_names():
                acols = {c["name"] for c in insp.get_columns("activity")}
                if "recording_url" not in acols:
                    conn.execute(text("ALTER TABLE activity ADD COLUMN recording_url VARCHAR(500)"))
            if "message" in insp.get_table_names():
                mcols = {c["name"] for c in insp.get_columns("message")}
                if "order_id" not in mcols:
                    conn.execute(text("ALTER TABLE message ADD COLUMN order_id INTEGER"))
            if "sales_history" in insp.get_table_names():
                shcols = {c["name"] for c in insp.get_columns("sales_history")}
                if "month" not in shcols:
                    conn.execute(text("ALTER TABLE sales_history ADD COLUMN month INTEGER"))
                if "product_id" not in shcols:
                    conn.execute(text("ALTER TABLE sales_history ADD COLUMN product_id INTEGER"))
            if "product" in insp.get_table_names():
                pcols = {c["name"] for c in insp.get_columns("product")}
                if "vat_applicable" not in pcols:
                    conn.execute(text("ALTER TABLE product ADD COLUMN vat_applicable BOOLEAN DEFAULT 0"))
                    # First time only: processed categories carry VAT.
                    conn.execute(text(
                        "UPDATE product SET vat_applicable=1 WHERE category_id IN ("
                        " SELECT c.id FROM category c LEFT JOIN category p ON c.parent_id=p.id"
                        " WHERE upper(COALESCE(p.name,c.name)) IN "
                        " ('SAUSAGES','COLD MEATS','HOTDOGS & VIENNAS','BACON','BURGERS','BETAR'))"))
                if "stock_on_hand" not in pcols:
                    conn.execute(text("ALTER TABLE product ADD COLUMN stock_on_hand FLOAT DEFAULT 0"))
                if "low_stock_level" not in pcols:
                    conn.execute(text("ALTER TABLE product ADD COLUMN low_stock_level FLOAT DEFAULT 0"))
                # Cost floor (QA audit 5 Jul 2026): UGX cost per product unit.
                if "unit_cost" not in pcols:
                    conn.execute(text("ALTER TABLE product ADD COLUMN unit_cost NUMERIC(16,4)"))
            # (QA audit 5 Jul 2026 M3: a second sales_order inspection block
            # duplicating stock_deducted was removed; the column is covered by
            # the ALTER list above.)
            # Accounting Phase 5: receipts track how much of each invoice is
            # settled. Added here (not create_all) because the table predates
            # the column on already-migrated databases.
            if "acc_invoice" in insp.get_table_names():
                icols = {c["name"] for c in insp.get_columns("acc_invoice")}
                if "paid_minor" not in icols:
                    conn.execute(text("ALTER TABLE acc_invoice ADD COLUMN "
                                      "paid_minor INTEGER NOT NULL DEFAULT 0"))
            # Accounting Phase 7: own-shop flag on customers.
            if "customer" in insp.get_table_names():
                c7 = {c["name"] for c in insp.get_columns("customer")}
                if "internal_location_id" not in c7:
                    conn.execute(text("ALTER TABLE customer ADD COLUMN "
                                      "internal_location_id INTEGER"))
            # Batch/lot traceability (QA audit 5 Jul 2026).
            if "stock_movement" in insp.get_table_names():
                smc = {c["name"] for c in insp.get_columns("stock_movement")}
                if "lot_number" not in smc:
                    conn.execute(text("ALTER TABLE stock_movement ADD COLUMN lot_number VARCHAR(64)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_stock_movement_lot_number "
                                      "ON stock_movement (lot_number)"))
                if "expiry" not in smc:
                    conn.execute(text("ALTER TABLE stock_movement ADD COLUMN expiry DATE"))
            if "prod_production" in insp.get_table_names():
                ppc = {c["name"] for c in insp.get_columns("prod_production")}
                if "lot_number" not in ppc:
                    conn.execute(text("ALTER TABLE prod_production ADD COLUMN lot_number VARCHAR(64)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_prod_production_lot_number "
                                      "ON prod_production (lot_number)"))
                if "expiry" not in ppc:
                    conn.execute(text("ALTER TABLE prod_production ADD COLUMN expiry DATE"))
    _cleanup_orphan_user_refs()
    _cleanup_datekey_payments()
    _fix_fx_invoice_lines()


def _fix_fx_invoice_lines():
    """One-time repair (1 Aug 2026, found via SEULE LA GRACE): the itemized
    Odoo export carries line amounts in DOCUMENT currency, so USD export
    invoices imported before the importer converted them hold dollar lines
    under shilling headers. Detect by ratio: header untaxed / line sum
    between 100 and 10,000 is an fx factor (UGX/USD runs ~3,500-3,900);
    genuine invoices sit near 1. Scale each affected invoice's lines by the
    exact ratio so lines sum back to the UGX header. Idempotent: once
    scaled, the ratio is 1 and no boot touches them again."""
    from sqlalchemy import text
    try:
        cands = db.session.execute(text(
            "SELECT i.id, i.untaxed * 1.0 / SUM(l.amount) AS f "
            "FROM invoice i JOIN invoice_line l ON l.invoice_id = i.id "
            "WHERE i.untaxed IS NOT NULL AND i.untaxed > 0 "
            "GROUP BY i.id, i.untaxed "
            "HAVING SUM(l.amount) > 0 "
            "AND i.untaxed * 1.0 / SUM(l.amount) BETWEEN 100 AND 10000")).all()
        if not cands:
            return
        with db.engine.begin() as conn:
            for iid, f in cands:
                conn.execute(text(
                    "UPDATE invoice_line SET amount = amount * :f "
                    "WHERE invoice_id = :iid"), {"f": float(f), "iid": iid})
        from flask import current_app
        current_app.logger.warning(
            "Converted document-currency invoice lines to UGX on %s invoices",
            len(cands))
    except Exception:
        from flask import current_app
        current_app.logger.exception(
            "FX invoice-line repair skipped (will retry next boot)")


def _cleanup_datekey_payments():
    """One-time repair (29 Jul 2026): daily payment files exported without
    the payment Number column keyed each row on a date column, collapsing
    every payment of a day into one record. Those records carry a datetime
    string as their number (e.g. '2026-07-27 00:00:00'); most duplicate
    properly numbered payments loaded from the full-history export, the
    rest are restored by re-importing a correct export. Delete them.
    sales_import.import_payments now rejects files without the Number
    column, so no new ones appear. Check-then-write like the orphan
    cleanup above: once clean, no boot takes the write lock again."""
    from sqlalchemy import text
    try:
        pending = db.session.execute(text(
            "SELECT COUNT(*) FROM customer_payment "
            "WHERE number LIKE '%00:00:00%'")).scalar()
        if not pending:
            return
        with db.engine.begin() as conn:
            conn.execute(text(
                "DELETE FROM customer_payment WHERE number LIKE '%00:00:00%'"))
        from flask import current_app
        current_app.logger.warning(
            "Removed %s date-keyed payment records left by payment exports "
            "missing the Number column", pending)
    except Exception:
        from flask import current_app
        current_app.logger.exception(
            "Date-keyed payment cleanup skipped (will retry next boot)")


def _cleanup_orphan_user_refs():
    """QA audit M1 (3 Jul 2026): history rows written before foreign-key
    enforcement reference users deleted under the old regime. Null the dangling
    ids so foreign_key_check is clean; audit_log keeps the stored username
    string, so the trail stays readable.

    Check-then-write: the SELECT costs nothing, and once the data is clean no
    boot ever takes the write lock for this again. Runs in its own transaction
    with its own guard so a busy database (second app instance, dev server on
    the same file) cannot fail the ALTER ladder above."""
    from sqlalchemy import text
    try:
        pending = db.session.execute(text(
            "SELECT (SELECT COUNT(*) FROM audit_log WHERE user_id IS NOT NULL "
            "        AND user_id NOT IN (SELECT id FROM user)) + "
            "       (SELECT COUNT(*) FROM exchange_rate WHERE created_by IS NOT NULL "
            "        AND created_by NOT IN (SELECT id FROM user))")).scalar()
        if not pending:
            return
        with db.engine.begin() as conn:
            conn.execute(text(
                "UPDATE audit_log SET user_id=NULL WHERE user_id IS NOT NULL "
                "AND user_id NOT IN (SELECT id FROM user)"))
            conn.execute(text(
                "UPDATE exchange_rate SET created_by=NULL WHERE created_by IS NOT NULL "
                "AND created_by NOT IN (SELECT id FROM user)"))
    except Exception:
        from flask import current_app
        current_app.logger.exception(
            "Orphan user-reference cleanup skipped (will retry next boot)")


def register_blueprints(app):
    from blueprints.auth import bp as auth_bp
    from blueprints.dashboard import bp as dashboard_bp
    from blueprints.pricelists import bp as pricelists_bp
    from blueprints.customer_pricelists import bp as cpl_bp
    from blueprints.offers import bp as offers_bp
    from blueprints.orders import bp as orders_bp
    from blueprints.portal import bp as portal_bp
    from blueprints.promotions import bp as promotions_bp
    from blueprints.messages import bp as messages_bp
    from blueprints.customers import bp as customers_bp
    from blueprints.products import bp as products_bp
    from blueprints.reps import bp as reps_bp
    from blueprints.reports import bp as reports_bp
    from blueprints.exchange_rates import bp as rates_bp
    from blueprints.admin import bp as admin_bp
    from blueprints.crm import bp as crm_bp
    from blueprints.driver import bp as driver_bp
    from blueprints.stock import bp as stock_bp
    from blueprints.catalogue import bp as catalogue_bp
    from blueprints.targets import bp as targets_bp
    from blueprints.rep_reports import bp as rep_reports_bp
    from blueprints.approvals import bp as approvals_bp
    from blueprints.price_promos import bp as price_promos_bp
    from blueprints.production import bp as production_bp
    from blueprints.accounting import bp as accounting_bp
    from blueprints.api import bp as api_bp
    # Costing module (ported from the standalone meat-costing-app).
    from blueprints.summary import summary_bp
    from blueprints.ingredients import ingredients_bp
    from blueprints.cuts import cuts_bp
    from blueprints.spice_mixes import spice_bp
    from blueprints.recipes import recipes_bp
    from blueprints.pricing import pricing_bp
    from blueprints.overhead import overhead_bp
    from blueprints.packaging import packaging_bp
    from blueprints.whatif import whatif_bp
    from blueprints.settings import settings_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(pricelists_bp)
    app.register_blueprint(cpl_bp)
    app.register_blueprint(offers_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(portal_bp)
    app.register_blueprint(promotions_bp)
    app.register_blueprint(messages_bp)
    app.register_blueprint(customers_bp)
    app.register_blueprint(products_bp)
    app.register_blueprint(reps_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(rates_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(crm_bp)
    app.register_blueprint(driver_bp)
    app.register_blueprint(stock_bp)
    app.register_blueprint(catalogue_bp)
    app.register_blueprint(targets_bp)
    app.register_blueprint(rep_reports_bp)
    app.register_blueprint(approvals_bp)
    app.register_blueprint(price_promos_bp)
    app.register_blueprint(production_bp)
    app.register_blueprint(accounting_bp)
    for costing_bp in (summary_bp, ingredients_bp, cuts_bp, spice_bp,
                       recipes_bp, pricing_bp, overhead_bp, packaging_bp,
                       whatif_bp, settings_bp):
        app.register_blueprint(costing_bp)
    app.register_blueprint(api_bp)
    # The API authenticates by bearer token, not browser session, so CSRF
    # (a browser defence) does not apply and would otherwise block every call.
    csrf.exempt(api_bp)


def register_template_helpers(app):
    @app.template_filter("money")
    def _money(amount, ccy="UGX"):
        return format_money(amount, ccy)

    @app.context_processor
    def inject_globals():
        from services.permissions import has_perm, can_view_report
        ctx = {
            "company_name": settings_svc.get("company_name", "Ranchers Finest U Ltd"),
            "app_name": settings_svc.get("app_name", "Ranchers Finest Sales Hub"),
            "login_image": settings_svc.get("login_image", None),
            "theme": settings_svc.get("theme", "classic"),
            "features": __import__("services.features", fromlist=["all_features"]).all_features(),
            "now": datetime.utcnow(),
            "has_perm": has_perm,
            "can_view_report": can_view_report,
            "portal_unread": 0,
            "staff_unread": 0,
            "pending_onboarding": 0,
            "credit_alerts_open": 0,
            "driver_new": 0,
            "portal_outlets": [],
            "portal_active_id": None,
        }
        from services.timing import durations as _od, delay_status as _ds, humanize as _hh
        ctx["order_durations"] = _od
        ctx["order_delay"] = _ds
        ctx["humanize_hours"] = _hh
        try:
            if current_user.is_authenticated:
                from models import Message
                if getattr(current_user, "is_customer_user", False) and current_user.customer_id:
                    outs = current_user.portal_outlets
                    ctx["portal_outlets"] = outs
                    from flask import session as _sess
                    ids = {c.id for c in outs}
                    aid = _sess.get("portal_cust")
                    ctx["portal_active_id"] = aid if aid in ids else current_user.customer_id
                    ctx["portal_unread"] = db.session.scalar(
                        db.select(db.func.count(Message.id)).where(
                            Message.customer_id == ctx["portal_active_id"],
                            Message.sender_type == "staff",
                            Message.read_by_customer.is_(False))) or 0
                elif current_user.role in ("admin", "manager", "order_manager", "rep", "cfo"):
                    from blueprints.messages import staff_unread_count
                    ctx["staff_unread"] = staff_unread_count(current_user)
                if getattr(current_user, "is_driver", False):
                    from models import SalesOrder
                    ctx["driver_new"] = db.session.scalar(
                        db.select(db.func.count(SalesOrder.id)).where(
                            SalesOrder.assigned_driver_id == current_user.id,
                            SalesOrder.status == "ready_for_dispatch")) or 0
                from services import approvals as _appr
                if _appr.is_approver(current_user):
                    ctx["approvals_pending"] = _appr.pending_count()
                elif getattr(current_user, "is_pricing_officer", False):
                    from models import PriceApproval as _PA
                    ctx["approvals_pending"] = db.session.scalar(
                        db.select(db.func.count(_PA.id)).where(
                            _PA.status == "pending",
                            _PA.requested_by_id == current_user.id)) or 0
                from services.security import can_allocate_pricelists
                if can_allocate_pricelists(current_user) or current_user.can_manage_all:
                    from models import Customer
                    ctx["pending_onboarding"] = db.session.scalar(
                        db.select(db.func.count(Customer.id)).where(
                            Customer.onboarding_status == "pending",
                            Customer.archived.is_(False))) or 0
                if getattr(current_user, "can_clear_credit", False):
                    from models import CreditAlert as _CA
                    ctx["credit_alerts_open"] = db.session.scalar(
                        db.select(db.func.count(_CA.id)).where(
                            _CA.status == "open")) or 0
        except Exception:
            # Badge counts are non-critical; log the failure but still render
            # the page with safe zero defaults set above.
            app.logger.exception("Failed to compute navbar badge counts")
        return ctx


def register_session_guard(app):
    @app.before_request
    def enforce_idle_timeout():
        # Sessions expire after 8 hours of inactivity (unless remembered cookie).
        session.permanent = True
        if current_user.is_authenticated:
            last = session.get("_last_seen")
            now = datetime.utcnow()
            if last:
                try:
                    last_dt = datetime.fromisoformat(last)
                    if now - last_dt > app.config["PERMANENT_SESSION_LIFETIME"]:
                        from flask_login import logout_user
                        logout_user()
                        session.clear()
                except ValueError:
                    pass
            session["_last_seen"] = now.isoformat()


def register_portal_guard(app):
    @app.before_request
    def keep_customers_in_portal():
        if not current_user.is_authenticated:
            return None
        ep = request.endpoint or ""
        if getattr(current_user, "is_customer_user", False):
            # Catalogue (view/file/download) is shared with customers; manage is admin-only.
            if ep.startswith("catalogue") and ep != "catalogue.manage":
                return None
            if not (ep.startswith("portal") or ep.startswith("auth")
                    or ep in ("static", "index")):
                return redirect(url_for("portal.home"))
            return None
        if getattr(current_user, "is_driver", False):
            # Drivers only see their delivery screens.
            if not (ep.startswith("driver") or ep.startswith("auth")
                    or ep in ("static", "index")):
                return redirect(url_for("driver.home"))
            return None
        if ep.startswith("portal"):
            return redirect(url_for("dashboard.home"))
        return None


def register_errorhandlers(app):
    def _wants_json():
        return request.path.startswith("/api/")

    @app.errorhandler(403)
    def forbidden(e):
        if _wants_json():
            return jsonify(error="forbidden", message="Not permitted."), 403
        return render_template("error.html", code=403,
                               message="You do not have permission to view or change this."), 403

    @app.errorhandler(404)
    def not_found(e):
        if _wants_json():
            return jsonify(error="not_found", message="Resource not found."), 404
        return render_template("error.html", code=404,
                               message="That page or record was not found."), 404

    @app.errorhandler(401)
    def unauthorized(e):
        if _wants_json():
            return jsonify(error="unauthorized", message="Authentication required."), 401
        return redirect(url_for("auth.login"))

    @app.errorhandler(405)
    def method_not_allowed(e):
        if _wants_json():
            return jsonify(error="method_not_allowed", message="Method not allowed."), 405
        return render_template("error.html", code=405,
                               message="That action is not allowed here."), 405

    @app.errorhandler(500)
    def server_error(e):
        if _wants_json():
            return jsonify(error="server_error", message="Internal error."), 500
        return render_template("error.html", code=500,
                               message="Something went wrong."), 500


app = create_app()


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
