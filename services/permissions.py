"""Role-based capability permissions, editable by an admin.

Permissions are stored as Settings rows ("perm:<role>:<cap>" and
"perm:report:<role>:<reportkey>"). Admins always have everything; customer
portal users never have staff capabilities. Other roles fall back to DEFAULTS
until an admin overrides them, so behaviour is unchanged until edited.
"""
from extensions import db
from models import Setting

# Capabilities that gate actions across the app.
CAPS = [
    ("manage_catalogue", "Manage catalogue", "Create/edit/archive pricelists, products, customers and customer pricelists."),
    ("fulfil_orders", "Fulfil orders", "Confirm, start, dispatch, complete orders and mark stock (the fulfilment inbox)."),
    ("view_reports", "View reports", "Open the Reports tab (which individual reports is set below)."),
    ("create_offers_orders", "Create offers & orders", "Build quotes and place orders for customers."),
    ("log_activity", "Log CRM activity", "Record visits and calls, manage contacts and follow-ups."),
    ("manage_stock", "Manage stock", "Add stock, record wastage and adjustments in the store."),
    ("audit_stock", "Audit stock", "Run stock takes, record physical counts and post corrections."),
    ("manage_targets", "Manage rep targets", "Set monthly sales targets for reps (Sales Manager)."),
    ("view_production", "View production", "Open the Production tab: what to produce, open-order coverage and stock versus demand."),
    ("record_production", "Record production", "Record produced goods, which adds them to stock (Production Manager)."),
    ("view_accounting", "View accounting", "Open the Accounting tab: journal, accounts, trial balance and reports."),
    ("post_journal", "Post journal entries", "Create and post manual journal entries (Accountant)."),
    ("view_costing", "View costing", "Open the Costing module: recipes, ingredients, carcass cuts, cost dashboard."),
    ("edit_costing", "Edit costing", "Change ingredient prices, recipes, carcass breakdowns, overhead and margins."),
    ("approve_credit_notes", "Approve credit notes", "Raise a fiscal credit note against a posted invoice (CFO). Must never sit with whoever records customer receipts."),
    ("record_purchases", "Record purchases", "Capture supplier bills and expenses: stock buys into inventory, non-stock costs to expense (Finance clerk and up)."),
    ("record_receipts", "Record receipts", "Record customer payments against invoices (Cashier and up). Never combine with credit-note approval."),
    ("pay_suppliers", "Pay suppliers", "Record payments clearing supplier bills (Finance manager and up)."),
    ("reconcile_bank", "Reconcile bank & cash", "Tick ledger lines against bank/cash statements and close reconciliations (Finance manager and up)."),
    ("record_shop_sales", "Record shop sales", "Enter a shop's daily takings: posts revenue, VAT and COGS and reduces the shop's stock (Cashier and up)."),
    ("import_finance_files", "Import daily finance files", "Upload the daily Odoo exports (itemized invoices, payments, customer receivables) that update customer statements and credit balances (Finance clerk and up)."),
]

# Individual reports that can be allowed per role (when view_reports is on).
REPORTS = [
    ("fulfilment", "Fulfilment"), ("sales", "Sales"),
    ("customer_insights", "Customer insights"), ("lapsed", "Lapsed & at-risk"),
    ("reorder", "Reorder due"), ("scorecard", "Scorecard"),
    ("velocity", "Product velocity"), ("fulfilment_perf", "Fulfilment KPIs"),
    ("offers", "Offer conversion"),
    ("feedback", "Customer feedback"),
    ("history", "Sales history"),
    ("product_month", "Products by month"),
    ("all_time", "All-time report"),
    ("top90", "Top performers (90 days)"),
]

