"""Sales Manager: set monthly sales targets for reps and track attainment."""
from datetime import date
from functools import wraps

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort)
from flask_login import login_required, current_user

from extensions import db
from models import User, Customer, Product
from services.permissions import has_perm
from services import targets as tsvc
from services.audit import log

bp = Blueprint("targets", __name__, url_prefix="/targets")


def _guard(fn):
    @wraps(fn)
    @login_required
    def wrapper(*a, **k):
        if not (current_user.is_admin or has_perm(current_user, "manage_targets")):
            abort(403)
        return fn(*a, **k)
    return wrapper


def _view_guard(fn):
    """Viewing rep performance is wider than setting targets: the CEO and
    sales director read the reports without the manage_targets permission
    (Stephan, 1 Aug 2026)."""
    @wraps(fn)
    @login_required
    def wrapper(*a, **k):
        if not (current_user.is_admin
                or getattr(current_user, "is_ceo", False)
                or getattr(current_user, "is_sales_director", False)
                or getattr(current_user, "is_sales_manager", False)
                or has_perm(current_user, "manage_targets")):
            abort(403)
        return fn(*a, **k)
    return wrapper


def _ym(default=None):
    raw = request.args.get("ym") or request.form.get("ym") or ""
    try:
        y, m = raw.split("-")
        return int(y), int(m)
    except ValueError:
        t = default or date.today()
        return t.year, t.month


def _to_int(v):
    """M13: parse an id from a parallel-array form field; skip blanks/bad values
    instead of raising a 500."""
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _reps():
    # A sales manager handles only the reps allocated to them; admins, the
    # sales director and other approvers see every rep.
    if getattr(current_user, "is_sales_manager", False):
        return sorted(current_user.managed_reps, key=lambda r: r.full_name or "")
    return db.session.scalars(
        db.select(User).filter_by(role="rep").order_by(User.full_name)).all()


@bp.route("/")
@_view_guard
def index():
    import re as _re
    from datetime import timedelta
    from models import Invoice, InvoiceLine, Product
    from services.inventory_costing import parse_pack_weight_kg
    year, month = _ym()
    # 30-day revenue and kg per customer, mapped onto each rep's book below.
    today = date.today()
    w30 = today - timedelta(days=29)
    rev30 = {}
    for cid, v in db.session.execute(
            db.select(Invoice.customer_id, db.func.sum(Invoice.untaxed))
            .where(Invoice.invoice_date >= w30,
                   Invoice.payment_status != "Reversed",
                   Invoice.customer_id.isnot(None))
            .group_by(Invoice.customer_id)):
        rev30[cid] = float(v or 0)

    def _pwt(p):
        t = _re.sub(r"^[^0-9]*", "", str(p.pack_size or ""))
        w = parse_pack_weight_kg(t)
        if w:
            return w
        return 1.0 if (p.unit_of_measure or "").strip().lower() == "kg" else None

    pw = {p.id: _pwt(p) for p in db.session.scalars(db.select(Product))}
    kg30 = {}
    for cid, pid, qty in db.session.execute(
            db.select(Invoice.customer_id, InvoiceLine.product_id,
                      InvoiceLine.quantity)
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .where(Invoice.invoice_date >= w30,
                   Invoice.payment_status != "Reversed",
                   Invoice.customer_id.isnot(None))):
        q = float(qty or 0)
        w = pw.get(pid)
        kg30[cid] = kg30.get(cid, 0.0) + (q * w if w else q)
    rows = []
    for rep in _reps():
        tg = tsvc.targets_for(rep.id, year, month)
        act = tsvc.rep_actuals(rep, year, month)
        tot = tg["total"]
        pct = (act["total"] / tot * 100.0) if tot else None
        assigned = rep.assigned_customers
        rows.append({"rep": rep, "target": tot, "actual": act["total"], "pct": pct,
                     "n_cust": len(tg["customer"]), "n_prod": len(tg["product"]),
                     "n_customers": len(assigned),
                     "rev30": sum(rev30.get(c.id, 0.0) for c in assigned),
                     "kg30": sum(kg30.get(c.id, 0.0) for c in assigned)})
    rows.sort(key=lambda r: (-r["kg30"], -r["rev30"]))
    label = date(year, month, 1).strftime("%B %Y")
    return render_template("targets/index.html", rows=rows, year=year, month=month,
                           ym=f"{year}-{month:02d}", label=label)


