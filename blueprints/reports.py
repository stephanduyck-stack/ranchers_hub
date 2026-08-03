"""Reporting: fulfilment, sales (by total/customer/product/rep) and offer
conversion. Visible to managers, admins and order managers."""
import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import (Blueprint, render_template, request, redirect, url_for,
                   abort, Response)
from flask_login import login_required, current_user

from extensions import db
from models import SalesOrder, SalesOrderLine, Offer, Customer, User
from services.revenue import net_ugx

bp = Blueprint("reports", __name__, url_prefix="/reports")

CONFIRMED = ("placed", "in_fulfillment", "pending", "ready_for_dispatch",
             "out_for_delivery", "dispatched", "delivered", "fulfilled")
DELIVERED = ("delivered", "fulfilled")


@bp.before_request
@login_required
def _guard():
    from services.permissions import has_perm, can_view_report
    if not has_perm(current_user, "view_reports"):
        abort(403)
    # Per-report visibility: map the requested endpoint to a report key.
    ep = (request.endpoint or "").split(".")[-1]
    key = {"fulfilment": "fulfilment", "sales": "sales",
           "sales_by_month": "sales", "sales_month_detail": "sales",
           "customer_insights": "customer_insights", "lapsed": "lapsed",
           "reorder": "reorder", "scorecard": "scorecard", "velocity": "velocity",
           "fulfilment_perf": "fulfilment_perf", "offers_report": "offers",
           "top90": "top90"}.get(ep)
    if key and not can_view_report(current_user, key):
        abort(403)


def _range():
    """Read from/to query params; default to today."""
    today = date.today()
    frm = _parse(request.args.get("from")) or today
    to = _parse(request.args.get("to")) or today
    if to < frm:
        frm, to = to, frm
    return frm, to