# Roles that can be configured (admin is always full; customer is portal-only).
ROLES = [("manager", "Manager"), ("sales_director", "Sales director"),
         ("sales_manager", "Sales manager"),
         ("rep", "Rep"), ("order_manager", "Order manager"),
         ("fulfillment_officer", "Fulfilment officer"), ("telesales", "Telesales"),
         ("pricing_officer", "Pricing officer"), ("dispatch_officer", "Dispatch officer"),
         ("delivery", "Delivery driver"), ("store_manager", "Store manager"),
         ("stock_auditor", "Stock auditor"),
         ("production_manager", "Production manager"),
         ("cfo", "CFO"),
         ("finance_manager", "Finance manager"),
         ("finance_clerk", "Finance clerk"),
         ("cashier", "Cashier"),
         ("finance_viewer", "Finance viewer")]

DEFAULTS = {
    # 21 Jul 2026: manage_stock removed from manager — only the store manager
    # adjusts stock quantities (adding produced goods, wastage, counts).
    # Admin/CEO keep the structural bypass in has_perm.
    "manager": {"manage_catalogue": True, "fulfil_orders": True,
                "view_reports": True, "create_offers_orders": True, "log_activity": True,
                "manage_stock": False, "audit_stock": True, "manage_targets": True,
                "view_production": True, "record_production": True,
                "view_accounting": True, "record_purchases": True,
                "record_receipts": True, "pay_suppliers": True,
                "reconcile_bank": True, "record_shop_sales": True,
                "view_costing": True, "edit_costing": True},
    "sales_director": {"manage_catalogue": False, "fulfil_orders": False,
                       "view_reports": True, "create_offers_orders": False,
                       "log_activity": True, "manage_targets": True},
    "sales_manager": {"manage_catalogue": False, "fulfil_orders": False,
                      "view_reports": True, "create_offers_orders": False,
                      "log_activity": True, "manage_targets": True},
    "rep": {"manage_catalogue": False, "fulfil_orders": False,
            "view_reports": False, "create_offers_orders": True, "log_activity": True},
    "order_manager": {"manage_catalogue": False, "fulfil_orders": False,
                      "view_reports": True, "create_offers_orders": True, "log_activity": True},
    "fulfillment_officer": {"manage_catalogue": False, "fulfil_orders": True,
                            "view_reports": False, "create_offers_orders": False,
                            "log_activity": False},
    "telesales": {"manage_catalogue": False, "fulfil_orders": False,
                  "view_reports": True, "create_offers_orders": False,
                  "log_activity": True},
    "pricing_officer": {"manage_catalogue": True, "fulfil_orders": False,
                        "view_reports": False, "create_offers_orders": False,
                        "log_activity": False,
                        "view_costing": True, "edit_costing": True},
    "dispatch_officer": {"manage_catalogue": False, "fulfil_orders": False,
                         "view_reports": False, "create_offers_orders": False,
                         "log_activity": False},
    "delivery": {"manage_catalogue": False, "fulfil_orders": False,
                 "view_reports": False, "create_offers_orders": False,
                 "log_activity": False},
    "store_manager": {"manage_catalogue": False, "fulfil_orders": False,
                      "view_reports": False, "create_offers_orders": False,
                      "log_activity": False, "manage_stock": True,
                      "view_production": True},
    "stock_auditor": {"manage_catalogue": False, "fulfil_orders": False,
                      "view_reports": False, "create_offers_orders": False,
                      "log_activity": False, "manage_stock": False, "audit_stock": True,
                      "view_production": True},
    "production_manager": {"manage_catalogue": False, "fulfil_orders": False,
                           "view_reports": False, "create_offers_orders": False,
                           "log_activity": False, "manage_stock": False,
                           "view_production": True, "record_production": True,
                           "view_costing": True},
    # Finance roles. Segregation of duties, encoded:
    #   * entry (clerk, cashier) never approves;
    #   * posting (finance manager) never approves credit notes;
    #   * approval (CFO) reviews, posts when needed, and — from Phase 3 —
    #     holds credit-note approval and period close, which must never sit
    #     with whoever records customer receipts.
    "cfo": {"manage_catalogue": False, "fulfil_orders": False,
            "view_reports": True, "create_offers_orders": False,
            "log_activity": False,
            "view_accounting": True, "post_journal": True,
            "view_costing": True, "edit_costing": False,
            "view_production": True, "approve_credit_notes": True,
            "record_purchases": True,
            "pay_suppliers": True, "reconcile_bank": True,
            "import_finance_files": True},
    "finance_manager": {"manage_catalogue": False, "fulfil_orders": False,
                        "view_reports": True, "create_offers_orders": False,
                        "log_activity": False,
                        "view_accounting": True, "post_journal": True,
                        "view_costing": True, "record_purchases": True,
                        "record_receipts": True, "pay_suppliers": True,
                        "reconcile_bank": True, "record_shop_sales": True,
                        "import_finance_files": True},
    "finance_clerk": {"manage_catalogue": False, "fulfil_orders": False,
                      "view_reports": False, "create_offers_orders": False,
                      "log_activity": False,
                      "view_accounting": True, "post_journal": False,
                      "record_purchases": True, "record_receipts": True,
                      "record_shop_sales": True,
                      "import_finance_files": True},
    # Cashier: no accounting screens today; gains record-receipt rights when
    # the cash module lands (Phase 5). The role exists now so shop accounts
    # can be created ahead of that rollout.
    "cashier": {"manage_catalogue": False, "fulfil_orders": False,
                "view_reports": False, "create_offers_orders": False,
                "log_activity": False, "view_accounting": False,
                "record_receipts": True, "record_shop_sales": True},
    "finance_viewer": {"manage_catalogue": False, "fulfil_orders": False,
                       "view_reports": True, "create_offers_orders": False,
                       "log_activity": False,
                       "view_accounting": True, "post_journal": False},
}


