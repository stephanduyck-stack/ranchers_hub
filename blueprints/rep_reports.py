"""Reports scoped to a sales rep's own assigned customers.

Sales figures come from invoice history for months up to the history cut-over
and from live orders after it, matching the rep dashboard. Everything is
filtered to the customers assigned to the logged-in rep.
"""
from collections import defaultdict
from datetime import date, timedelta

from flask import Blueprint, render_template, request, abort
from flask_login import login_required, current_user

from extensions import db
from models import Invoice, SalesOrder, Customer
from services import targets as tsvc

bp = Blueprint("my_reports", __name__, url_prefix="/my-reports")

CONFIRMED = tsvc.CONFIRMED


@bp.before_request
@login_required
def _guard():
    # Reps see their own; admins/managers/sales roles may preview (their assigned
    # set is usually empty, so they simply see no rows).
    if current_user.role == "customer" or getattr(current_user, "is_driver", False):
        abort(403)


def _i2d(idx):
    return date((idx - 1) // 12, (idx - 1) % 12 + 1, 1)


def _assigned():
    return list(current_user.assigned_customers)


def _series():
    """Return monthly{idx->ugx}, cust (per-customer dict), cutover, target idx."""
    assigned = _assigned()
    ids = {c.id for c in assigned}
    cutover = tsvc.cutover_idx()
    today = date.today()
    target = max(cutover, today.year * 12 + today.month)
    monthly = defaultdict(float)
    cust = {c.id: {"obj": c, "recent": 0.0, "life": 0.0, "last": 0, "orders": 0}
            for c in assigned}
    if ids:
        for i in db.session.scalars(db.select(Invoice).where(
                Invoice.customer_id.in_(ids),
                Invoice.payment_status != "Reversed", Invoice.invoice_date.isnot(None))):
            idx = i.invoice_date.year * 12 + i.invoice_date.month
            if idx > cutover:
                continue
            v = float(i.untaxed or 0)
            monthly[idx] += v
            c = cust.get(i.customer_id)
            if c:
                c["life"] += v
                if v > 0:
                    c["last"] = max(c["last"], idx)
                if target - 2 <= idx <= target:
                    c["recent"] += v
        for o in db.session.scalars(db.select(SalesOrder).where(
                SalesOrder.customer_id.in_(ids), SalesOrder.status.in_(CONFIRMED),
                SalesOrder.order_date.isnot(None))):
            idx = o.order_date.year * 12 + o.order_date.month
            if idx <= cutover:
                continue
            v = tsvc._ugx(o)
            monthly[idx] += v
            c = cust.get(o.customer_id)
            if c:
                c["life"] += v
                c["last"] = max(c["last"], idx)
                if target - 2 <= idx <= target:
                    c["recent"] += v
    return monthly, cust, cutover, target


@bp.route("/")
def index():
    monthly, cust, cutover, target = _series()
    mtd = monthly.get(target, 0.0)
    last = monthly.get(target - 1, 0.0)
    tg = tsvc.targets_for(current_user.id, _i2d(target).year, _i2d(target).month)
    n_lapsed = sum(1 for c in cust.values() if c["last"] and c["last"] <= target - 3)
    return render_template("my_reports/index.html", mtd=mtd, last=last,
                           mtd_label=_i2d(target).strftime("%B %Y"),
                           n_customers=len(cust), n_lapsed=n_lapsed,
                           target_total=tg["total"])


@bp.route("/sales")
def sales():
    monthly, cust, cutover, target = _series()
    idxs = list(range(target - 11, target + 1))
    labels = [_i2d(i).strftime("%b %y") for i in idxs]
    values = [round(monthly.get(i, 0.0)) for i in idxs]
    by_customer = sorted(
        ({"name": c["obj"].name, "recent": c["recent"], "life": c["life"]}
         for c in cust.values()), key=lambda r: r["recent"], reverse=True)
    months = [{"label": _i2d(i).strftime("%B %Y"), "value": monthly.get(i, 0.0)}
              for i in reversed(idxs)]
    return render_template("my_reports/sales.html", labels=labels, values=values,
                           by_customer=by_customer, months=months,
                           mtd_label=_i2d(target).strftime("%B %Y"))


@bp.route("/customers")
def customers():
    monthly, cust, cutover, target = _series()
    today = date.today()
    rows = []
    for c in cust.values():
        last_dt = _i2d(c["last"]) if c["last"] else None
        gap = c["last"] and (target - c["last"])
        status = "active"
        if c["last"] and c["last"] <= target - 3:
            status = "lapsed"
        elif c["last"] and c["last"] <= target - 1 and c["last"] != target:
            status = "quiet"
        rows.append({"customer": c["obj"], "last": last_dt, "recent": c["recent"],
                     "life": c["life"], "status": status, "gap": gap or 0})
    rows.sort(key=lambda r: r["recent"], reverse=True)
    return render_template("my_reports/customers.html", rows=rows, today=today)


@bp.route("/lapsed")
def lapsed():
    months = request.args.get("months", default=3, type=int)
    monthly, cust, cutover, target = _series()
    rows = []
    for c in cust.values():
        if c["last"] and c["last"] <= target - months:
            rows.append({"customer": c["obj"], "last": _i2d(c["last"]),
                         "gap": target - c["last"], "life": c["life"]})
    rows.sort(key=lambda r: -r["gap"])
    return render_template("my_reports/lapsed.html", rows=rows, months=months)


@bp.route("/reorder")
def reorder():
    today = date.today()
    ids = {c.id for c in _assigned()}
    by_cust = defaultdict(set)
    cobj = {c.id: c for c in _assigned()}
    if ids:
        for i in db.session.scalars(db.select(Invoice).where(
                Invoice.customer_id.in_(ids), Invoice.number.like("INV%"),
                Invoice.payment_status != "Reversed", Invoice.invoice_date.isnot(None))):
            by_cust[i.customer_id].add(i.invoice_date)
    rows = []
    for cid, dset in by_cust.items():
        dates = sorted(dset)
        if len(dates) < 3:
            continue
        intervals = sorted((dates[k] - dates[k - 1]).days for k in range(1, len(dates)))
        avg = intervals[len(intervals) // 2]
        predicted = dates[-1] + timedelta(days=round(avg))
        rows.append({"customer": cobj.get(cid), "last": dates[-1], "avg": round(avg),
                     "predicted": predicted, "due_in": (predicted - today).days,
                     "orders": len(dates)})
    rows = [r for r in rows if r["customer"]]
    rows.sort(key=lambda r: r["due_in"])
    overdue = [r for r in rows if r["due_in"] < 0]
    due_soon = [r for r in rows if 0 <= r["due_in"] <= 7]
    return render_template("my_reports/reorder.html", rows=rows, overdue=overdue,
                           due_soon=due_soon, today=today)


@bp.route("/feedback")
def feedback():
    ids = {c.id for c in _assigned()}
    rated = []
    if ids:
        rated = db.session.scalars(db.select(SalesOrder).where(
            SalesOrder.customer_id.in_(ids), SalesOrder.rating.isnot(None))
            .order_by(SalesOrder.rated_at.desc())).all()
    n = len(rated)
    avg = round(sum(o.rating for o in rated) / n, 1) if n else None
    low = [o for o in rated if o.rating <= 2]
    return render_template("my_reports/feedback.html", rated=rated, n=n, avg=avg, low=low)
