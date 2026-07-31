"""Landing dashboard, focused on orders and fulfilment."""
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, redirect, url_for
from flask_login import login_required, current_user

from extensions import db
from models import (Pricelist, Product, Offer, SalesOrder, ExchangeRate,
                    AuditLog, Customer)
from services.security import can_see_customer_pricelist
from services.revenue import net_ugx

bp = Blueprint("dashboard", __name__)

CONFIRMED = ("placed", "in_fulfillment", "pending", "ready_for_dispatch",
             "out_for_delivery", "dispatched", "delivered", "fulfilled")
QUEUE = ("submitted", "placed", "in_fulfillment", "pending", "ready_for_dispatch",
         "out_for_delivery", "dispatched")


@bp.route("/dashboard")
@login_required
def home():
    # 21 Jul 2026: the fulfilment officer's whole world is the orders inbox —
    # no dashboard, straight there.
    if getattr(current_user, "is_fulfilment_officer", False):
        return redirect(url_for("orders.index"))
    if getattr(current_user, "is_store_manager", False):
        return _store_dashboard()
    if getattr(current_user, "is_stock_auditor", False):
        return _audit_dashboard()
    if getattr(current_user, "is_finance", False):
        return _finance_dashboard()
    if getattr(current_user, "is_ceo", False) or getattr(current_user, "is_sales_director", False):
        return _ceo_dashboard()
    today = date.today()
    week_ago = today - timedelta(days=7)

    # Orders visible to this user
    orders = db.session.scalars(
        db.select(SalesOrder).order_by(SalesOrder.created_at.desc())).all()
    if not current_user.sees_all_orders:
        assigned = {c.id for c in current_user.assigned_customers}
        orders = [o for o in orders if o.customer_id in assigned]

    counts = defaultdict(int)
    for o in orders:
        counts[o.status] += 1

    to_confirm = [o for o in orders if o.status == "submitted"]
    in_prep = [o for o in orders if o.status == "in_fulfillment"]
    dispatched = [o for o in orders if o.status in ("ready_for_dispatch", "out_for_delivery", "dispatched")]
    pending = [o for o in orders if o.status == "pending"]
    backorders_open = [o for o in orders
                       if o.backorder_of_id and o.status in QUEUE]
    # Active orders: every order from received until fully delivered, oldest first.
    queue = [o for o in orders if o.status in QUEUE]
    queue.sort(key=lambda o: o.created_at or datetime.min)
    from services.timing import delay_status
    delayed = [o for o in queue if delay_status(o)[0]]

    # value of confirmed orders today / this week, per currency
    val_today, val_week = defaultdict(float), defaultdict(float)
    n_today = n_week = 0
    for o in orders:
        if o.status in CONFIRMED:
            if o.order_date == today:
                n_today += 1
                val_today[o.currency] += float(o.total or 0)
            if o.order_date and o.order_date >= week_ago:
                n_week += 1
                val_week[o.currency] += float(o.total or 0)

    recent = orders[:8]

    # secondary: offers awaiting decision, rate alerts, pricelist quick links
    offers = db.session.scalars(db.select(Offer)).all()
    if not (current_user.can_manage_all or current_user.is_order_manager):
        assigned = {c.id for c in current_user.assigned_customers}
        offers = [o for o in offers if o.customer_id in assigned]
    open_offers = [o for o in offers if o.status in ("draft", "issued")]

    rate_alerts = [r for r in db.session.scalars(db.select(ExchangeRate))
                   if r.status_for() in ("expiring", "expired")]

    generic = db.session.scalars(
        db.select(Pricelist).filter_by(is_customer=False, archived=False)
        .order_by(Pricelist.name)).all()
    n_products = db.session.scalar(
        db.select(db.func.count(Product.id)).filter_by(status="active")) or 0

    # Recent customer message threads this user can see
    from models import Message
    from blueprints.messages import can_see_thread
    msg_threads, seen = [], set()
    for m in db.session.scalars(db.select(Message).order_by(Message.created_at.desc())):
        if m.customer_id in seen:
            continue
        cust = m.customer
        if cust is None or not can_see_thread(current_user, cust):
            continue
        seen.add(m.customer_id)
        unread = db.session.scalar(
            db.select(db.func.count(Message.id)).where(
                Message.customer_id == cust.id, Message.sender_type == "customer",
                Message.read_by_staff.is_(False))) or 0
        msg_threads.append({"customer": cust, "last": m, "unread": unread})
        if len(msg_threads) >= 6:
            break

    # CRM reminders (opt-in feature)
    crm_overdue, crm_today = [], []
    from services.features import feature_on
    from services.permissions import has_perm
    if feature_on("reminders") and has_perm(current_user, "log_activity"):
        from models import Activity
        if current_user.can_manage_all or getattr(current_user, "sees_all_customers", False):
            vis = None
        else:
            vis = {c.id for c in current_user.assigned_customers}
        acts = db.session.scalars(
            db.select(Activity).where(Activity.next_action_date.isnot(None),
                                      Activity.follow_up_done.is_(False))
            .order_by(Activity.next_action_date)).all()
        if vis is not None:
            acts = [a for a in acts if a.customer_id in vis]
        crm_overdue = [a for a in acts if a.next_action_date < today]
        crm_today = [a for a in acts if a.next_action_date == today]

    if current_user.is_pricing_officer:
        from services.allocation import allowed_pricelists_for
        all_cust = db.session.scalars(
            db.select(Customer).filter_by(archived=False)).all()
        trade = [c for c in all_cust if (c.segment or "customer") != "distributor"]
        dists = [c for c in all_cust if c.segment == "distributor"]
        unallocated = [c for c in all_cust if not allowed_pricelists_for(c)]
        pending = [c for c in all_cust if c.onboarding_status == "pending"]
        n_generic_pl = db.session.scalar(
            db.select(db.func.count(Pricelist.id)).filter_by(is_customer=False, archived=False)) or 0
        n_tailored = db.session.scalar(
            db.select(db.func.count(Pricelist.id)).filter_by(is_customer=True, archived=False)) or 0
        from models import PriceApproval
        my_appr = db.session.scalars(db.select(PriceApproval).where(
            PriceApproval.requested_by_id == current_user.id)
            .order_by(PriceApproval.requested_at.desc()).limit(40)).all()
        my_pending = [a for a in my_appr if a.status == "pending"]
        return render_template(
            "dashboard_pricing.html", today=today,
            n_customers=len(trade), n_distributors=len(dists),
            n_generic=n_generic_pl, n_tailored=n_tailored,
            unallocated=unallocated[:50], n_unallocated=len(unallocated),
            pending=pending[:50], n_pending=len(pending),
            my_appr=my_appr, my_pending=my_pending)

    if current_user.is_fulfilment_officer:
        return render_template(
            "dashboard_fulfilment.html", today=today, queue=queue, delayed=delayed,
            to_confirm=to_confirm, in_prep=in_prep, dispatched=dispatched,
            pending=pending, n_today=n_today)

    if current_user.is_order_manager:
        proposed_bo = [o for o in orders if o.bo_confirm_state == "proposed"]
        held = [o for o in orders if o.customer
                and o.customer.account_status in ("on_hold", "blocked")
                and o.status in QUEUE]
        delivered_today = [o for o in orders if o.status in ("delivered", "fulfilled")
                           and o.delivered_at and o.delivered_at.date() == today]
        oos_open = [o for o in orders if o.status in QUEUE
                    and any(l.availability == "out_of_stock" for l in o.lines)]
        # orders received per day, last 14 days
        win = today - timedelta(days=13)
        rec = {win + timedelta(days=i): 0 for i in range((today - win).days + 1)}
        for o in orders:
            if o.order_date and o.order_date in rec:
                rec[o.order_date] += 1
        rec_days = sorted(rec)
        # active status mix
        nice = {"submitted": "To accept", "placed": "Placed",
                "in_fulfillment": "In fulfilment", "pending": "Pending stock",
                "ready_for_dispatch": "Ready", "out_for_delivery": "Out for delivery",
                "dispatched": "Dispatched"}
        mix_labels, mix_values = [], []
        for s in ("submitted", "placed", "in_fulfillment", "pending",
                  "ready_for_dispatch", "out_for_delivery", "dispatched"):
            if counts.get(s):
                mix_labels.append(nice[s])
                mix_values.append(counts[s])
        # customer feedback inbox — only ratings not yet reviewed; once marked
        # reviewed they drop off here (still in the feedback report).
        new_fb = [o for o in orders if o.rating and not o.feedback_ack]
        new_fb.sort(key=lambda o: o.rated_at or datetime.min, reverse=True)
        fb_recent = new_fb
        fb_low = [o for o in new_fb if o.rating <= 2]
        rated30 = [o for o in orders if o.rating and o.rated_at
                   and o.rated_at.date() >= (today - timedelta(days=30))]
        fb_avg = round(sum(o.rating for o in rated30) / len(rated30), 1) if rated30 else None
        return render_template(
            "dashboard_order.html", today=today, queue=queue, delayed=delayed,
            to_confirm=to_confirm, in_prep=in_prep, dispatched=dispatched,
            pending=pending, backorders_open=backorders_open, n_today=n_today,
            proposed_bo=proposed_bo, held=held, delivered_today=delivered_today,
            oos_open=oos_open, msg_threads=msg_threads,
            recv_labels=[d.strftime("%d %b") for d in rec_days],
            recv_values=[rec[d] for d in rec_days],
            mix_labels=mix_labels, mix_values=mix_values,
            fb_recent=fb_recent, fb_low=fb_low, fb_avg=fb_avg, n_rated=len(rated30))

    if current_user.is_dispatch_officer:
        ready = [o for o in orders if o.status == "ready_for_dispatch"]
        to_assign = [o for o in ready if not o.assigned_driver_id]
        awaiting_accept = [o for o in orders if o.status == "ready_for_dispatch"
                           and o.assigned_driver_id and not o.driver_accepted_at]
        en_route = [o for o in orders if o.status == "out_for_delivery"]
        late = [o for o in (ready + en_route) if delay_status(o)[0]]
        delivered_today = [o for o in orders if o.status in ("delivered", "fulfilled")
                           and o.delivered_at and o.delivered_at.date() == today]
        # deliveries completed per day, last 14 days
        win = today - timedelta(days=13)
        dd = {win + timedelta(days=i): 0 for i in range((today - win).days + 1)}
        for o in orders:
            if o.delivered_at and o.delivered_at.date() in dd:
                dd[o.delivered_at.date()] += 1
        ddays = sorted(dd)
        # load per driver (active deliveries)
        from collections import Counter
        load = Counter()
        for o in (ready + en_route):
            if o.assigned_driver:
                load[o.assigned_driver.full_name] += 1
        load_sorted = load.most_common(8)
        return render_template(
            "dashboard_dispatch.html", today=today, to_assign=to_assign,
            awaiting_accept=awaiting_accept, en_route=en_route, late=late,
            ready=ready, delivered_today=delivered_today, msg_threads=msg_threads,
            load_labels=[f"{n} ({v})" for n, v in load_sorted],
            load_values=[v for _, v in load_sorted])

    if current_user.is_sales_manager:
        return _sales_manager_dashboard(today)

    if current_user.is_rep:
        return _rep_dashboard(orders, today)

    # Invoiced sales from the accounting import (net of VAT and credit notes,
    # reversed documents excluded). The invoice table is the running sales
    # record from 1 Jul 2026; app orders show separately below it until the
    # app itself becomes the invoicing source.
    inv_stats = None
    if current_user.sees_all_orders:
        from models import Invoice
        month_start = today.replace(day=1)
        lo = min(month_start, week_ago)
        inv_stats = {"today": defaultdict(float), "week": defaultdict(float),
                     "mtd": defaultdict(float),
                     "n_today": 0, "n_week": 0, "n_mtd": 0,
                     "latest": db.session.scalar(db.select(db.func.max(Invoice.invoice_date)))}
        for d, untaxed, ccy in db.session.execute(
                db.select(Invoice.invoice_date, Invoice.untaxed, Invoice.currency)
                .where(Invoice.invoice_date >= lo,
                       Invoice.payment_status != "Reversed")):
            v = float(untaxed or 0)
            ccy = ccy or "UGX"
            if d >= month_start:
                inv_stats["mtd"][ccy] += v
                inv_stats["n_mtd"] += 1
            if d >= week_ago:
                inv_stats["week"][ccy] += v
                inv_stats["n_week"] += 1
            if d == today:
                inv_stats["today"][ccy] += v
                inv_stats["n_today"] += 1

    return render_template(
        "dashboard.html", today=today,
        counts=counts, to_confirm=to_confirm, in_prep=in_prep,
        dispatched=dispatched, pending=pending, backorders_open=backorders_open,
        queue=queue, recent=recent, n_today=n_today, n_week=n_week,
        val_today=val_today, val_week=val_week, open_offers=open_offers,
        rate_alerts=rate_alerts, n_generic=len(generic), n_products=n_products,
        can_fulfill=current_user.can_fulfill, msg_threads=msg_threads,
        delayed=delayed, crm_overdue=crm_overdue, crm_today=crm_today,
        inv_stats=inv_stats)