def _parse(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _money(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------
@bp.route("/")
def index():
    today = date.today()
    orders = db.session.scalars(db.select(SalesOrder)).all()
    confirmed_today = [o for o in orders if o.status in CONFIRMED and o.order_date == today]
    pending = [o for o in orders if o.status == "pending"]
    backorders_open = [o for o in orders
                       if o.backorder_of_id and o.status in ("placed", "in_fulfillment", "pending")]
    # value today by currency
    val_today = defaultdict(float)
    for o in confirmed_today:
        val_today[o.currency] += _money(o.total)

    offers = db.session.scalars(db.select(Offer)).all()
    decided = [o for o in offers if o.status in ("converted", "not_ordered")]
    won = [o for o in offers if o.status == "converted"]
    conv_rate = (len(won) / len(decided) * 100.0) if decided else None

    # at-risk customers: ordered before but nothing in the last 60 days
    cutoff = today - timedelta(days=60)
    n_at_risk = 0
    for c in db.session.scalars(db.select(Customer).where(Customer.archived.is_(False))):
        co = _customer_orders(c.id)
        if co and max(o.order_date for o in co) < cutoff:
            n_at_risk += 1

    # --- glanceable snapshot: one continuous monthly timeline ---
    # Uploaded invoices (net UGX) own every month they cover — the invoice
    # table is the running sales record from 1 Jul 2026, topped up per upload.
    # Live app orders own months after the latest invoice only, so nothing
    # double-counts when an app order later comes back as an Odoo invoice.
    from models import Invoice, SalesHistory

    # Net (excl-VAT) UGX so live orders match the net invoice history.
    _ugx = net_ugx

    from services.revenue import export_customer_ids, dist_band
    _exp_ids = export_customer_ids()

    def _chan_label(c):
        if c is None:
            return "Other"
        if (c.segment or "customer") == "distributor":
            return f"{dist_band(c, _exp_ids)} distributors"
        return (c.category.name if c.category else None) or "Other"

    hist_cutover = _sh_latest_idx()                   # last pivot month (product mix)
    last_inv = db.session.scalar(db.select(db.func.max(Invoice.invoice_date)))
    cutover = (last_inv.year * 12 + last_inv.month) if last_inv else 0
    cur_idx = today.year * 12 + today.month
    target = max(cutover, hist_cutover, cur_idx)      # the month we report "to date"
    start_idx = target - 5                            # 6-month context
    hist_month, live_month = defaultdict(float), defaultdict(float)
    chan, custr, prodm = defaultdict(float), defaultdict(float), defaultdict(float)

    # H5 resolved 8 Jul 2026: Odoo "Signed" amounts are company currency (UGX)
    # for every invoice; the currency column is informational only.
    for i in db.session.scalars(db.select(Invoice).where(
            Invoice.payment_status != "Reversed",
            Invoice.invoice_date.isnot(None))):
        idx = i.invoice_date.year * 12 + i.invoice_date.month
        val = float(i.untaxed or 0)
        if start_idx <= idx <= target:
            hist_month[idx] += val
        if idx == target:                            # current month only (to date)
            chan[_chan_label(i.customer)] += val
            custr[i.customer_name or "—"] += val

    for o in orders:
        if o.status in CONFIRMED and o.order_date:
            idx = o.order_date.year * 12 + o.order_date.month
            if idx <= cutover:
                continue
            ov = _ugx(o)
            if start_idx <= idx <= target:
                live_month[idx] += ov
            if idx == target:
                chan[_chan_label(o.customer)] += ov
                custr[(o.customer.name if o.customer else "—")] += ov
                # product split on the same net UGX basis
                net = float(o.subtotal or 0)
                rate = (ov / net) if net else (
                    1.0 if (o.currency or "UGX") == "UGX" else 0.0)
                for l in o.lines:
                    prodm[(l.description or l.article_no or "—")] += _money(l.line_total) * rate

    # products this month: from the monthly history pivot while the month is
    # inside it, from the itemized invoice lines once past it.
    ty, tm = (target - 1) // 12, (target - 1) % 12 + 1
    if target <= hist_cutover:
        for s in db.session.scalars(db.select(SalesHistory).where(
                SalesHistory.year == ty, SalesHistory.month == tm)):
            prodm[s.product] += float(s.revenue or 0)
    elif target <= cutover:
        from models import InvoiceLine, Product as _P
        _pl = {p.id: p.description for p in db.session.scalars(db.select(_P))}
        m_start = date(ty, tm, 1)
        for d, pid, pname, amt in db.session.execute(
                db.select(Invoice.invoice_date, InvoiceLine.product_id,
                          InvoiceLine.product_name, InvoiceLine.amount)
                .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
                .where(Invoice.invoice_date >= m_start,
                       Invoice.payment_status != "Reversed")):
            if d.year * 12 + d.month == target:
                prodm[_pl.get(pid) or pname or "—"] += float(amt or 0)

    def mrev(idx):
        return hist_month[idx] if idx <= cutover else live_month[idx]

    idxs = list(range(start_idx, target + 1))
    trend_labels = [_idx_to_date(i).strftime("%b %y") for i in idxs]
    trend_values = [round(mrev(i)) for i in idxs]
    mtd_label = _idx_to_date(target).strftime("%B %Y")
    mtd_rev = mrev(target)
    prev_rev = mrev(target - 1)
    mtd_delta = ((mtd_rev - prev_rev) / prev_rev * 100.0) if prev_rev else None
    chan = dict(sorted(chan.items(), key=lambda kv: kv[1], reverse=True))
    top_cust = sorted(custr.items(), key=lambda kv: kv[1], reverse=True)[:6]
    top_prod = sorted(prodm.items(), key=lambda kv: kv[1], reverse=True)[:6]

    # --- fulfilment performance (last 30 days, by delivery date) — live data ---
    win = today - timedelta(days=29)
    dord = {win + timedelta(days=i): 0.0 for i in range((today - win).days + 1)}
    ddel = {d: 0.0 for d in dord}
    dshort_today = 0
    for o in db.session.scalars(db.select(SalesOrder).where(SalesOrder.status.in_(DELIVERED))):
        fdate = ((o.delivered_at or o.fulfilled_at).date()
                 if (o.delivered_at or o.fulfilled_at) else o.order_date)
        if fdate not in dord:
            continue
        for l in o.lines:
            ordered = l.quantity or 0
            delivered = l.delivered_qty or 0
            dord[fdate] += ordered
            ddel[fdate] += delivered
            if fdate == today and (ordered - delivered > 1e-9 or l.availability == "not_delivered"):
                dshort_today += 1
    fdays = sorted(dord)
    fill_series = [round(ddel[d] / dord[d] * 100, 1) if dord[d] else None for d in fdays]
    tot_ord = sum(dord.values())
    tot_del = sum(ddel.values())
    fill_30 = round(tot_del / tot_ord * 100, 1) if tot_ord else None
    fill_today = (round(ddel[today] / dord[today] * 100, 1) if dord[today] else None)
    n_delivered_today = sum(1 for o in orders
                            if o.status in DELIVERED
                            and ((o.delivered_at or o.fulfilled_at).date()
                                 if (o.delivered_at or o.fulfilled_at) else o.order_date) == today)

    snap = {
        "trend_labels": trend_labels,
        "trend_values": trend_values,
        "chan_labels": list(chan.keys()),
        "chan_values": [round(v) for v in chan.values()],
        "cust_labels": [n for n, _v in top_cust],
        "cust_values": [round(v) for _n, v in top_cust],
        "prod_labels": [n for n, _v in top_prod],
        "prod_values": [round(v) for _n, v in top_prod],
        "mtd_label": mtd_label,
        "mtd": mtd_rev,
        "prev": prev_rev,
        "mtd_delta": mtd_delta,
        "total": sum(trend_values),
        "months": len(idxs),
        "fill_labels": [d.strftime("%d %b") for d in fdays],
        "fill_series": fill_series,
        "fill_today": fill_today,
        "fill_30": fill_30,
        "n_delivered_today": n_delivered_today,
        "short_today": dshort_today,
    }

    return render_template("reports/index.html", today=today,
                           n_today=len(confirmed_today), val_today=val_today,
                           n_pending=len(pending), n_backorders=len(backorders_open),
                           conv_rate=conv_rate, n_won=len(won), n_decided=len(decided),
                           n_at_risk=n_at_risk, snap=snap)


# ---------------------------------------------------------------------------
# Fulfilment report
# ---------------------------------------------------------------------------
def _fulfilment_data(frm, to):
    orders = db.session.scalars(
        db.select(SalesOrder).where(SalesOrder.status.in_(DELIVERED))).all()
    rows, missed = [], []
    units_ordered = units_delivered = 0.0
    lines_total = lines_full = lines_short = 0
    val_ordered = defaultdict(float)
    val_delivered = defaultdict(float)
    val_missed = defaultdict(float)
    for o in orders:
        fdate = ((o.delivered_at or o.fulfilled_at).date() if (o.delivered_at or o.fulfilled_at) else o.order_date)
        if not (frm <= fdate <= to):
            continue
        o_ord = o_del = 0.0
        o_missed_lines = 0
        for l in o.lines:
            lines_total += 1
            ordered = l.quantity or 0
            delivered = l.delivered_qty or 0
            units_ordered += ordered
            units_delivered += delivered
            o_ord += ordered
            o_del += delivered
            val_ordered[o.currency] += _money(l.ordered_total)
            val_delivered[o.currency] += _money(l.line_total)
            short = ordered - delivered
            if short > 1e-9 or l.availability == "not_delivered":
                lines_short += 1
                o_missed_lines += 1
                val_missed[o.currency] += _money(l.ordered_total) - _money(l.line_total)
                missed.append({
                    "order": o.number, "customer": o.customer.name,
                    "article": l.article_no, "description": l.description,
                    "ordered": ordered, "delivered": delivered, "short": short,
                    "status": l.availability, "currency": o.currency,
                    "value_short": _money(l.ordered_total) - _money(l.line_total)})
            else:
                lines_full += 1
        rows.append({"order": o.number, "customer": o.customer.name, "date": fdate,
                     "currency": o.currency, "ordered": o_ord, "delivered": o_del,
                     "missed_lines": o_missed_lines, "total": _money(o.total)})
    fill = (units_delivered / units_ordered * 100.0) if units_ordered else None
    summary = {"orders": len(rows), "lines": lines_total, "lines_full": lines_full,
               "lines_short": lines_short, "units_ordered": units_ordered,
               "units_delivered": units_delivered, "fill": fill,
               "val_ordered": val_ordered, "val_delivered": val_delivered,
               "val_missed": val_missed}
    return rows, missed, summary


@bp.route("/fulfilment")
def fulfilment():
    frm, to = _range()
    rows, missed, summary = _fulfilment_data(frm, to)
    if request.args.get("export") == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Missed items", f"{frm} to {to}"])
        w.writerow(["Order", "Customer", "Article", "Description", "Ordered",
                    "Delivered", "Short", "Status", "Currency", "Value short"])
        for m in missed:
            w.writerow([m["order"], m["customer"], m["article"], m["description"],
                        m["ordered"], m["delivered"], m["short"], m["status"],
                        m["currency"], f"{m['value_short']:.2f}"])
        return _csv(out, f"fulfilment_{frm}_{to}.csv")
    return render_template("reports/fulfilment.html", frm=frm, to=to,
                           rows=rows, missed=missed, summary=summary)


# ---------------------------------------------------------------------------
# Sales report
# ---------------------------------------------------------------------------
@bp.route("/sales")
def sales():
    """Sales report from the uploaded history: revenue by total, customer,
    category, product and salesperson, over a date range (UGX, net of credits)."""
    from models import Invoice, SalesHistory
    # Date range sticks (Stephan, 3 Aug 2026): default is the CURRENT MONTH
    # to date, and an explicitly chosen range is remembered in the session,
    # so switching groupings, segments or sub-tabs never asks for the dates
    # again. The old default was 2024-01-01 → today on every visit.
    from flask import session as _sess
    qf = _parse(request.args.get("from"))
    qt = _parse(request.args.get("to"))
    today_ = date.today()
    if qf or qt:
        frm = qf or today_.replace(day=1)
        to = qt or today_
        _sess["sales_range"] = [frm.isoformat(), to.isoformat()]
    else:
        saved = _sess.get("sales_range") or []
        frm = _parse(saved[0]) if len(saved) > 0 else None
        to = _parse(saved[1]) if len(saved) > 1 else None
        frm = frm or today_.replace(day=1)
        to = to or today_
    if to < frm:
        frm, to = to, frm
    group = request.args.get("group", "total")
    seg = request.args.get("segment", "all")
    cur = "UGX"

    def seg_ok(customer):
        s = (customer.segment or "customer") if customer else None
        if seg == "distributor":
            return s == "distributor"
        if seg == "direct":
            return customer is not None and s != "distributor"
        return True

    by_currency = defaultdict(lambda: {"orders": 0, "value": 0.0})
    by_customer = defaultdict(lambda: defaultdict(lambda: {"orders": 0, "value": 0.0}))
    by_rep = defaultdict(lambda: defaultdict(lambda: {"orders": 0, "value": 0.0}))
    by_category = defaultdict(lambda: defaultdict(lambda: {"orders": 0, "value": 0.0}))
    by_product = defaultdict(lambda: defaultdict(lambda: {"qty": 0.0, "value": 0.0, "desc": ""}))

    # invoice headers -> total / customer / category / salesperson (net UGX)
    n_invoices = 0
    for i in db.session.scalars(db.select(Invoice).where(
            Invoice.payment_status != "Reversed",
            Invoice.invoice_date.isnot(None))):
        if not (frm <= i.invoice_date <= to):
            continue
        if not seg_ok(i.customer):
            continue
        val = float(i.untaxed or 0)
        n_invoices += 1
        by_currency[cur]["orders"] += 1
        by_currency[cur]["value"] += val
        by_customer[cur][i.customer_name or "—"]["orders"] += 1
        by_customer[cur][i.customer_name or "—"]["value"] += val
        catn = i.customer.category.name if (i.customer and i.customer.category) else "Uncategorised"
        by_category[cur][catn]["orders"] += 1
        by_category[cur][catn]["value"] += val
        rep = i.salesperson or "—"
        by_rep[cur][rep]["orders"] += 1
        by_rep[cur][rep]["value"] += val

    # monthly product pivot -> product quantities & value (catalogue only)
    pmap = _product_labels()
    for s in db.session.scalars(db.select(SalesHistory).where(SalesHistory.month.isnot(None))):
        d = date(s.year, s.month, 1)
        if not (frm <= d <= to):
            continue
        if seg != "all" and not seg_ok(s.customer):
            continue
        lbl = pmap.get(s.product_id)
        if not lbl:
            continue
        cell = by_product[cur][lbl]
        cell["qty"] += float(s.quantity or 0)
        cell["value"] += float(s.revenue or 0)
        cell["desc"] = ""

    def sort_map(m):
        return {cur: sorted(d.items(), key=lambda kv: -kv[1].get("value", 0))
                for cur, d in m.items()}

    data = {"total": dict(by_currency), "customer": sort_map(by_customer),
            "product": sort_map(by_product), "rep": sort_map(by_rep),
            "category": sort_map(by_category)}

    if request.args.get("export") == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        seg_label = {"distributor": "distributors only", "direct": "direct customers only"}.get(seg, "all customers")
        w.writerow([f"Sales report {frm} to {to}", f"grouped by {group}", seg_label])
        if group == "total":
            w.writerow(["Currency", "Orders", "Value"])
            for cur, v in by_currency.items():
                w.writerow([cur, v["orders"], f"{v['value']:.2f}"])
        elif group == "product":
            w.writerow(["Currency", "Article", "Description", "Qty", "Value"])
            for cur, items in data["product"].items():
                for art, v in items:
                    w.writerow([cur, art, v["desc"], v["qty"], f"{v['value']:.2f}"])
        else:
            label = {"customer": "Customer", "rep": "Rep", "category": "Category"}.get(group, "Customer")
            w.writerow(["Currency", label, "Orders", "Value"])
            for cur, items in data[group].items():
                for name, v in items:
                    w.writerow([cur, name, v["orders"], f"{v['value']:.2f}"])
        return _csv(out, f"sales_{group}_{seg}_{frm}_{to}.csv")

    cat_max = {c: max((v["value"] for _n, v in items), default=0)
               for c, items in data["category"].items()}

    # ---- CEO dashboard on top (Stephan, 2 Aug 2026): channel view over the
    # same date range and segment filter. Channel = customer category, with
    # distributors split Export vs Local. Kilograms lead, value follows.
    import re as _re2
    from models import InvoiceLine, Product, Customer
    from services.revenue import export_customer_ids, dist_band
    from services.inventory_costing import parse_pack_weight_kg
    exp_ids = export_customer_ids()

    def _chan_of(c):
        if c is None:
            return "Other"
        if (c.segment or "customer") == "distributor":
            return f"{dist_band(c, exp_ids)} distributors"
        return (c.category.name if c.category else None) or "Other"

    # The channel cards render only on the By total view (3 Aug 2026,
    # Stephan: with the cards always on top, switching the grouping looked
    # like nothing changed). Skip the heavy line scan for the other views.
    show_channels = group == "total"
    prods_all = (db.session.scalars(db.select(Product)).all()
                 if show_channels else [])

    def _pwt(p):
        t = _re2.sub(r"^[^0-9]*", "", str(p.pack_size or ""))
        w = parse_pack_weight_kg(t)
        if w:
            return w
        return 1.0 if (p.unit_of_measure or "").strip().lower() == "kg" else None

    pw = {p.id: _pwt(p) for p in prods_all}
    pname_all = {p.id: p.description for p in prods_all}
    cust_chan = {c.id: _chan_of(c) for c in db.session.scalars(db.select(Customer))}

    chan_val = defaultdict(float)
    chan_orders = defaultdict(int)
    chan_kg = defaultdict(float)
    chan_cust = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))   # val, kg
    chan_prod = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))   # val, kg
    tot_val = tot_kg = 0.0

    for i in (db.session.scalars(db.select(Invoice).where(
            Invoice.payment_status != "Reversed",
            Invoice.invoice_date >= frm, Invoice.invoice_date <= to))
            if show_channels else []):
        if not seg_ok(i.customer):
            continue
        ch = cust_chan.get(i.customer_id, "Other")
        v = float(i.untaxed or 0)
        chan_val[ch] += v
        chan_orders[ch] += 1
        chan_cust[ch][i.customer_name or "—"][0] += v
        tot_val += v

    for cid, cname, pid, pnm, qty, amt in (db.session.execute(
            db.select(Invoice.customer_id, Invoice.customer_name,
                      InvoiceLine.product_id, InvoiceLine.product_name,
                      InvoiceLine.quantity, InvoiceLine.amount)
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .where(Invoice.payment_status != "Reversed",
                   Invoice.invoice_date >= frm, Invoice.invoice_date <= to))
            if show_channels else []):
        ch = cust_chan.get(cid, "Other")
        if seg == "distributor" and "distributors" not in ch:
            continue
        if seg == "direct" and (cid is None or "distributors" in ch):
            continue
        q = float(qty or 0)
        w = pw.get(pid)
        kgs = q * w if w else q
        a = float(amt or 0)
        label = pname_all.get(pid) or pnm or "—"
        chan_kg[ch] += kgs
        chan_prod[ch][label][0] += a
        chan_prod[ch][label][1] += kgs
        chan_cust[ch][cname or "—"][1] += kgs
        tot_kg += kgs

    channels = []
    for ch in chan_val.keys() | chan_kg.keys():
        tp = sorted(((n, v[0], v[1]) for n, v in chan_prod[ch].items()),
                    key=lambda r: -r[2])[:5]
        tc = sorted(((n, v[0], v[1]) for n, v in chan_cust[ch].items()),
                    key=lambda r: (-r[2], -r[1]))[:5]
        channels.append({"name": ch, "value": chan_val.get(ch, 0.0),
                         "orders": chan_orders.get(ch, 0),
                         "kg": chan_kg.get(ch, 0.0),
                         "top_products": tp, "top_customers": tc})
    channels.sort(key=lambda r: -r["kg"])

    return render_template("reports/sales.html", frm=frm, to=to, group=group,
                           data=data, n_orders=n_invoices, cat_max=cat_max, seg=seg,
                           channels=channels, tot_val=tot_val, tot_kg=tot_kg,
                           chart_chan=[c["name"] for c in channels],
                           chart_kg=[round(c["kg"]) for c in channels],
                           chart_val=[round(c["value"]) for c in channels])