def _key(role, cap):
    return f"perm:{role}:{cap}"


def _rkey(role, rep):
    return f"perm:report:{role}:{rep}"


def has_perm(user, cap):
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.role in ("admin", "ceo"):
        return True
    if user.role == "customer":
        return False
    row = db.session.get(Setting, _key(user.role, cap))
    if row is not None and row.value is not None:
        return row.value == "1"
    return DEFAULTS.get(user.role, {}).get(cap, False)


def can_view_report(user, report_key):
    if not has_perm(user, "view_reports"):
        return False
    if user.role in ("admin", "ceo"):
        return True
    row = db.session.get(Setting, _rkey(user.role, report_key))
    if row is not None and row.value is not None:
        return row.value == "1"
    return True   # default: if reports are allowed, all reports are visible


def allowed_reports(user):
    return {k for k, _ in REPORTS if can_view_report(user, k)}


def _set(key, value):
    row = db.session.get(Setting, key)
    if row is None:
        db.session.add(Setting(key=key, value="1" if value else "0"))
    else:
        row.value = "1" if value else "0"


def save_matrix(form):
    """form contains checkbox names 'cap:<role>:<cap>' and 'rep:<role>:<key>'."""
    for role, _ in ROLES:
        for cap, _l, _d in CAPS:
            _set(_key(role, cap), form.get(f"cap:{role}:{cap}") == "on")
        for rk, _l in REPORTS:
            _set(_rkey(role, rk), form.get(f"rep:{role}:{rk}") == "on")
    db.session.commit()


def current_matrix():
    """Return {role: {'caps': {cap:bool}, 'reports': {key:bool}}} for the UI."""
    out = {}
    for role, label in ROLES:
        caps = {}
        for cap, _l, _d in CAPS:
            row = db.session.get(Setting, _key(role, cap))
            caps[cap] = (row.value == "1") if (row and row.value is not None) \
                else DEFAULTS.get(role, {}).get(cap, False)
        reps = {}
        for rk, _l in REPORTS:
            row = db.session.get(Setting, _rkey(role, rk))
            reps[rk] = (row.value == "1") if (row and row.value is not None) else True
        out[role] = {"label": label, "caps": caps, "reports": reps}
    return out