def _ugx(o):
    """Net (excl-VAT) order value in UGX so live orders sit on the same basis
    as the net invoice history. Delegates to the shared revenue helper."""
    return net_ugx(o)


def _pct(cur, prev):
    if not prev:
        return None
    return (cur - prev) / prev * 100.0


def _ceo_dashboard():
    """Executive overview: revenue, growth, mix, leaders and risk."""
    today = date.today()
    yesterday = today - timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())          # Monday
    last_week_start = week_start - timedelta(days=7)
    days_into_week = (today - week_start).days
    month_start = today.replace(day=1)
    prev_month_end = month_start - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    days_into_month = (today - month_start).days
    win90 = today - timedelta(days=89)

    from models import Invoice, SalesHistory
    orders = db.session.scalars(db.select(SalesOrder)).all()
    booked = [o for o in orders if o.status in CONFIRMED and o.order_date]

    # The uploaded invoices are the sales record for every month they cover
    # (running record from 1 Jul 2026, topped up per upload). App orders only
    # own months BEYOND the latest invoice, so nothing double-counts when an
    # app order later comes back as an Odoo invoice. sales_history keeps its
    # own cutover for the product mix (monthly pivot, catalogue products).
    hist_cutover = db.session.scalar(
        db.select(db.func.max(SalesHistory.year * 12 + SalesHistory.month))) or 0
    last_inv_date = db.session.scalar(db.select(db.func.max(Invoice.invoice_date)))
    inv_cutover = (last_inv_date.year * 12 + last_inv_date.month) if last_inv_date else 0
    cur_idx = today.year * 12 + today.month
    target = max(inv_cutover, hist_cutover, cur_idx)  # latest month to report on
    recent_lo = target - 2                    # last 3 months for the top lists

    def _i2d(idx):
        return date((idx - 1) // 12, (idx - 1) % 12 + 1, 1)

    # Export vs Local distributor split (Stephan, 30 Jul 2026): a customer
    # allocated to an Export pricelist counts as export.
    from services.revenue import export_customer_ids, dist_band
    exp_ids = export_customer_ids()

    def _chan(c):
        if c is None:
            return "Other"
        if (c.segment or "customer") == "distributor":
            return f"{dist_band(c, exp_ids)} distributors"
        return (c.category.name if c.category else None) or "Other"

    pmap = {p.id: p.description for p in db.session.scalars(db.select(Product))}
    monthly = defaultdict(float)
    chan = defaultdict(float)
    cust_rev = defaultdict(float)
    rep_rev = defaultdict(float)
    dist_rev = defaultdict(float)
    prod_rev = defaultdict(float)
    prod_qty = defaultdict(float)

    # Uploaded invoices (net UGX) own every month up to the latest invoice.
    # H5 resolved 8 Jul 2026: the Odoo "Signed" amount columns are company
    # currency (UGX) for every invoice, including USD ones — the currency
    # column is informational only. No rate lookup needed; no currency filter.
    inv_daily = defaultdict(float)            # current-month daily curve
    for i in db.session.scalars(db.select(Invoice).where(
            Invoice.payment_status != "Reversed",
            Invoice.invoice_date.isnot(None))):
        idx = i.invoice_date.year * 12 + i.invoice_date.month
        v = float(i.untaxed or 0)
        if i.invoice_date >= month_start:
            inv_daily[i.invoice_date] += v
        monthly[idx] += v
        if idx == target:
            chan[_chan(i.customer)] += v
        if recent_lo <= idx <= target:
            cust_rev[i.customer_name or "—"] += v
            if i.salesperson:
                rep_rev[i.salesperson] += v
            if i.customer and (i.customer.segment or "") == "distributor":
                dist_rev[i.customer_name or "—"] += v

    # Live app orders own months after the latest invoice (keeps growing)
    for o in booked:
        idx = o.order_date.year * 12 + o.order_date.month
        if idx <= inv_cutover:
            continue
        v = _ugx(o)
        c = o.customer
        monthly[idx] += v
        if idx == target:
            chan[_chan(c)] += v
        if recent_lo <= idx <= target:
            cust_rev[c.name if c else "—"] += v
            sreps = [r for r in (c.reps if c else []) if r.role in ("rep", "telesales")]
            rep_rev[sreps[0].full_name if sreps else "Unassigned"] += v
            if c and (c.segment or "") == "distributor":
                dist_rev[c.name] += v
            # Product split on the same net UGX basis as the order value above.
            net = float(o.subtotal or 0)
            rate = (v / net) if net else (
                1.0 if (o.currency or "UGX") == "UGX" else 0.0)
            for l in o.lines:
                nm = (l.product.description if getattr(l, "product", None) else None) \
                    or l.description or "—"
                prod_rev[nm] += float(l.line_total or 0) * rate
                prod_qty[nm] += float(l.delivered_qty or 0)

    # Top products (last 3 months): the monthly history pivot owns its months
    # (catalogue only); itemized invoice lines own the months after it.
    for s in db.session.scalars(db.select(SalesHistory).where(SalesHistory.month.isnot(None))):
        idx = s.year * 12 + s.month
        if recent_lo <= idx <= target and idx <= hist_cutover:
            l = pmap.get(s.product_id)
            if l:
                prod_rev[l] += float(s.revenue or 0)
                prod_qty[l] += float(s.quantity or 0)
    if inv_cutover > hist_cutover:
        from models import InvoiceLine
        lo_idx = max(recent_lo, hist_cutover + 1)
        lo_date = _i2d(lo_idx)
        for d, pid, pname, amt, qty in db.session.execute(
                db.select(Invoice.invoice_date, InvoiceLine.product_id,
                          InvoiceLine.product_name, InvoiceLine.amount,
                          InvoiceLine.quantity)
                .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
                .where(Invoice.invoice_date >= lo_date,
                       Invoice.payment_status != "Reversed")):
            idx = d.year * 12 + d.month
            if hist_cutover < idx <= min(target, inv_cutover):
                label = pmap.get(pid) or pname or "—"
                prod_rev[label] += float(amt or 0)
                prod_qty[label] += float(qty or 0)

    # Main chart: sales PER DAY for the current month (Stephan, 6 Jul 2026 —
    # was a cumulative curve). Invoices carry daily dates, so the current
    # month draws from them; app orders add the days beyond the latest
    # invoice (their months are past inv_cutover).
    mtd_days = [month_start + timedelta(days=i) for i in range((today - month_start).days + 1)]
    dser = {d: 0.0 for d in mtd_days}
    for d, v in inv_daily.items():
        if d in dser:
            dser[d] += v
    for o in booked:
        idx = o.order_date.year * 12 + o.order_date.month
        if idx > inv_cutover and o.order_date in dser:
            dser[o.order_date] += _ugx(o)
    chart_labels, chart_values = [], []
    for d in sorted(dser):
        chart_labels.append(d.strftime("%d %b"))
        chart_values.append(round(dser[d]))
    chart_mode = "mtd"
    if not any(chart_values):
        # No activity yet this month: fall back to the 6-month bar view.
        idxs = list(range(target - 5, target + 1))
        chart_labels = [_i2d(i).strftime("%b %y") for i in idxs]
        chart_values = [round(monthly[i]) for i in idxs]
        chart_mode = "months"

    # revenue tiles (month based)
    ty = (target - 1) // 12
    rev = {
        "this_month": monthly[target],
        "last_month": monthly[target - 1],
        "this_year": sum(v for ix, v in monthly.items() if (ix - 1) // 12 == ty),
        "mtd_label": _i2d(target).strftime("%B %Y"),
        "year": ty,
    }
    deltas = {"mtd": _pct(rev["this_month"], rev["last_month"])}

    channel = dict(sorted(chan.items(), key=lambda kv: kv[1], reverse=True))
    dist_exp_mtd = channel.get("Export distributors", 0.0)
    dist_loc_mtd = channel.get("Local distributors", 0.0)
    dist_mtd = dist_exp_mtd + dist_loc_mtd
    direct_mtd = sum(channel.values()) - dist_mtd
    # Top lists: LAST 30 DAYS (Stephan, 28 Jul 2026 — the 90-day view lives in
    # Reports > Top performers). Ownership is daily here: invoices own every
    # day up to the latest invoice date; app orders own the days after it, so
    # nothing double-counts when yesterday's orders arrive as invoices.
    win3 = today - timedelta(days=29)
    c3 = defaultdict(float)
    r3 = defaultdict(float)
    d3 = defaultdict(float)
    d3_band = {}                    # distributor name -> 'Export' | 'Local'
    p3v = defaultdict(float)
    p3q = defaultdict(float)
    from models import InvoiceLine
    for i in db.session.scalars(db.select(Invoice).where(
            Invoice.payment_status != "Reversed",
            Invoice.invoice_date >= win3)):
        v = float(i.untaxed or 0)
        c3[i.customer_name or "—"] += v
        if i.salesperson:
            r3[i.salesperson] += v
        if i.customer and (i.customer.segment or "") == "distributor":
            d3[i.customer_name or "—"] += v
            d3_band[i.customer_name or "—"] = dist_band(i.customer, exp_ids)
    for _d, pid, pname, amt, qty in db.session.execute(
            db.select(Invoice.invoice_date, InvoiceLine.product_id,
                      InvoiceLine.product_name, InvoiceLine.amount,
                      InvoiceLine.quantity)
            .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
            .where(Invoice.invoice_date >= win3,
                   Invoice.payment_status != "Reversed")):
        label = pmap.get(pid) or pname or "—"
        p3v[label] += float(amt or 0)
        p3q[label] += float(qty or 0)
    for o in booked:
        if o.order_date < win3:
            continue
        if last_inv_date and o.order_date <= last_inv_date:
            continue
        v = _ugx(o)
        c = o.customer
        c3[c.name if c else "—"] += v
        sreps = [r for r in (c.reps if c else []) if r.role in ("rep", "telesales")]
        r3[sreps[0].full_name if sreps else "Unassigned"] += v
        if c and (c.segment or "") == "distributor":
            d3[c.name] += v
            d3_band[c.name] = dist_band(c, exp_ids)
        net = float(o.subtotal or 0)
        rate = (v / net) if net else (1.0 if (o.currency or "UGX") == "UGX" else 0.0)
        for l in o.lines:
            nm = (l.product.description if getattr(l, "product", None) else None) \
                or l.description or "—"
            p3v[nm] += float(l.line_total or 0) * rate
            p3q[nm] += float(l.delivered_qty or 0)
    top_customers = sorted(c3.items(), key=lambda kv: kv[1], reverse=True)[:10]
    rep_leaders = sorted(r3.items(), key=lambda kv: kv[1], reverse=True)[:10]
    top_products = sorted(((k, v, p3q.get(k, 0.0)) for k, v in p3v.items()),
                          key=lambda kv: kv[1], reverse=True)[:10]
    top_distributors = sorted(
        ((k, v, d3_band.get(k, "Local")) for k, v in d3.items()),
        key=lambda kv: kv[1], reverse=True)[:10]
    d30_exp = sum(v for k, v in d3.items() if d3_band.get(k) == "Export")
    d30_loc = sum(v for k, v in d3.items() if d3_band.get(k) != "Export")

    # ---- Risk & health strip ----
    customers = db.session.scalars(
        db.select(Customer).filter_by(archived=False)).all()
    on_hold = [c for c in customers if c.account_status in ("on_hold", "blocked")]
    new_customers = sum(1 for c in customers if c.created_at and c.created_at.date() >= month_start)

    def is_credit(c):
        t = (c.payment_terms or "").strip().lower()
        return bool(t) and "cash" not in t and t not in ("prepaid", "pro forma", "proforma")
    credit_exposure = sum(_ugx(o) for o in orders
                          if o.status in QUEUE and o.customer and is_credit(o.customer))

    products = db.session.scalars(
        db.select(Product).filter_by(status="active")).all()
    oos = sum(1 for p in products if p.is_out_of_stock)
    low = sum(1 for p in products if p.is_low_stock and not p.is_out_of_stock)

    from services.timing import delay_status
    active = [o for o in orders if o.status in QUEUE]
    delayed = sum(1 for o in active if delay_status(o)[0])

    offers = db.session.scalars(db.select(Offer)).all()
    won = sum(1 for o in offers if o.status == "converted"
              and o.created_at and o.created_at.date() >= win90)
    lost = sum(1 for o in offers if o.status == "not_ordered"
               and o.created_at and o.created_at.date() >= win90)
    conv_rate = (won / (won + lost) * 100.0) if (won + lost) else None

    # Customer delivery feedback (last 90 days)
    rated = [o for o in orders if o.rating and o.rated_at and o.rated_at.date() >= win90]
    n_rated = len(rated)
    avg_rating = round(sum(o.rating for o in rated) / n_rated, 1) if n_rated else None
    low_rated = sum(1 for o in rated if o.rating <= 2)
    recent_comments = sorted([o for o in rated if o.rating_comment],
                             key=lambda o: o.rated_at, reverse=True)[:5]

    risk = {
        "on_hold": len(on_hold), "credit_exposure": credit_exposure,
        "oos": oos, "low": low, "delayed": delayed, "active": len(active),
        "conv_rate": conv_rate, "won": won, "decided": won + lost,
        "new_customers": new_customers,
        "avg_rating": avg_rating, "n_rated": n_rated, "low_rated": low_rated,
    }

    # outstanding receivables (positive unpaid invoices, excl credit notes)
    outstanding_total = db.session.scalar(
        db.select(db.func.sum(Invoice.total)).where(
            Invoice.payment_status.in_(("Not Paid", "Partially Paid", "In Payment")),
            Invoice.total > 0)) or 0

    return render_template(
        "dashboard_ceo.html", today=today, rev=rev, deltas=deltas,
        chart_labels=chart_labels, chart_values=chart_values, chart_mode=chart_mode,
        channel_labels=list(channel.keys()),
        channel_values=[round(v) for v in channel.values()],
        top_customers=top_customers, rep_leaders=rep_leaders,
        top_products=top_products, top_distributors=top_distributors,
        dist_mtd=dist_mtd, direct_mtd=direct_mtd,
        dist_exp_mtd=dist_exp_mtd, dist_loc_mtd=dist_loc_mtd,
        d30_exp=d30_exp, d30_loc=d30_loc, risk=risk,
        recent_comments=recent_comments, n_customers=len(customers),
        outstanding_total=float(outstanding_total))


def _store_dashboard():
    """Store manager: stock-health alerts and quick links."""
    from models import StockMovement, Store, StoreItem
    today = date.today()
    products = db.session.scalars(
        db.select(Product).filter_by(status="active")).all()
    oos = [p for p in products if p.is_out_of_stock]
    low = [p for p in products if p.is_low_stock and not p.is_out_of_stock]
    ok = len(products) - len(oos) - len(low)

    # store items running low (production / materials stores)
    low_items = [i for i in db.session.scalars(db.select(StoreItem)) if i.is_low]
    stores = db.session.scalars(db.select(Store).order_by(Store.sort_order)).all()

    recent = db.session.scalars(
        db.select(StockMovement).order_by(StockMovement.created_at.desc()).limit(10)).all()

    # movements in vs out, last 14 days
    win = today - timedelta(days=13)
    mv_in = {win + timedelta(days=i): 0.0 for i in range((today - win).days + 1)}
    mv_out = {d: 0.0 for d in mv_in}
    for m in db.session.scalars(db.select(StockMovement)):
        d = m.created_at.date() if m.created_at else None
        if d in mv_in:
            if (m.qty or 0) >= 0:
                mv_in[d] += m.qty or 0
            else:
                mv_out[d] += -(m.qty or 0)
    mdays = sorted(mv_in)

    return render_template(
        "dashboard_store.html", today=today, n_products=len(products),
        oos=oos, low=low, ok=ok, low_items=low_items, stores=stores, recent=recent,
        mv_labels=[d.strftime("%d %b") for d in mdays],
        mv_in=[round(mv_in[d]) for d in mdays],
        mv_out=[round(mv_out[d]) for d in mdays])


def _audit_dashboard():
    """Stock auditor: counts, discrepancies and out-of-stock at a glance."""
    from models import StockCount, Product
    today = date.today()
    month_start = today.replace(day=1)
    counts = db.session.scalars(
        db.select(StockCount).order_by(StockCount.created_at.desc())).all()
    open_counts = [c for c in counts if c.status == "open"]
    posted = [c for c in counts if c.status == "posted"]
    this_month = [c for c in counts if c.created_at and c.created_at.date() >= month_start]
    last_count = counts[0] if counts else None

    recent_posted = posted[:8]
    disc_recent = sum(len(c.discrepancy_lines) for c in recent_posted)

    # top discrepancy items from the most recent posted count
    top_disc = []
    if recent_posted:
        for l in sorted(recent_posted[0].discrepancy_lines,
                        key=lambda x: abs(x.discrepancy or 0), reverse=True)[:8]:
            top_disc.append({"name": l.product.description if l.product else "—",
                             "system": l.system_qty, "counted": l.counted_qty,
                             "diff": l.discrepancy})

    products = db.session.scalars(db.select(Product).filter_by(status="active")).all()
    oos = [p for p in products if p.is_out_of_stock]

    # discrepancy count per recent posted take (chart, oldest first)
    chrono = list(reversed(recent_posted))
    chart_labels = [(c.posted_at or c.created_at).strftime("%d %b") if (c.posted_at or c.created_at) else "—"
                    for c in chrono]
    chart_values = [len(c.discrepancy_lines) for c in chrono]

    return render_template(
        "dashboard_audit.html", today=today, counts=counts,
        open_counts=open_counts, n_posted=len(posted), this_month=this_month,
        last_count=last_count, disc_recent=disc_recent, top_disc=top_disc,
        oos=oos, recent=counts[:8],
        chart_labels=chart_labels, chart_values=chart_values)


def _sales_manager_dashboard(today):
    """Sales Manager: every rep's month-to-date vs target for the current month."""
    from models import User
    from services import targets as tsvc
    year, month = today.year, today.month
    reps = sorted(current_user.managed_reps, key=lambda r: r.full_name or "")
    rows = []
    team_target = team_actual = 0.0
    n_with_target = on_track = behind = 0
    for rep in reps:
        tg = tsvc.targets_for(rep.id, year, month)
        act = tsvc.rep_actuals(rep, year, month)
        tot = tg["total"]
        pct = (act["total"] / tot * 100.0) if tot else None
        if tot:
            n_with_target += 1
            team_target += tot
            if pct is not None and pct >= 100:
                on_track += 1
            else:
                behind += 1
        team_actual += act["total"]
        rows.append({"rep": rep, "target": tot, "actual": act["total"], "pct": pct,
                     "n_cust": len(tg["customer"]), "n_prod": len(tg["product"])})
    rows.sort(key=lambda r: (r["pct"] is None, -(r["pct"] or 0)))
    team_pct = (team_actual / team_target * 100.0) if team_target else None
    label = date(year, month, 1).strftime("%B %Y")
    return render_template("dashboard_sales_manager.html", today=today, label=label,
                           ym=f"{year}-{month:02d}", rows=rows, team_target=team_target,
                           team_actual=team_actual, team_pct=team_pct,
                           n_reps=len(reps), n_with_target=n_with_target,
                           on_track=on_track, behind=behind)


def _rep_dashboard(orders, today):
    """Rep: personal performance (from the sales history of their customers,
    extended by live orders) plus the active orders they can push."""
    from models import Offer, Activity, Invoice, SalesHistory
    month_start = today.replace(day=1)
    win90 = today - timedelta(days=89)

    assigned = current_user.assigned_customers
    assigned_ids = {c.id for c in assigned}
    booked = [o for o in orders if o.status in CONFIRMED and o.order_date]

    # Invoices own every month they cover (running record from 1 Jul 2026);
    # app orders only own months beyond the latest invoice — same rule as the
    # CEO dashboard, so a rep's July sales come from the uploaded invoices.
    last_inv_date = db.session.scalar(db.select(db.func.max(Invoice.invoice_date)))
    cutover = (last_inv_date.year * 12 + last_inv_date.month) if last_inv_date else 0
    target = max(cutover, today.year * 12 + today.month)
    recent_lo = target - 2

    def _i2d(idx):
        return date((idx - 1) // 12, (idx - 1) % 12 + 1, 1)

    monthly = defaultdict(float)
    cust_rev = defaultdict(float)
    last_idx = {}
    if assigned_ids:
        for i in db.session.scalars(db.select(Invoice).where(
                Invoice.customer_id.in_(assigned_ids),
                Invoice.payment_status != "Reversed", Invoice.invoice_date.isnot(None))):
            idx = i.invoice_date.year * 12 + i.invoice_date.month
            v = float(i.untaxed or 0)
            monthly[idx] += v
            if v > 0:
                last_idx[i.customer_id] = max(last_idx.get(i.customer_id, 0), idx)
            if recent_lo <= idx <= target:
                cust_rev[i.customer_name or "—"] += v
    for o in booked:
        if o.customer_id not in assigned_ids:
            continue
        idx = o.order_date.year * 12 + o.order_date.month
        if idx <= cutover:
            continue
        v = _ugx(o)
        monthly[idx] += v
        last_idx[o.customer_id] = max(last_idx.get(o.customer_id, 0), idx)
        if recent_lo <= idx <= target:
            cust_rev[o.customer.name if o.customer else "—"] += v

    mtd = monthly[target]
    last_mtd = monthly[target - 1]
    delta_mtd = _pct(mtd, last_mtd)
    mtd_label = _i2d(target).strftime("%B %Y")
    idxs = list(range(target - 11, target + 1))
    trend_labels = [_i2d(i).strftime("%b %y") for i in idxs]
    trend_values = [round(monthly[i]) for i in idxs]
    top_customers = sorted(cust_rev.items(), key=lambda kv: kv[1], reverse=True)[:8]

    # targets for the current month (set by the Sales Manager); hidden if unset
    from services import targets as tsvc
    from models import Customer, Product
    tmonth = _i2d(target)
    tg = tsvc.targets_for(current_user.id, tmonth.year, tmonth.month)
    act = tsvc.rep_actuals(current_user, tmonth.year, tmonth.month)
    target_total = tg["total"]
    target_pct = (act["total"] / target_total * 100.0) if target_total else None
    cust_targets = []
    for cid, amt in tg["customer"].items():
        c = db.session.get(Customer, cid)
        a = act["by_customer"].get(cid, 0.0)
        cust_targets.append({"name": c.name if c else "—", "target": amt, "actual": a,
                             "pct": (a / amt * 100.0) if amt else None})
    cust_targets.sort(key=lambda r: r["pct"] or 0)
    prod_targets = []
    for pid, amt in tg["product"].items():
        p = db.session.get(Product, pid)
        a = act["by_product"].get(pid, 0.0)
        prod_targets.append({"name": (p.description if p else "—"), "target": amt, "actual": a,
                             "pct": (a / amt * 100.0) if amt else None})
    prod_targets.sort(key=lambda r: r["pct"] or 0)
    target_actual = act["total"]

    # active orders to push
    queue = [o for o in orders if o.status in QUEUE]
    queue.sort(key=lambda o: o.created_at or datetime.min)

    # my open quotes
    offers = db.session.scalars(db.select(Offer)).all()
    my_offers = [o for o in offers if o.customer_id in assigned_ids
                 and o.status in ("draft", "issued")]
    won = sum(1 for o in offers if o.customer_id in assigned_ids and o.status == "converted")
    lost = sum(1 for o in offers if o.customer_id in assigned_ids and o.status == "not_ordered")
    conv_rate = (won / (won + lost) * 100.0) if (won + lost) else None

    # my follow-ups
    acts = db.session.scalars(
        db.select(Activity).where(Activity.next_action_date.isnot(None),
                                  Activity.follow_up_done.is_(False))
        .order_by(Activity.next_action_date)).all()
    acts = [a for a in acts if a.customer_id in assigned_ids or a.user_id == current_user.id]
    fu_overdue = [a for a in acts if a.next_action_date < today]
    fu_today = [a for a in acts if a.next_action_date == today]

    # at-risk: my customers with no purchase in the last 3 months (from history)
    at_risk = []
    for c in assigned:
        li = last_idx.get(c.id)
        if li and li <= target - 3:
            at_risk.append({"customer": c, "last": _i2d(li)})
    at_risk.sort(key=lambda r: r["last"])

    # delivery feedback for my customers (last 90 days)
    rated = [o for o in orders if o.rating and o.rated_at and o.rated_at.date() >= win90]
    n_rated = len(rated)
    avg_rating = round(sum(o.rating for o in rated) / n_rated, 1) if n_rated else None
    fb_comments = sorted([o for o in rated if o.rating_comment],
                         key=lambda o: o.rated_at, reverse=True)[:5]
    low_fb = [o for o in rated if o.rating <= 2]

    return render_template(
        "dashboard_rep.html", today=today, mtd=mtd, last_mtd=last_mtd, delta_mtd=delta_mtd,
        mtd_label=mtd_label,
        n_customers=len(assigned), queue=queue, my_offers=my_offers,
        conv_rate=conv_rate, won=won, decided=won + lost,
        fu_overdue=fu_overdue, fu_today=fu_today, at_risk=at_risk[:10],
        top_customers=top_customers,
        avg_rating=avg_rating, n_rated=n_rated, fb_comments=fb_comments, low_fb=low_fb,
        trend_labels=trend_labels, trend_values=trend_values,
        target_total=target_total, target_actual=target_actual, target_pct=target_pct,
        cust_targets=cust_targets, prod_targets=prod_targets)


def _finance_dashboard():
    """Dashboards for the finance roles. The cashier sees the till and the
    Record-receipt action, nothing else; the rest see the money position,
    what needs chasing, and their own actions first. No fulfilment noise —
    the CFO does not care which order is being picked."""
    from services import cash_posting as cash
    from services import reports_finance as rf
    from services import inventory_costing as inv_svc
    from services import ledger as ledger_svc
    from services import efris as efris_svc
    from services.permissions import has_perm
    from models import AccReceipt, AccJournalEntry, AccInvoice

    today = date.today()

    if current_user.role == "cashier":
        receipts_today = db.session.scalars(
            db.select(AccReceipt).where(AccReceipt.receipt_date == today)
            .order_by(AccReceipt.id.desc())).all()
        total_today = sum(r.amount_minor for r in receipts_today
                          if r.status == "posted" and r.currency == "UGX")
        return render_template("dashboard_cashier.html",
                               receipts=receipts_today,
                               total_today=total_today, today=today)

    month_from = today.replace(day=1)
    pl = rf.profit_and_loss(month_from, today)
    ar = rf.aged_receivables()
    ap = rf.aged_payables()
    money = cash.money_balances()
    vat = rf.vat_summary(month_from, today)
    inv_ties = inv_svc.valuation_summary()
    inv_total = sum(t["subledger"] for t in inv_ties)
    _ready, blocked = inv_svc.opening_candidates()
    _rows, tdr, tcr = ledger_svc.trial_balance()
    recent = db.session.scalars(
        db.select(AccJournalEntry).where(AccJournalEntry.posted.is_(True))
        .order_by(AccJournalEntry.id.desc()).limit(8)).all()
    pending_invoices = db.session.scalar(
        db.select(db.func.count(AccInvoice.id))
        .where(AccInvoice.efris_status == "pending")) or 0

    # The CFO also gets the commercial pulse — orders and who placed them —
    # below the money position. Information, not workflow.
    orders_view = None
    if current_user.role == "cfo":
        week_ago = today - timedelta(days=7)
        orders = db.session.scalars(
            db.select(SalesOrder).order_by(SalesOrder.created_at.desc())
            .limit(200)).all()
        counts = defaultdict(int)
        for o in orders:
            counts[o.status] += 1
        open_orders = [o for o in orders if o.status in QUEUE]
        val_today = sum(net_ugx(o) for o in orders
                        if o.status in CONFIRMED and o.order_date == today)
        val_week = sum(net_ugx(o) for o in orders
                       if o.status in CONFIRMED and o.order_date
                       and o.order_date >= week_ago)
        from services import approvals as appr_svc
        from blueprints.messages import staff_unread_count
        orders_view = {"recent": orders[:8], "counts": dict(counts),
                       "n_open": len(open_orders),
                       "val_today": val_today, "val_week": val_week,
                       "approvals_pending": appr_svc.pending_count(),
                       "unread_messages": staff_unread_count(current_user)}

    return render_template(
        "dashboard_finance.html", today=today, month_from=month_from,
        orders_view=orders_view,
        pl=pl, ar=ar, ap=ap, money=money, vat=vat,
        inv_total=inv_total, inv_tied=all(t["tied"] for t in inv_ties),
        worklist_count=len(blocked),
        tb_ok=(tdr == tcr), tb_total=tdr,
        efris_pending=efris_svc.pending_count(),
        pending_invoices=pending_invoices,
        recent=recent,
        can_receipts=has_perm(current_user, "record_receipts"),
        can_purchases=has_perm(current_user, "record_purchases"),
        can_pay=has_perm(current_user, "pay_suppliers"),
        can_reconcile=has_perm(current_user, "reconcile_bank"),
        can_post=has_perm(current_user, "post_journal"))