# ---------------------------------------------------------------------------
# Offer conversion report
# ---------------------------------------------------------------------------
@bp.route("/offers")
def offers_report():
    frm, to = _range()
    offers = [o for o in db.session.scalars(db.select(Offer)).all()
              if frm <= (o.created_at.date() if o.created_at else date.today()) <= to]
    buckets = {"issued": 0, "converted": 0, "not_ordered": 0, "draft": 0, "archived": 0}
    val = defaultdict(lambda: defaultdict(float))
    for o in offers:
        buckets[o.status] = buckets.get(o.status, 0) + 1
        val[o.status][o.currency] += _money(o.total)
    decided = buckets["converted"] + buckets["not_ordered"]
    conv = (buckets["converted"] / decided * 100.0) if decided else None
    return render_template("reports/offers.html", frm=frm, to=to, buckets=buckets,
                           val=val, conv=conv, n=len(offers))


@bp.route("/feedback")
def feedback():
    try:
        days = max(1, min(365, int(request.args.get("days", 90))))
    except ValueError:
        days = 90
    since = date.today() - timedelta(days=days - 1)
    all_rated = [o for o in db.session.scalars(db.select(SalesOrder))
                 if o.rating and o.rated_at]
    rated = [o for o in all_rated if o.rated_at.date() >= since]
    n = len(rated)
    avg = round(sum(o.rating for o in rated) / n, 2) if n else None
    dist = {star: sum(1 for o in rated if o.rating == star) for star in range(1, 6)}
    pct_poor = round(sum(1 for o in rated if o.rating <= 2) / n * 100) if n else 0
    pct_great = round(dist[5] / n * 100) if n else 0

    # average by month (last 6 months, all-time pool)
    monthly = defaultdict(list)
    for o in all_rated:
        monthly[o.rated_at.strftime("%Y-%m")].append(o.rating)
    months = sorted(monthly)[-6:]
    month_labels = [datetime.strptime(m, "%Y-%m").strftime("%b %y") for m in months]
    month_avg = [round(sum(monthly[m]) / len(monthly[m]), 2) for m in months]

    low = sorted([o for o in rated if o.rating <= 2],
                 key=lambda o: o.rated_at, reverse=True)
    comments = sorted([o for o in rated if o.rating_comment],
                      key=lambda o: o.rated_at, reverse=True)[:50]

    if request.args.get("export") == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow([f"Customer feedback last {days} days", f"avg {avg}", f"{n} rated"])
        w.writerow(["Order", "Customer", "Rating", "Comment", "Rated on"])
        for o in sorted(rated, key=lambda x: x.rated_at, reverse=True):
            w.writerow([o.number, o.customer.name if o.customer else "", o.rating,
                        o.rating_comment or "", o.rated_at.strftime("%Y-%m-%d")])
        return _csv(out, f"feedback_{days}d.csv")

    return render_template("reports/feedback.html", days=days, n=n, avg=avg,
                           dist=dist, pct_poor=pct_poor, pct_great=pct_great,
                           month_labels=month_labels, month_avg=month_avg,
                           low=low, comments=comments)


@bp.route("/history")
def sales_history():
    """Invoice-based sales history (dated): monthly trend, top customers,
    salesperson performance and receivables. Product mix comes from the pivot
    (sales_history) since invoices carry no product lines. Figures: untaxed UGX,
    excluding reversed invoices."""
    from models import Invoice, SalesHistory
    invs = db.session.scalars(db.select(Invoice)).all()
    if not invs:
        return render_template("reports/sales_history.html", has_data=False)

    def rev_of(i):
        if i.payment_status != "Reversed":
            return float(i.untaxed or 0)
        return 0.0

    monthly = defaultdict(float)
    year_tot = defaultdict(lambda: {"rev": 0.0, "n": 0})
    cust_year = defaultdict(lambda: defaultdict(float))
    sp = defaultdict(lambda: {"rev": 0.0, "n": 0})
    owe = defaultdict(float)
    outstanding_total = 0.0
    owe_count = 0
    total_rev = matched_rev = 0.0
    for i in invs:
        d = i.invoice_date
        r = rev_of(i)
        if d and r:
            monthly[d.strftime("%Y-%m")] += r
            year_tot[d.year]["rev"] += r
            year_tot[d.year]["n"] += 1
            cust_year[i.customer_name][d.year] += r
            if i.salesperson:
                sp[i.salesperson]["rev"] += r
                sp[i.salesperson]["n"] += 1
            total_rev += r
            if i.customer_id:
                matched_rev += r
        if (i.payment_status in ("Not Paid", "Partially Paid", "In Payment")
                and float(i.total or 0) > 0):   # exclude credit notes
            t = float(i.total or 0)
            owe[i.customer_name] += t
            outstanding_total += t
            owe_count += 1

    years = sorted(year_tot)
    months = sorted(monthly)
    month_labels = [datetime.strptime(m, "%Y-%m").strftime("%b %y") for m in months]
    month_values = [round(monthly[m]) for m in months]

    def top_cy(d, n=15):
        items = [(name, sum(by.values()), dict(by)) for name, by in d.items()]
        items.sort(key=lambda t: t[1], reverse=True)
        return items[:n]

    top_customers = top_cy(cust_year)
    top_sales = sorted(((k, v["rev"], v["n"]) for k, v in sp.items()),
                       key=lambda t: t[1], reverse=True)[:15]
    top_owing = sorted(owe.items(), key=lambda kv: kv[1], reverse=True)[:15]

    # product mix from the pivot import, consolidated to catalogue products
    # (off-catalogue / unlinked rows are omitted)
    pmap = _product_labels()
    sh = db.session.scalars(db.select(SalesHistory)).all()
    prod_year = defaultdict(lambda: defaultdict(float))
    prod_qty2 = defaultdict(float)
    for s in sh:
        lbl = pmap.get(s.product_id)
        if lbl:
            prod_year[lbl][s.year] += float(s.revenue or 0)
            prod_qty2[lbl] += float(s.quantity or 0)
    prod_years = sorted({s.year for s in sh})
    top_products = [(name, total, prod_qty2.get(name, 0.0), by)
                    for name, total, by in top_cy(prod_year)]

    if request.args.get("export") == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Sales history by customer (invoiced untaxed UGX, excl reversed)"])
        w.writerow(["Customer"] + [str(y) for y in years] + ["Total"])
        for name, by in sorted(cust_year.items(), key=lambda kv: sum(kv[1].values()), reverse=True):
            w.writerow([name] + [f"{by.get(y,0):.0f}" for y in years] + [f"{sum(by.values()):.0f}"])
        return _csv(out, "sales_history_by_customer.csv")

    return render_template(
        "reports/sales_history.html", has_data=True, years=years,
        year_tot=year_tot, month_labels=month_labels, month_values=month_values,
        top_customers=top_customers, top_sales=top_sales,
        top_products=top_products, prod_years=prod_years,
        outstanding_total=outstanding_total, owe_count=owe_count, top_owing=top_owing,
        total_rev=total_rev, coverage=(matched_rev / total_rev * 100 if total_rev else 0))