@bp.route("/rep/<int:rep_id>/performance")
@_view_guard
def performance(rep_id):
    """CEO report per rep: 30-day, 90-day and 12-month performance,
    attainment against the month's target, and the rep's customers ranked
    best first (Stephan, 1 Aug 2026)."""
    import re as _re
    from datetime import timedelta
    from collections import defaultdict
    from models import Invoice, InvoiceLine, SalesOrder, Product
    from services.revenue import net_ugx
    from services.inventory_costing import parse_pack_weight_kg

    rep = db.session.get(User, rep_id)
    if not rep or rep.role != "rep":
        abort(404)
    if getattr(current_user, "is_sales_manager", False) \
            and not current_user.is_admin and rep.manager_id != current_user.id:
        abort(403)

    today = date.today()
    w30 = today - timedelta(days=29)
    w90 = today - timedelta(days=89)
    w365 = today - timedelta(days=364)
    assigned = list(rep.assigned_customers)
    ids = {c.id for c in assigned}
    last_inv = db.session.scalar(db.select(db.func.max(Invoice.invoice_date)))

    # Kilograms per quantity unit — same convention as the customer report:
    # quantities are packs; the pack-size string converts to kg; unknown
    # products count 1 qty = 1 kg and flag the page as approximate.
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

    per = {cid: [0.0, 0.0, 0.0] for cid in ids}       # value 30d, 90d, 1y
    per_kg = {cid: [0.0, 0.0, 0.0] for cid in ids}    # kg    30d, 90d, 1y
    prod_rev = defaultdict(lambda: [0.0, 0.0, 0.0])   # label -> value windows
    prod_kg = defaultdict(lambda: [0.0, 0.0, 0.0])    # label -> kg windows
    last_buy = {}
    monthly = defaultdict(float)
    monthly_kg = defaultdict(float)

    def _windows(d):
        out = [2]
        if d >= w90:
            out.append(1)
        if d >= w30:
            out.append(0)
        return out

    def hit(cid, d, v):
        monthly[d.year * 12 + d.month] += v
        row = per.get(cid)
        if row is None:
            return
        for wi in _windows(d):
            row[wi] += v
        if v > 0 and (cid not in last_buy or d > last_buy[cid]):
            last_buy[cid] = d

    def hit_line(cid, d, label, pid, qty, amount):
        kgs = _kg(pid, qty)
        monthly_kg[d.year * 12 + d.month] += kgs
        for wi in _windows(d):
            prod_rev[label][wi] += amount
            prod_kg[label][wi] += kgs
            if cid in per_kg:
                per_kg[cid][wi] += kgs

    if ids:
        # Invoices own every day up to the latest invoice date...
        for i in db.session.scalars(db.select(Invoice).where(
                Invoice.customer_id.in_(ids),
                Invoice.payment_status != "Reversed",
                Invoice.invoice_date >= w365)):
            hit(i.customer_id, i.invoice_date, float(i.untaxed or 0))
        for cid, d, pid, pnm, qty, amt in db.session.execute(
                db.select(Invoice.customer_id, Invoice.invoice_date,
                          InvoiceLine.product_id, InvoiceLine.product_name,
                          InvoiceLine.quantity, InvoiceLine.amount)
                .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
                .where(Invoice.customer_id.in_(ids),
                       Invoice.payment_status != "Reversed",
                       Invoice.invoice_date >= w365)):
            label = pname.get(pid) or pnm or "—"
            hit_line(cid, d, label, pid, qty, float(amt or 0))
        # ...live app orders own the days after it.
        CONFIRMED = ("placed", "in_fulfillment", "pending", "ready_for_dispatch",
                     "out_for_delivery", "dispatched", "delivered", "fulfilled")
        for o in db.session.scalars(db.select(SalesOrder).where(
                SalesOrder.customer_id.in_(ids),
                SalesOrder.status.in_(CONFIRMED),
                SalesOrder.order_date.isnot(None))):
            if o.order_date < w365 or (last_inv and o.order_date <= last_inv):
                continue
            v = net_ugx(o)
            hit(o.customer_id, o.order_date, v)
            net = float(o.subtotal or 0)
            rate = (v / net) if net else (
                1.0 if (o.currency or "UGX") == "UGX" else 0.0)
            for l in o.lines:
                label = pname.get(l.product_id) or l.description or "—"
                hit_line(o.customer_id, o.order_date, label, l.product_id,
                         l.delivered_qty, float(l.line_total or 0) * rate)

    tot30 = sum(r[0] for r in per.values())
    tot90 = sum(r[1] for r in per.values())
    tot365 = sum(r[2] for r in per.values())
    kg30 = sum(r[0] for r in per_kg.values())
    kg90 = sum(r[1] for r in per_kg.values())
    kg365 = sum(r[2] for r in per_kg.values())

    cust_rows = sorted(
        ({"customer": c, "d30": per[c.id][0], "d90": per[c.id][1],
          "y1": per[c.id][2], "kg30": per_kg[c.id][0], "kg90": per_kg[c.id][1],
          "kgy1": per_kg[c.id][2], "last": last_buy.get(c.id)} for c in assigned),
        key=lambda r: (-r["kgy1"], (r["customer"].name or "").lower()))

    PROD_CAP = 30
    prod_rows = sorted(
        ({"name": k, "d30": v[0], "d90": v[1], "y1": v[2],
          "kg30": prod_kg[k][0], "kg90": prod_kg[k][1], "kgy1": prod_kg[k][2]}
         for k, v in prod_rev.items() if v[2] or prod_kg[k][2]),
        key=lambda r: -r["kgy1"])
    prod_more = max(0, len(prod_rows) - PROD_CAP)
    prod_rows = prod_rows[:PROD_CAP]

    cur_idx = today.year * 12 + today.month
    trend_idx = list(range(cur_idx - 11, cur_idx + 1))
    trend_labels = [date((i - 1) // 12, (i - 1) % 12 + 1, 1).strftime("%b %y")
                    for i in trend_idx]
    trend_values = [round(monthly.get(i, 0.0)) for i in trend_idx]
    trend_kg = [round(monthly_kg.get(i, 0.0)) for i in trend_idx]

    tg = tsvc.targets_for(rep_id, today.year, today.month)
    act = tsvc.rep_actuals(rep, today.year, today.month)
    target_pct = (act["total"] / tg["total"] * 100.0) if tg["total"] else None
    cname = {c.id: c.name for c in assigned}
    cust_targets = sorted(
        ({"cid": cid, "name": cname.get(cid, "—"), "target": amt,
          "actual": act["by_customer"].get(cid, 0.0),
          "pct": (act["by_customer"].get(cid, 0.0) / amt * 100.0) if amt else None}
         for cid, amt in tg["customer"].items()),
        key=lambda r: r["pct"] or 0)

    label = today.strftime("%B %Y")
    return render_template(
        "targets/performance.html", rep=rep, today=today, label=label,
        ym=f"{today.year}-{today.month:02d}",
        tot30=tot30, tot90=tot90, tot365=tot365,
        kg30=kg30, kg90=kg90, kg365=kg365, approx_kg=approx[0],
        prod_rows=prod_rows, prod_more=prod_more,
        target_total=tg["total"], target_actual=act["total"], target_pct=target_pct,
        cust_targets=cust_targets, cust_rows=cust_rows,
        n_customers=len(assigned),
        trend_labels=trend_labels, trend_values=trend_values,
        trend_kg=trend_kg)


@bp.route("/rep/<int:rep_id>", methods=["GET", "POST"])
@_guard
def rep(rep_id):
    rep = db.session.get(User, rep_id)
    if not rep or rep.role != "rep":
        abort(404)
    if getattr(current_user, "is_sales_manager", False) and rep.manager_id != current_user.id:
        abort(403)
    year, month = _ym()

    if request.method == "POST":
        # overall total
        tsvc.upsert_target(rep_id, year, month, "total", _money(request.form.get("total")))
        # customer lines (parallel arrays)
        cids = request.form.getlist("cust_id")
        camts = request.form.getlist("cust_amt")
        for cid, amt in zip(cids, camts):
            iid = _to_int(cid)
            if iid is not None:
                tsvc.upsert_target(rep_id, year, month, "customer", _money(amt),
                                   customer_id=iid)
        # product lines
        pids = request.form.getlist("prod_id")
        pamts = request.form.getlist("prod_amt")
        for pid, amt in zip(pids, pamts):
            iid = _to_int(pid)
            if iid is not None:
                tsvc.upsert_target(rep_id, year, month, "product", _money(amt),
                                   product_id=iid)
        db.session.commit()
        log("targets_set", "user", rep_id,
            detail=f"targets set for {rep.full_name} {year}-{month:02d}", commit=True)
        flash("Targets saved.", "success")
        return redirect(url_for("targets.rep", rep_id=rep_id, ym=f"{year}-{month:02d}"))

    tg = tsvc.targets_for(rep_id, year, month)
    act = tsvc.rep_actuals(rep, year, month)
    assigned = sorted(rep.assigned_customers, key=lambda c: c.name)
    products = db.session.scalars(
        db.select(Product).order_by(Product.description)).all()
    cust_by_id = {c.id: c for c in assigned}
    prod_by_id = {p.id: p for p in products}
    label = date(year, month, 1).strftime("%B %Y")
    return render_template("targets/rep.html", rep=rep, year=year, month=month,
                           ym=f"{year}-{month:02d}", label=label, tg=tg, act=act,
                           assigned=assigned, products=products,
                           cust_by_id=cust_by_id, prod_by_id=prod_by_id)


def _money(v):
    try:
        return round(float(str(v).replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return 0.0