@bp.route("/top90")
def top90():
    """Top performers over the last 90 days — products, customers, reps,
    distributors — plus a month-by-month revenue comparison (Stephan,
    28 Jul 2026: the CEO dashboard tops show the last 3 days; the 90-day
    view lives here). Ownership rules match the dashboard: invoices own
    every day up to the latest invoice date, app orders own the days after;
    for the monthly bars, invoices own whole months up to their latest
    month, app orders the months beyond."""
    from models import Invoice, InvoiceLine, Product
    today = date.today()
    win90 = today - timedelta(days=89)
    last_inv_date = db.session.scalar(db.select(db.func.max(Invoice.invoice_date)))
    inv_cutover = (last_inv_date.year * 12 + last_inv_date.month) if last_inv_date else 0
    pmap = {p.id: p.description for p in db.session.scalars(db.select(Product))}

    from services.revenue import export_customer_ids, dist_band
    exp_ids = export_customer_ids()

    cust = defaultdict(float)
    reps = defaultdict(float)
    dist = defaultdict(float)
    dist_bands = {}                 # distributor name -> 'Export' | 'Local'
    prod_v = defaultdict(float)
    prod_q = defaultdict(float)
    monthly = defaultdict(float)

    for i in db.session.scalars(db.select(Invoice).where(
            Invoice.payment_status != "Reversed",
            Invoice.invoice_date.isnot(None))):
        v = float(i.untaxed or 0)
        monthly[i.invoice_date.year * 12 + i.invoice_date.month] += v
        if i.invoice_date >= win90:
            cust[i.customer_name or "—"] += v
            if i.salesperson:
                reps[i.salesperson] += v
            if i.customer and (i.customer.segment or "") == "distributor":
                dist[i.customer_name or "—"] += v
                dist_bands[i.customer_name or "—"] = dist_band(i.customer, exp_ids)
    for _d, pid, pname, amt, qty in db.session.execute(
            db.select(Invoice.invoice_date, InvoiceLine.product_id,
                      InvoiceLine.product_name, InvoiceLine.amount,
                      InvoiceLine.quantity)
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .where(Invoice.invoice_date >= win90,
                   Invoice.payment_status != "Reversed")):
        label = pmap.get(pid) or pname or "—"
        prod_v[label] += float(amt or 0)
        prod_q[label] += float(qty or 0)

    orders = db.session.scalars(db.select(SalesOrder)).all()
    for o in orders:
        if o.status not in CONFIRMED or not o.order_date:
            continue
        idx = o.order_date.year * 12 + o.order_date.month
        v = net_ugx(o)
        if idx > inv_cutover:
            monthly[idx] += v
        if o.order_date < win90:
            continue
        if last_inv_date and o.order_date <= last_inv_date:
            continue
        c = o.customer
        cust[c.name if c else "—"] += v
        sreps = [r for r in (c.reps if c else []) if r.role in ("rep", "telesales")]
        reps[sreps[0].full_name if sreps else "Unassigned"] += v
        if c and (c.segment or "") == "distributor":
            dist[c.name] += v
            dist_bands[c.name] = dist_band(c, exp_ids)
        net = float(o.subtotal or 0)
        rate = (v / net) if net else (1.0 if (o.currency or "UGX") == "UGX" else 0.0)
        for l in o.lines:
            nm = (l.product.description if getattr(l, "product", None) else None) \
                or l.description or "—"
            prod_v[nm] += float(l.line_total or 0) * rate
            prod_q[nm] += float(l.delivered_qty or 0)

    # Month-by-month bars: the last 13 months that have data, oldest first.
    cur_idx = today.year * 12 + today.month
    idxs = [i for i in range(cur_idx - 12, cur_idx + 1) if monthly.get(i)]
    month_labels = [date((i - 1) // 12, (i - 1) % 12 + 1, 1).strftime("%b %y")
                    for i in idxs]
    month_values = [round(monthly[i]) for i in idxs]

    # ---- Last-3-months comparison per product / customer / rep ----
    # Month ownership: sales_history owns product months up to its latest
    # month; invoice lines own months after that; app orders own months past
    # the latest invoice. Customers and reps come from invoice headers, then
    # app orders past the invoice cutover.
    from models import SalesHistory
    m3 = [cur_idx - 2, cur_idx - 1, cur_idx]
    m3_labels = [date((i - 1) // 12, (i - 1) % 12 + 1, 1).strftime("%B %Y")
                 for i in m3]
    hist_cutover = db.session.scalar(
        db.select(db.func.max(SalesHistory.year * 12 + SalesHistory.month))) or 0

    prod_m = defaultdict(lambda: defaultdict(float))
    cust_m = defaultdict(lambda: defaultdict(float))
    rep_m = defaultdict(lambda: defaultdict(float))

    for s in db.session.scalars(db.select(SalesHistory).where(
            SalesHistory.month.isnot(None))):
        idx = s.year * 12 + s.month
        if idx in m3 and idx <= hist_cutover:
            label = pmap.get(s.product_id)
            if label:
                prod_m[label][idx] += float(s.revenue or 0)
    lo3 = date((m3[0] - 1) // 12, (m3[0] - 1) % 12 + 1, 1)
    for d, pid, pname, amt in db.session.execute(
            db.select(Invoice.invoice_date, InvoiceLine.product_id,
                      InvoiceLine.product_name, InvoiceLine.amount)
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .where(Invoice.invoice_date >= lo3,
                   Invoice.payment_status != "Reversed")):
        idx = d.year * 12 + d.month
        if idx in m3 and hist_cutover < idx <= inv_cutover:
            prod_m[pmap.get(pid) or pname or "—"][idx] += float(amt or 0)
    for i in db.session.scalars(db.select(Invoice).where(
            Invoice.payment_status != "Reversed",
            Invoice.invoice_date >= lo3)):
        idx = i.invoice_date.year * 12 + i.invoice_date.month
        if idx in m3 and idx <= inv_cutover:
            v = float(i.untaxed or 0)
            cust_m[i.customer_name or "—"][idx] += v
            if i.salesperson:
                rep_m[i.salesperson][idx] += v
    for o in orders:
        if o.status not in CONFIRMED or not o.order_date:
            continue
        idx = o.order_date.year * 12 + o.order_date.month
        if idx not in m3 or idx <= inv_cutover:
            continue
        v = net_ugx(o)
        c = o.customer
        cust_m[c.name if c else "—"][idx] += v
        sreps = [r for r in (c.reps if c else []) if r.role in ("rep", "telesales")]
        rep_m[sreps[0].full_name if sreps else "Unassigned"][idx] += v
        net = float(o.subtotal or 0)
        rate = (v / net) if net else (1.0 if (o.currency or "UGX") == "UGX" else 0.0)
        for l in o.lines:
            nm = (l.product.description if getattr(l, "product", None) else None) \
                or l.description or "—"
            prod_m[nm][idx] += float(l.line_total or 0) * rate

    def chart3(dd, n=10):
        top = sorted(dd.items(), key=lambda kv: sum(kv[1].values()),
                     reverse=True)[:n]
        return {"labels": [k for k, _v in top],
                "series": [[round(v.get(i, 0.0)) for _k, v in top] for i in m3]}

    return render_template(
        "reports/top90.html", win90=win90, today=today,
        top_products=sorted(((k, v, prod_q.get(k, 0.0)) for k, v in prod_v.items()),
                            key=lambda kv: kv[1], reverse=True)[:15],
        top_customers=sorted(cust.items(), key=lambda kv: kv[1], reverse=True)[:15],
        rep_leaders=sorted(reps.items(), key=lambda kv: kv[1], reverse=True)[:15],
        top_distributors=sorted(
            ((k, v, dist_bands.get(k, "Local")) for k, v in dist.items()),
            key=lambda kv: kv[1], reverse=True)[:10],
        dist_exp=sum(v for k, v in dist.items() if dist_bands.get(k) == "Export"),
        dist_loc=sum(v for k, v in dist.items() if dist_bands.get(k) != "Export"),
        month_labels=month_labels, month_values=month_values,
        m3_labels=m3_labels, chart_products=chart3(prod_m),
        chart_customers=chart3(cust_m), chart_reps=chart3(rep_m))


@bp.route("/all-time")
def all_time():
    """All-time performance: overall totals, best customers, products, months
    and salespeople across the full sales history."""
    from models import SalesHistory, Invoice
    sh = db.session.scalars(db.select(SalesHistory)).all()
    if not sh:
        return render_template("reports/all_time.html", has_data=False)

    pmap = _product_labels()
    year_tot = defaultdict(float)
    month_tot = defaultdict(float)
    cust = defaultdict(float)
    prod = defaultdict(float)
    prod_q = defaultdict(float)
    total = 0.0
    for s in sh:
        rev = float(s.revenue or 0)
        total += rev
        year_tot[s.year] += rev
        if s.month:
            month_tot[(s.year, s.month)] += rev
        cust[s.customer_name] += rev
        lbl = pmap.get(s.product_id)
        if lbl:
            prod[lbl] += rev
            prod_q[lbl] += float(s.quantity or 0)

    months = sorted(month_tot)
    best_month = max(month_tot.items(), key=lambda kv: kv[1]) if month_tot else None
    avg_month = (sum(month_tot.values()) / len(month_tot)) if month_tot else 0
    top_customers = sorted(cust.items(), key=lambda kv: kv[1], reverse=True)[:15]
    top_products = sorted(((k, v, prod_q.get(k, 0.0)) for k, v in prod.items()),
                          key=lambda kv: kv[1], reverse=True)[:15]

    # salespeople + invoice/credit totals from invoices
    sp = defaultdict(float)
    n_inv = n_cn = 0
    cn_total = 0.0
    for i in db.session.scalars(db.select(Invoice)):
        if i.payment_status == "Reversed":
            continue
        v = float(i.untaxed or 0)
        if (i.number or "").startswith("RINV"):
            n_cn += 1
            cn_total += v
        else:
            n_inv += 1
            if i.salesperson:
                sp[i.salesperson] += v
    top_sales = sorted(sp.items(), key=lambda kv: kv[1], reverse=True)[:10]

    return render_template(
        "reports/all_time.html", has_data=True, total=total,
        years=sorted(year_tot), year_tot=year_tot,
        month_labels=[datetime(y, m, 1).strftime("%b %y") for y, m in months],
        month_values=[round(month_tot[k]) for k in months],
        best_month=(datetime(best_month[0][0], best_month[0][1], 1).strftime("%B %Y"),
                    best_month[1]) if best_month else None,
        avg_month=avg_month, n_months=len(month_tot),
        top_customers=top_customers, top_products=top_products, top_sales=top_sales,
        n_invoices=n_inv, n_credit=n_cn, cn_total=cn_total)


@bp.route("/products-month")
def products_monthly():
    """Products sold per month (from the monthly pivot): top products with a
    recent-vs-previous trend, plus a per-product monthly drill-down."""
    from models import SalesHistory
    rows = db.session.scalars(db.select(SalesHistory).where(
        SalesHistory.month.isnot(None))).all()
    if not rows:
        return render_template("reports/products_month.html", has_data=False)

    latest = max(r.year * 100 + r.month for r in rows)
    ly, lm = latest // 100, latest % 100
    latest_idx = ly * 12 + lm

    def idx(r):
        return r.year * 12 + r.month

    sel = (request.args.get("product") or "").strip() or None
    pmap = _product_labels()   # consolidate to catalogue products; omit unlinked

    prod = defaultdict(lambda: {"rev": 0.0, "qty": 0.0, "last": 0,
                                "recent": 0.0, "prev": 0.0})
    for r in rows:
        lbl = pmap.get(r.product_id)
        if not lbl:
            continue                      # off-catalogue: omitted
        rev = float(r.revenue or 0)
        p = prod[lbl]
        p["rev"] += rev
        p["qty"] += float(r.quantity or 0)
        if rev > 0:
            p["last"] = max(p["last"], r.year * 100 + r.month)
        gap = latest_idx - idx(r)
        if 0 <= gap < 3:
            p["recent"] += rev
        elif 3 <= gap < 6:
            p["prev"] += rev

    def trend(p):
        rec, prv = p["recent"], p["prev"]
        if rec == 0 and prv > 0:
            return "stopped"
        if prv > 0 and rec < prv * 0.6:
            return "down"
        if rec > prv * 1.4 and prv > 0:
            return "up"
        if prv == 0 and rec > 0:
            return "new"
        return "steady"

    items = []
    for name, p in prod.items():
        ly2, lm2 = p["last"] // 100, p["last"] % 100
        last_label = datetime(ly2, lm2, 1).strftime("%b %Y") if p["last"] else "—"
        items.append({"name": name, "rev": p["rev"], "qty": p["qty"],
                      "last": last_label, "trend": trend(p)})
    items.sort(key=lambda x: x["rev"], reverse=True)
    products = items[:60]
    n_stopped = sum(1 for x in items if x["trend"] == "stopped")
    all_names = sorted(prod.keys())

    # Product drill (reworked 3 Aug 2026, Stephan): months side by side in
    # kg with the month-on-month change, customer performance underneath
    # ranked by kg bought. Merges the invoice months on top of the pivot,
    # so the drill runs to the latest upload, not the pivot cutover.
    drill = None
    if sel and sel in prod:
        from models import Invoice, InvoiceLine
        inv_months = _invoice_month_set()
        pw = _pack_weights()
        sel_ids = {pid for pid, lbl2 in pmap.items() if lbl2 == sel}
        latest_all = max([latest_idx] +
                         [y * 12 + m for (y, m) in inv_months] or [latest_idx])
        win_start = latest_all - 11          # last 12 months window

        sm = defaultdict(lambda: {"kg": 0.0, "val": 0.0})
        sc = {}

        def _cust(name, cid=None):
            c = sc.get(name)
            if c is None:
                c = sc[name] = {"name": name, "cid": cid, "kg": 0.0,
                                "val": 0.0, "kg12": 0.0, "val12": 0.0}
            if cid and not c["cid"]:
                c["cid"] = cid
            return c

        def _add(y, m, cname, cid, kgs, v):
            sm[(y, m)]["kg"] += kgs
            sm[(y, m)]["val"] += v
            c = _cust(cname or "—", cid)
            c["kg"] += kgs
            c["val"] += v
            if y * 12 + m >= win_start:
                c["kg12"] += kgs
                c["val12"] += v

        for r in rows:
            if pmap.get(r.product_id) != sel or (r.year, r.month) in inv_months:
                continue
            w = pw.get(r.product_id)
            q = float(r.quantity or 0)
            _add(r.year, r.month, r.customer_name, r.customer_id,
                 q * w if w else q, float(r.revenue or 0))
        if sel_ids:
            for d, cid, cname, pid, qty, amt in db.session.execute(
                    db.select(Invoice.invoice_date, Invoice.customer_id,
                              Invoice.customer_name, InvoiceLine.product_id,
                              InvoiceLine.quantity, InvoiceLine.amount)
                    .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
                    .where(Invoice.payment_status != "Reversed",
                           Invoice.invoice_date.isnot(None),
                           InvoiceLine.product_id.in_(sel_ids))):
                w = pw.get(pid)
                q = float(qty or 0)
                _add(d.year, d.month, cname, cid,
                     q * w if w else q, float(amt or 0))

        ms = sorted(sm)
        month_cols = []
        for (y, m) in ms[-13:]:              # side-by-side table
            pk = (y - 1, 12) if m == 1 else (y, m - 1)
            p2 = sm.get(pk)
            cell = sm[(y, m)]
            month_cols.append({
                "label": datetime(y, m, 1).strftime("%b %y"),
                "kg": cell["kg"], "val": cell["val"],
                "kg_d": ((cell["kg"] - p2["kg"]) / p2["kg"] * 100.0)
                        if p2 and p2["kg"] else None,
            })
        cust_rows = sorted(sc.values(),
                           key=lambda c: (-c["kg12"], -c["kg"]))
        drill = {
            "name": sel,
            "labels": [datetime(y, m, 1).strftime("%b %y") for y, m in ms],
            "rev": [round(sm[k]["val"]) for k in ms],
            "kg": [round(sm[k]["kg"]) for k in ms],
            "months": month_cols,
            "customers": cust_rows[:30],
            "n_cust": len(cust_rows),
            "win_label": _idx_to_date(win_start).strftime("%b %Y"),
        }

    latest_label = datetime(ly, lm, 1).strftime("%b %Y")
    return render_template("reports/products_month.html", has_data=True,
                           products=products, all_names=all_names, drill=drill,
                           n_stopped=n_stopped, latest_label=latest_label)


# ---------------------------------------------------------------------------
# Sales by month — month-on-month comparison with a per-month drill-down
# (Stephan, 3 Aug 2026). One row per month, kilograms lead, value follows.
# Clicking a month opens customer, rep and distributor performance for the
# month, each against the previous month. Months covered by uploaded
# invoices aggregate the invoice lines; older months come from the monthly
# history pivot, which carries no salesperson — rep performance starts
# with the first invoice month.
# ---------------------------------------------------------------------------
def _pack_weights():
    """product_id -> kg per unit (None when unknown; callers count 1 unit
    = 1 kg and flag the estimate, same convention as the Sales dashboard)."""
    import re as _re
    from models import Product
    from services.inventory_costing import parse_pack_weight_kg
    w_map = {}
    for p in db.session.scalars(db.select(Product)):
        t = _re.sub(r"^[^0-9]*", "", str(p.pack_size or ""))
        w = parse_pack_weight_kg(t)
        if not w:
            w = 1.0 if (p.unit_of_measure or "").strip().lower() == "kg" else None
        w_map[p.id] = w
    return w_map


def _invoice_month_set():
    """(year, month) pairs covered by uploaded invoices. Those months
    aggregate from invoices; every other month falls back to the pivot."""
    from models import Invoice
    return {(d.year, d.month) for (d,) in db.session.execute(
        db.select(Invoice.invoice_date).distinct().where(
            Invoice.invoice_date.isnot(None),
            Invoice.payment_status != "Reversed"))}


def _cust_dist_info():
    """customer_id -> distributor flag + Export/Local band."""
    from models import Customer
    from services.revenue import export_customer_ids
    exp_ids = export_customer_ids()
    return {cid: {"dist": (seg or "customer") == "distributor",
                  "band": "Export" if cid in exp_ids else "Local"}
            for cid, seg in db.session.execute(
                db.select(Customer.id, Customer.segment))}


def _month_perf(year, month, pw, cust_info, inv_months):
    """One month aggregated: totals plus per-customer, per-rep and
    per-distributor kg/value."""
    from models import Invoice, InvoiceLine, SalesHistory
    out = {"kg": 0.0, "val": 0.0, "inv": 0, "unknown_packs": 0,
           "customers": {}, "reps": {}, "dists": {},
           "source": "invoices" if (year, month) in inv_months else "history"}

    def cell(store, key, cid=None):
        c = store.get(key)
        if c is None:
            c = store[key] = {"kg": 0.0, "val": 0.0, "inv": 0, "cid": cid}
        if cid and not c["cid"]:
            c["cid"] = cid
        return c

    if out["source"] == "invoices":
        m_start = date(year, month, 1)
        m_next = date(year + (month == 12), month % 12 + 1, 1)
        for cid, cname, rep, untaxed in db.session.execute(
                db.select(Invoice.customer_id, Invoice.customer_name,
                          Invoice.salesperson, Invoice.untaxed)
                .where(Invoice.payment_status != "Reversed",
                       Invoice.invoice_date >= m_start,
                       Invoice.invoice_date < m_next)):
            v = float(untaxed or 0)
            out["val"] += v
            out["inv"] += 1
            c = cell(out["customers"], cname or "—", cid)
            c["val"] += v
            c["inv"] += 1
            r = cell(out["reps"], (rep or "").strip() or "—")
            r["val"] += v
            r["inv"] += 1
            info = cust_info.get(cid)
            if info and info["dist"]:
                d = cell(out["dists"], cname or "—", cid)
                d["val"] += v
                d["inv"] += 1
                d["band"] = info["band"]
        for cid, cname, rep, pid, qty in db.session.execute(
                db.select(Invoice.customer_id, Invoice.customer_name,
                          Invoice.salesperson, InvoiceLine.product_id,
                          InvoiceLine.quantity)
                .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
                .where(Invoice.payment_status != "Reversed",
                       Invoice.invoice_date >= m_start,
                       Invoice.invoice_date < m_next)):
            q = float(qty or 0)
            w = pw.get(pid)
            kgs = q * w if w else q
            if not w:
                out["unknown_packs"] += 1
            out["kg"] += kgs
            cell(out["customers"], cname or "—", cid)["kg"] += kgs
            cell(out["reps"], (rep or "").strip() or "—")["kg"] += kgs
            info = cust_info.get(cid)
            if info and info["dist"]:
                d = cell(out["dists"], cname or "—", cid)
                d["kg"] += kgs
                d["band"] = info["band"]
    else:
        for r in db.session.scalars(db.select(SalesHistory).where(
                SalesHistory.year == year, SalesHistory.month == month)):
            v = float(r.revenue or 0)
            q = float(r.quantity or 0)
            w = pw.get(r.product_id)
            kgs = q * w if w else q
            if not w:
                out["unknown_packs"] += 1
            out["val"] += v
            out["kg"] += kgs
            c = cell(out["customers"], r.customer_name or "—", r.customer_id)
            c["val"] += v
            c["kg"] += kgs
            info = cust_info.get(r.customer_id)
            if info and info["dist"]:
                d = cell(out["dists"], r.customer_name or "—", r.customer_id)
                d["val"] += v
                d["kg"] += kgs
                d["band"] = info["band"]
    return out


@bp.route("/sales-month")
def sales_by_month():
    """One row per month: kg, net UGX and the month-on-month change.
    Each month links to its detail page."""
    from models import Invoice, InvoiceLine, SalesHistory
    try:
        months_back = int(request.args.get("months") or 13)
    except ValueError:
        months_back = 13
    months_back = min(max(months_back, 3), 36)
    today = date.today()
    end_idx = today.year * 12 + today.month
    start_idx = end_idx - months_back        # one extra month for the first delta

    pw = _pack_weights()
    inv_months = _invoice_month_set()
    agg = defaultdict(lambda: {"kg": 0.0, "val": 0.0, "inv": 0})

    m_from = _idx_to_date(start_idx)
    for d, untaxed in db.session.execute(
            db.select(Invoice.invoice_date, Invoice.untaxed).where(
                Invoice.payment_status != "Reversed",
                Invoice.invoice_date >= m_from)):
        idx = d.year * 12 + d.month
        if idx <= end_idx:
            agg[idx]["val"] += float(untaxed or 0)
            agg[idx]["inv"] += 1
    for d, pid, qty in db.session.execute(
            db.select(Invoice.invoice_date, InvoiceLine.product_id,
                      InvoiceLine.quantity)
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .where(Invoice.payment_status != "Reversed",
                   Invoice.invoice_date >= m_from)):
        idx = d.year * 12 + d.month
        if idx <= end_idx:
            q = float(qty or 0)
            w = pw.get(pid)
            agg[idx]["kg"] += q * w if w else q
    for r in db.session.scalars(db.select(SalesHistory).where(
            SalesHistory.month.isnot(None))):
        idx = r.year * 12 + r.month
        if not (start_idx <= idx <= end_idx) or (r.year, r.month) in inv_months:
            continue
        agg[idx]["val"] += float(r.revenue or 0)
        q = float(r.quantity or 0)
        w = pw.get(r.product_id)
        agg[idx]["kg"] += q * w if w else q

    rows = []
    for idx in range(end_idx, start_idx, -1):
        a = agg.get(idx, {"kg": 0.0, "val": 0.0, "inv": 0})
        p = agg.get(idx - 1)
        d = _idx_to_date(idx)
        rows.append({
            "year": d.year, "month": d.month,
            "label": d.strftime("%B %Y"),
            "kg": a["kg"], "val": a["val"], "inv": a["inv"],
            "kg_d": ((a["kg"] - p["kg"]) / p["kg"] * 100.0)
                    if p and p["kg"] else None,
            "val_d": ((a["val"] - p["val"]) / p["val"] * 100.0)
                     if p and p["val"] else None,
            "current": idx == end_idx,
            "source": "invoices" if (d.year, d.month) in inv_months else "history",
        })

    chart = list(reversed(rows))
    return render_template("reports/sales_month.html", rows=rows,
                           months_back=months_back,
                           chart_labels=[r["label"] for r in chart],
                           chart_kg=[round(r["kg"]) for r in chart],
                           chart_val=[round(r["val"]) for r in chart])


@bp.route("/sales-month/<int:year>/<int:month>")
def sales_month_detail(year, month):
    """One month in detail: customer, rep and distributor performance,
    ranked by kg, each against the previous month."""
    if not (2015 <= year <= 2100 and 1 <= month <= 12):
        abort(404)
    pw = _pack_weights()
    cust_info = _cust_dist_info()
    inv_months = _invoice_month_set()
    cur = _month_perf(year, month, pw, cust_info, inv_months)
    py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
    prv = _month_perf(py, pm, pw, cust_info, inv_months)

    # A month with no data at all (e.g. the current month before its first
    # invoice upload) must NOT flood the tables with last month's names at
    # zero — compare against nothing and show the empty notice instead
    # (Stephan, 3 Aug 2026).
    empty_month = not (cur["kg"] or cur["val"] or cur["inv"])
    if empty_month:
        prv_cmp = {"customers": {}, "reps": {}, "dists": {}}
    else:
        prv_cmp = prv

    def table(cur_map, prv_map, top=None):
        items = []
        for name, c in cur_map.items():
            p = prv_map.get(name)
            items.append({
                "name": name, "cid": c.get("cid"),
                "kg": c["kg"], "val": c["val"], "inv": c.get("inv", 0),
                "prev_kg": p["kg"] if p else 0.0,
                "kg_d": ((c["kg"] - p["kg"]) / p["kg"] * 100.0)
                        if p and p["kg"] else None,
                "band": c.get("band"),
                "share": (c["kg"] / cur["kg"] * 100.0) if cur["kg"] else None,
                "gone": False,
            })
        for name, p in prv_map.items():
            if name not in cur_map and p["kg"] > 0:
                items.append({"name": name, "cid": p.get("cid"), "kg": 0.0,
                              "val": 0.0, "inv": 0, "prev_kg": p["kg"],
                              "kg_d": -100.0, "band": p.get("band"),
                              "share": 0.0, "gone": True})
        items.sort(key=lambda r: (r["gone"], -r["kg"], -r["prev_kg"]))
        return (items[:top], len(items)) if top else (items, len(items))

    customers, n_cust = table(cur["customers"], prv_cmp["customers"], top=60)
    reps, _ = table(cur["reps"], prv_cmp["reps"])
    dists, _ = table(cur["dists"], prv_cmp["dists"])
    exp_rows = [r for r in dists if r["band"] == "Export"]
    loc_rows = [r for r in dists if r["band"] != "Export"]

    def subtotal(rs):
        kg = sum(r["kg"] for r in rs)
        pk = sum(r["prev_kg"] for r in rs)
        return {"kg": kg, "val": sum(r["val"] for r in rs), "prev_kg": pk,
                "kg_d": ((kg - pk) / pk * 100.0) if pk else None}

    return render_template(
        "reports/sales_month_detail.html",
        year=year, month=month,
        label=date(year, month, 1).strftime("%B %Y"),
        prev_label=date(py, pm, 1).strftime("%B %Y"),
        cur=cur, prv=prv,
        kg_d=((cur["kg"] - prv["kg"]) / prv["kg"] * 100.0) if prv["kg"] else None,
        val_d=((cur["val"] - prv["val"]) / prv["val"] * 100.0) if prv["val"] else None,
        customers=customers, n_cust=n_cust, reps=reps, empty_month=empty_month,
        exp_rows=exp_rows, loc_rows=loc_rows,
        exp_tot=subtotal(exp_rows), loc_tot=subtotal(loc_rows),
        n_active=sum(1 for c in cur["customers"].values() if c["kg"] > 0))


def _csv(out, filename):
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


# ---------------------------------------------------------------------------
# Customer insights — product mix, trends, and what they've stopped buying
# ---------------------------------------------------------------------------
def _trend(recent, prev):
    if recent > 0 and prev == 0:
        return "new"
    if recent == 0 and prev > 0:
        return "stopped"
    if prev > 0 and recent < prev * 0.6:
        return "down"
    if recent > prev * 1.4:
        return "up"
    return "steady"


# --- historical-data helpers (reports run off the uploaded sales history) ----
def _sh_latest_idx():
    from models import SalesHistory
    return db.session.scalar(
        db.select(db.func.max(SalesHistory.year * 12 + SalesHistory.month))) or 0


def _idx_to_date(idx):
    y = (idx - 1) // 12
    m = (idx - 1) % 12 + 1
    return date(y, m, 1)


def _win_months(days):
    return max(1, round(days / 30))


def _product_labels():
    """Map catalogue product_id -> description, for consolidating history
    product variants and omitting off-catalogue (unlinked) rows."""
    from models import Product
    return {p.id: p.description for p in db.session.scalars(db.select(Product))}


def _customer_orders(customer_id):
    return [o for o in db.session.scalars(
        db.select(SalesOrder).filter_by(customer_id=customer_id))
        if o.status in CONFIRMED]


@bp.route("/customer")
def customer_insights():
    customers = db.session.scalars(
        db.select(Customer).where(Customer.archived.is_(False))
        .order_by(Customer.name)).all()
    cid = request.args.get("customer_id", type=int)
    days = request.args.get("days", default=90, type=int)
    today = date.today()
    r_start = today - timedelta(days=days)
    p_start = today - timedelta(days=2 * days)

    selected = db.session.get(Customer, cid) if cid else None
    products, summary = [], None
    hist_years, hist_products, hist_prod_years, hist_outstanding = {}, [], [], 0.0
    hist_month_labels, hist_month_values, hist_n_lapsed, hist_latest = [], [], 0, None
    if selected:
        from models import SalesHistory as _SH
        wm = _win_months(days)
        L = _sh_latest_idx()
        pmap = _product_labels()
        prod = {}
        recent_value = 0.0
        recent_months = set()
        for s in db.session.scalars(db.select(_SH).filter_by(customer_id=selected.id)):
            if not s.month:
                continue
            lbl = pmap.get(s.product_id)
            if not lbl:
                continue                  # off-catalogue: omitted
            idx = s.year * 12 + s.month
            rev = float(s.revenue or 0)
            qty = float(s.quantity or 0)
            e = prod.setdefault(lbl, {
                "article": lbl, "desc": "", "currency": "UGX",
                "recent_q": 0.0, "recent_v": 0.0, "prev_q": 0.0, "prev_v": 0.0, "last": None})
            cur_dt = _idx_to_date(idx)
            if (rev > 0 or qty > 0) and (e["last"] is None or cur_dt > e["last"]):
                e["last"] = cur_dt
            if L - wm < idx <= L:
                e["recent_q"] += qty
                e["recent_v"] += rev
                if rev > 0:
                    recent_value += rev
                    recent_months.add(idx)
            elif L - 2 * wm < idx <= L - wm:
                e["prev_q"] += qty
                e["prev_v"] += rev
        for e in prod.values():
            e["trend"] = _trend(e["recent_q"], e["prev_q"])
        products = sorted(prod.values(),
                          key=lambda e: (e["trend"] != "stopped", -e["recent_v"], -e["prev_v"]))
        last_order = max((e["last"] for e in prod.values() if e["last"]), default=None)
        summary = {"orders": len(recent_months), "value": {"UGX": recent_value},
                   "last_order": last_order,
                   "n_products": len([e for e in prod.values() if e["recent_q"] > 0]),
                   "n_stopped": len([e for e in prod.values() if e["trend"] == "stopped"])}

        # uploaded sales history for this customer: net invoiced revenue per year
        # (invoices + credit notes) and product mix per year (from the pivot)
        from models import Invoice, SalesHistory
        for i in db.session.scalars(db.select(Invoice).filter_by(customer_id=selected.id)):
            if i.payment_status != "Reversed" and i.invoice_date:
                hist_years[i.invoice_date.year] = hist_years.get(i.invoice_date.year, 0.0) + float(i.untaxed or 0)
            if (float(i.total or 0) > 0
                    and i.payment_status in ("Not Paid", "Partially Paid", "In Payment")):
                hist_outstanding += float(i.total or 0)
        hist_years = dict(sorted(hist_years.items()))

        # monthly product history for this customer: per-year mix, last-bought
        # month, and which products are no longer being bought
        sh_rows = db.session.scalars(
            db.select(SalesHistory).filter_by(customer_id=selected.id)).all()
        latest = db.session.scalar(
            db.select(db.func.max(SalesHistory.year * 100 + SalesHistory.month))) or 0
        ly, lm = latest // 100, latest % 100
        hist_latest = datetime(ly, lm, 1).strftime("%b %Y") if latest else None

        pm = defaultdict(lambda: defaultdict(float))
        last_ym = {}
        cust_month = defaultdict(float)
        for s in sh_rows:
            rev = float(s.revenue or 0)
            lbl = pmap.get(s.product_id)
            if s.month:
                cust_month[(s.year, s.month)] += rev   # monthly trend keeps all sales
            if not lbl:
                continue                                # product mix: catalogue only
            pm[lbl][s.year] += rev
            if s.month and rev > 0:
                k = (s.year, s.month)
                if lbl not in last_ym or k > last_ym[lbl]:
                    last_ym[lbl] = k

        def months_since(ym):
            return (ly * 12 + lm) - (ym[0] * 12 + ym[1]) if (latest and ym) else None

        all_items = []
        for name, by in pm.items():
            lym = last_ym.get(name)
            since = months_since(lym)
            lapsed = since is not None and since >= 3
            label = datetime(lym[0], lym[1], 1).strftime("%b %Y") if lym else "—"
            all_items.append((name, sum(by.values()), dict(by), label, lapsed))
        hist_n_lapsed = sum(1 for it in all_items if it[4])
        all_items.sort(key=lambda t: t[1], reverse=True)
        hist_products = all_items[:25]
        hist_prod_years = sorted({y for _, _, by, _, _ in hist_products for y in by})

        ms = sorted(cust_month)
        hist_month_labels = [datetime(y, m, 1).strftime("%b %y") for y, m in ms]
        hist_month_values = [round(cust_month[k]) for k in ms]

    if selected and request.args.get("export") == "csv":
        out = io.StringIO(); w = csv.writer(out)
        w.writerow([f"Customer insights — {selected.name}", f"window {days} days"])
        w.writerow(["Article", "Description", "Recent qty", "Recent value",
                    "Previous qty", "Previous value", "Last ordered", "Trend"])
        for e in products:
            w.writerow([e["article"], e["desc"], e["recent_q"], f"{e['recent_v']:.2f}",
                        e["prev_q"], f"{e['prev_v']:.2f}", e["last"], e["trend"]])
        return _csv(out, f"customer_{selected.id}_{days}d.csv")

    return render_template("reports/customer.html", customers=customers,
                           selected=selected, products=products, summary=summary,
                           days=days, today=today,
                           hist_years=hist_years, hist_products=hist_products,
                           hist_prod_years=hist_prod_years, hist_outstanding=hist_outstanding,
                           hist_month_labels=hist_month_labels, hist_month_values=hist_month_values,
                           hist_n_lapsed=hist_n_lapsed, hist_latest=hist_latest)


# ---------------------------------------------------------------------------
# Lapsed & at-risk — customers gone quiet and products they've dropped
# ---------------------------------------------------------------------------
@bp.route("/lapsed")
def lapsed():
    """Customers gone quiet and products dropped — from the uploaded monthly
    sales history (matched customers)."""
    from models import SalesHistory
    days = request.args.get("days", default=90, type=int)
    today = date.today()
    wm = _win_months(days)
    L = _sh_latest_idx()
    if not L:
        return render_template("reports/lapsed.html", at_risk=[], stopped=[],
                               days=days, today=today)

    customers = {c.id: c for c in db.session.scalars(
        db.select(Customer).where(Customer.archived.is_(False)))}
    per = defaultdict(lambda: {"last": 0, "recent": set(), "prev": {}})
    for r in db.session.scalars(db.select(SalesHistory).where(
            SalesHistory.month.isnot(None), SalesHistory.customer_id.isnot(None))):
        idx = r.year * 12 + r.month
        p = per[r.customer_id]
        if float(r.revenue or 0) > 0 or (r.quantity or 0) > 0:
            p["last"] = max(p["last"], idx)
        if L - wm < idx <= L:
            p["recent"].add(r.product)
        elif L - 2 * wm < idx <= L - wm:
            e = p["prev"].setdefault(r.product, {"qty": 0.0, "last": 0})
            e["qty"] += float(r.quantity or 0)
            e["last"] = max(e["last"], idx)

    at_risk, stopped = [], []
    for cid, p in per.items():
        c = customers.get(cid)
        if c is None or not p["last"]:
            continue
        last_dt = _idx_to_date(p["last"])
        cat = c.category.name if c.category else "—"
        if p["last"] <= L - wm:
            at_risk.append({"customer": c, "last_order": last_dt,
                            "days": (today - last_dt).days, "category": cat})
        for prod, e in p["prev"].items():
            if prod not in p["recent"]:
                stopped.append({"customer": c.name, "customer_id": c.id, "article": prod,
                                "desc": "", "prev_qty": e["qty"],
                                "last": _idx_to_date(e["last"]) if e["last"] else None,
                                "category": cat})
    at_risk.sort(key=lambda x: -x["days"])
    stopped.sort(key=lambda x: (x["customer"], -x["prev_qty"]))

    if request.args.get("export") == "csv":
        out = io.StringIO(); w = csv.writer(out)
        w.writerow([f"Stopped buying (window ~{wm} month(s))"])
        w.writerow(["Customer", "Product", "Prev qty", "Last bought"])
        for s in stopped:
            w.writerow([s["customer"], s["article"], s["prev_qty"], s["last"]])
        return _csv(out, f"lapsed_{days}d.csv")

    return render_template("reports/lapsed.html", at_risk=at_risk, stopped=stopped,
                           days=days, today=today)


# ---------------------------------------------------------------------------
# Reorder reminders — predict each customer's next order from their cadence
# ---------------------------------------------------------------------------
@bp.route("/reorder")
def reorder():
    """Predict each customer's next order from their invoice cadence (history)."""
    from models import Invoice
    today = date.today()
    by_cust = defaultdict(set)
    cust_obj = {}
    for i in db.session.scalars(db.select(Invoice).where(
            Invoice.customer_id.isnot(None),
            Invoice.number.like("INV%"),
            Invoice.payment_status != "Reversed",
            Invoice.invoice_date.isnot(None))):
        by_cust[i.customer_id].add(i.invoice_date)
    customers = {c.id: c for c in db.session.scalars(
        db.select(Customer).where(Customer.archived.is_(False)))}
    rows = []
    for cid, dset in by_cust.items():
        dates = sorted(dset)
        if len(dates) < 3:
            continue
        intervals = [(dates[k] - dates[k - 1]).days for k in range(1, len(dates))]
        # use median interval to resist outliers
        intervals.sort()
        avg = intervals[len(intervals) // 2]
        last = dates[-1]
        predicted = last + timedelta(days=round(avg))
        rows.append({"customer": customers.get(cid), "last": last, "avg": round(avg),
                     "predicted": predicted, "due_in": (predicted - today).days,
                     "orders": len(dates)})
    rows = [r for r in rows if r["customer"] is not None]
    rows.sort(key=lambda r: r["due_in"])
    overdue = [r for r in rows if r["due_in"] < 0]
    due_soon = [r for r in rows if 0 <= r["due_in"] <= 7]
    return render_template("reports/reorder.html", rows=rows, overdue=overdue,
                           due_soon=due_soon, today=today)


# ---------------------------------------------------------------------------
# Month-on-month customer scorecard
# ---------------------------------------------------------------------------
def _month_keys(n):
    today = date.today()
    keys = []
    y, m = today.year, today.month
    for _ in range(n):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12; y -= 1
    return list(reversed(keys))


@bp.route("/scorecard")
def scorecard():
    """Month-on-month customer spend from the uploaded monthly history."""
    from models import SalesHistory
    n = max(2, min(24, request.args.get("months", default=6, type=int)))
    L = _sh_latest_idx()
    if not L:
        return render_template("reports/scorecard.html", months=[], rows=[],
                               growers=[], decliners=[], last=None, prev=None)
    idxs = list(range(L - n + 1, L + 1))
    months = [_idx_to_date(i).strftime("%Y-%m") for i in idxs]
    monthset = set(months)
    grid = defaultdict(lambda: defaultdict(float))
    for s in db.session.scalars(db.select(SalesHistory).where(SalesHistory.month.isnot(None))):
        key = f"{s.year:04d}-{s.month:02d}"
        if key in monthset:
            grid[(s.customer_name, "UGX")][key] += float(s.revenue or 0)
    rows = []
    last, prev = (months[-1], months[-2]) if len(months) >= 2 else (months[-1], None)
    for (name, cur), vals in grid.items():
        cur_v = vals.get(last, 0.0)
        prev_v = vals.get(prev, 0.0) if prev else 0.0
        change = ((cur_v - prev_v) / prev_v * 100.0) if prev_v else (100.0 if cur_v else 0.0)
        rows.append({"name": name, "currency": cur, "vals": vals,
                     "cur": cur_v, "prev": prev_v, "change": change})
    rows.sort(key=lambda r: -r["change"])
    growers = [r for r in rows if r["change"] > 0][:8]
    decliners = sorted([r for r in rows if r["change"] < 0], key=lambda r: r["change"])[:8]
    return render_template("reports/scorecard.html", months=months, rows=rows,
                           growers=growers, decliners=decliners, last=last, prev=prev)


# ---------------------------------------------------------------------------
# Product velocity & demand
# ---------------------------------------------------------------------------
@bp.route("/velocity")
def velocity():
    """Product movement (units) from the uploaded monthly history."""
    from models import SalesHistory
    days = request.args.get("days", default=90, type=int)
    wm = _win_months(days)
    L = _sh_latest_idx()
    pmap = _product_labels()
    prod = {}
    if L:
        for s in db.session.scalars(db.select(SalesHistory).where(SalesHistory.month.isnot(None))):
            idx = s.year * 12 + s.month
            in_recent = L - wm < idx <= L
            in_prev = L - 2 * wm < idx <= L - wm
            if not (in_recent or in_prev):
                continue
            lbl = pmap.get(s.product_id)
            if not lbl:
                continue                  # off-catalogue: omitted
            e = prod.setdefault(lbl, {"article": lbl, "desc": "",
                                      "recent": 0.0, "prev": 0.0, "short": 0})
            if in_recent:
                e["recent"] += float(s.quantity or 0)
            else:
                e["prev"] += float(s.quantity or 0)
    weeks = max(wm * 4.345, 1)
    items = list(prod.values())
    for e in items:
        e["per_week"] = e["recent"] / weeks
        e["change"] = ((e["recent"] - e["prev"]) / e["prev"] * 100.0) if e["prev"] else (100.0 if e["recent"] else 0.0)
    movers = sorted(items, key=lambda e: -e["recent"])[:20]
    decliners = sorted([e for e in items if e["prev"] > 0 and e["change"] < 0],
                       key=lambda e: e["change"])[:20]
    if request.args.get("export") == "csv":
        out = io.StringIO(); w = csv.writer(out)
        w.writerow([f"Product velocity (last {days} days)"])
        w.writerow(["Article", "Description", "Units recent", "Per week", "Units prev", "Change %", "Short events"])
        for e in sorted(items, key=lambda e: -e["recent"]):
            w.writerow([e["article"], e["desc"], e["recent"], f"{e['per_week']:.1f}",
                        e["prev"], f"{e['change']:.0f}", e["short"]])
        return _csv(out, f"velocity_{days}d.csv")
    return render_template("reports/velocity.html", movers=movers, decliners=decliners,
                           days=days)


# ---------------------------------------------------------------------------
# Fulfilment performance — fill rate, on-time dispatch, most-short products
# ---------------------------------------------------------------------------
@bp.route("/fulfilment-perf")
def fulfilment_perf():
    days = request.args.get("days", default=90, type=int)
    today = date.today()
    start = today - timedelta(days=days)
    orders = [o for o in db.session.scalars(db.select(SalesOrder).where(SalesOrder.status.in_(DELIVERED)))
              if ((o.delivered_at or o.fulfilled_at).date() if (o.delivered_at or o.fulfilled_at) else o.order_date) >= start]
    units_ord = units_del = 0.0
    on_time = on_time_base = 0
    weekly = defaultdict(lambda: [0.0, 0.0])   # week -> [ordered, delivered]
    short = defaultdict(lambda: {"desc": "", "count": 0})
    for o in orders:
        fdate = (o.delivered_at or o.fulfilled_at).date() if (o.delivered_at or o.fulfilled_at) else o.order_date
        wk = fdate.strftime("%Y-W%V")
        if o.delivery_date and o.dispatched_at:
            on_time_base += 1
            if o.dispatched_at.date() <= o.delivery_date:
                on_time += 1
        for l in o.lines:
            ordq = l.quantity or 0
            delq = l.delivered_qty or 0
            units_ord += ordq; units_del += delq
            weekly[wk][0] += ordq; weekly[wk][1] += delq
            if delq < ordq or l.availability == "not_delivered":
                s = short[l.article_no]; s["desc"] = l.description; s["count"] += 1
    fill = (units_del / units_ord * 100.0) if units_ord else None
    ontime_pct = (on_time / on_time_base * 100.0) if on_time_base else None
    weekly_rows = [{"week": k, "fill": (v[1] / v[0] * 100.0) if v[0] else None}
                   for k, v in sorted(weekly.items())]
    short_rows = sorted([{"article": a, **d} for a, d in short.items()],
                        key=lambda x: -x["count"])[:20]

    # Speed metrics from timestamps
    from services.timing import durations, humanize
    disp, deliv, cyc, slow = [], [], [], []
    for o in orders:
        d = durations(o)
        if d["to_dispatch"] is not None:
            disp.append(d["to_dispatch"])
        if d["dispatch_to_deliver"] is not None:
            deliv.append(d["dispatch_to_deliver"])
        if d["to_deliver"] is not None:
            cyc.append(d["to_deliver"])
            slow.append({"number": o.number, "customer": o.customer.name,
                         "to_dispatch": d["to_dispatch"], "to_deliver": d["to_deliver"]})
    avg = lambda xs: (sum(xs) / len(xs)) if xs else None
    speed = {"dispatch": humanize(avg(disp)), "deliver": humanize(avg(deliv)),
             "cycle": humanize(avg(cyc))}
    slow.sort(key=lambda x: -(x["to_deliver"] or 0))
    slowest = [{"number": s["number"], "customer": s["customer"],
                "to_dispatch": humanize(s["to_dispatch"]),
                "to_deliver": humanize(s["to_deliver"])} for s in slow[:15]]
    return render_template("reports/fulfilment_perf.html", days=days, orders=len(orders),
                           fill=fill, ontime_pct=ontime_pct, on_time=on_time,
                           on_time_base=on_time_base, weekly=weekly_rows, short=short_rows,
                           speed=speed, slowest=slowest)
