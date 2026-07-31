"""Invoice-after-delivery (31 Jul 2026).

The operational decision behind this module: direct invoicing at order entry
produced a stream of credit notes, because some customers weigh the goods at
the gate and accept slightly different quantities than were dispatched. The
flow is now:

    fulfilment -> DELIVERY NOTE only (no invoice) -> driver delivers ->
    customer checks quantities and signs -> driver uploads the signed note ->
    INVOICING QUEUE: the clerk keys the ACCEPTED quantities off the signed
    note and posts the fiscal invoice for exactly those -> the driver brings
    the physical signed note back -> FILING QUEUE: the clerk marks the paper
    filed under a filing number linked to the invoice (proof of delivery).

Shortfalls carry a reason code. Reasons where goods physically return
(rejected, damaged, short loaded) put the quantity back into stock; reasons
where the goods are gone (weighed short, other loss) carry the cost into the
sale's COGS so the margin stays honest. Every variance line feeds the
variance report (by driver, by customer, by product).
"""
import os
from datetime import datetime, date, timedelta

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort, current_app, send_from_directory, send_file)
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import (SalesOrder, SalesOrderLine, Customer, AccInvoice,
                    Product, User, VARIANCE_REASONS)
from services.audit import log

bp = Blueprint("invoicing", __name__, url_prefix="/invoicing")

POD_AGING_HOURS = 24       # truck out, no signed note uploaded
PAPER_AGING_HOURS = 48     # invoiced, physical note still not back


@bp.before_request
@login_required
def _guard():
    """Invoicing clerks (any finance role), managers and admins."""
    if not (current_user.is_finance or current_user.role in
            ("admin", "ceo", "manager")):
        abort(403)


def _external(q):
    """Real customers only — internal shop orders transfer stock, no invoice."""
    return (q.join(Customer, SalesOrder.customer_id == Customer.id)
             .where(Customer.internal_location_id.is_(None)))


def _get_order(order_id):
    o = db.session.get(SalesOrder, order_id)
    if o is None:
        abort(404)
    return o


def _invoice_for(order):
    from services.sale_posting import invoice_for_order
    return invoice_for_order(order)


def _uninvoiced():
    """Orders with no live fiscal invoice — keeps legacy deliveries that were
    invoiced at fulfilment (before this flow) out of the queue."""
    return ~SalesOrder.id.in_(
        db.select(AccInvoice.order_id).where(
            AccInvoice.order_id.is_not(None),
            AccInvoice.kind == "invoice",
            AccInvoice.status != "void"))


def queue_count():
    """Signed notes waiting to be invoiced (navbar badge)."""
    return db.session.scalar(_external(
        db.select(db.func.count(SalesOrder.id))
        .where(SalesOrder.status == "delivered",
               SalesOrder.accepted_recorded_at.is_(None),
               _uninvoiced()))) or 0


def filing_count():
    """Invoiced deliveries whose physical signed note is not filed yet."""
    return db.session.scalar(
        db.select(db.func.count(SalesOrder.id))
        .where(SalesOrder.accepted_recorded_at.is_not(None),
               SalesOrder.dnote_filing_no.is_(None))) or 0


@bp.route("/")
def index():
    """The invoicing queue plus the on-the-road aging list."""
    to_invoice = db.session.scalars(_external(
        db.select(SalesOrder)
        .where(SalesOrder.status == "delivered",
               SalesOrder.accepted_recorded_at.is_(None),
               _uninvoiced())
        .order_by(SalesOrder.delivered_at.asc()))).all()
    on_road = db.session.scalars(_external(
        db.select(SalesOrder)
        .where(SalesOrder.status == "out_for_delivery")
        .order_by(SalesOrder.dispatched_at.asc()))).all()
    now = datetime.utcnow()

    def hours_since(ts):
        return (now - ts).total_seconds() / 3600.0 if ts else None

    return render_template(
        "invoicing/queue.html", to_invoice=to_invoice, on_road=on_road,
        hours_since=hours_since, pod_aging=POD_AGING_HOURS,
        filing_open=filing_count())


@bp.route("/order/<int:order_id>")
def invoice_form(order_id):
    """The clerk's screen: signed note on one side, accepted quantities on
    the other. Defaults to the dispatched quantity per line."""
    order = _get_order(order_id)
    if order.status != "delivered":
        flash("This order is not delivered yet — invoicing works from the "
              "signed delivery note after delivery.", "warning")
        return redirect(url_for("invoicing.index"))
    if order.accepted_recorded_at is not None or _invoice_for(order):
        flash(f"{order.number} is already invoiced.", "info")
        return redirect(url_for("invoicing.index"))
    if order.customer and order.customer.internal_location_id:
        flash("Internal shop orders transfer stock — nothing to invoice.", "info")
        return redirect(url_for("invoicing.index"))
    pod_ext = os.path.splitext(order.pod_filename or "")[1].lower()
    return render_template("invoicing/invoice_form.html", order=order,
                           reasons=VARIANCE_REASONS,
                           pod_is_image=pod_ext in (".jpg", ".jpeg", ".png", ".webp"))


@bp.route("/order/<int:order_id>/invoice", methods=["POST"])
def post_invoice(order_id):
    """Record accepted quantities, return/write off the variance, post the
    fiscal invoice for the accepted quantities, fiscalize after commit."""
    order = _get_order(order_id)
    if order.status != "delivered" or order.accepted_recorded_at is not None \
            or _invoice_for(order) is not None \
            or (order.customer and order.customer.internal_location_id):
        abort(400)

    # ---- parse and validate every line before touching anything -----------
    parsed, errors = [], []
    for l in order.lines:
        disp = l.dispatched_qty or 0
        if disp <= 0:
            continue
        raw = (request.form.get(f"accepted_{l.id}") or "").strip()
        try:
            accepted = float(raw) if raw != "" else disp
        except ValueError:
            errors.append(f"{l.description}: '{raw}' is not a number.")
            continue
        if accepted < 0:
            errors.append(f"{l.description}: accepted quantity below zero.")
            continue
        if accepted > disp + 1e-9:
            errors.append(f"{l.description}: accepted {accepted:g} exceeds the "
                          f"dispatched {disp:g}. The customer signed for at "
                          f"most what was on the truck.")
            continue
        reason = (request.form.get(f"reason_{l.id}") or "").strip()
        short = disp - accepted
        if short > 1e-9 and reason not in VARIANCE_REASONS:
            errors.append(f"{l.description}: {short:g} short — pick a reason.")
            continue
        parsed.append((l, accepted, reason if short > 1e-9 else None))
    if errors:
        for e in errors:
            flash(e, "danger")
        return redirect(url_for("invoicing.invoice_form", order_id=order.id))

    # ---- apply: accepted quantities, variance, stock returns --------------
    from services import stock as stock_svc
    returned_lines, lost_lines = [], []
    for l, accepted, reason in parsed:
        l.accepted_qty = accepted
        l.variance_reason = reason
        short = (l.dispatched_qty or 0) - accepted
        if short > 1e-9 and reason:
            label, returns_goods = VARIANCE_REASONS[reason]
            if returns_goods and l.product_id:
                stock_svc.apply_movement(
                    l.product, short, "delivery_return",
                    user_id=current_user.id, order_id=order.id,
                    note=f"Delivery variance {order.number}: {label}")
                returned_lines.append((l, short))
            else:
                lost_lines.append((l, short))
    order.accepted_recorded_at = datetime.utcnow()
    order.accepted_recorded_by_id = current_user.id
    log("delivery_accepted_qty", "sales_order", order.id,
        detail=(f"{order.number} accepted quantities recorded from signed "
                f"{order.dnote_number or 'delivery note'}: "
                f"{len(returned_lines)} line(s) returned to stock, "
                f"{len(lost_lines)} line(s) short with no return"))

    any_billable = any(a > 1e-9 for _l, a, _r in parsed)
    if not any_billable:
        # Full rejection: nothing to bill. Returned goods are back in stock;
        # gone goods (if any) need an inventory write-off by finance.
        db.session.commit()
        msg = (f"{order.number}: the customer accepted nothing, so no invoice "
               f"was raised.")
        if returned_lines:
            msg += f" {len(returned_lines)} line(s) returned to stock."
        if lost_lines:
            msg += (f" {len(lost_lines)} line(s) are gone with no return — "
                    f"write the value off from Accounting > Inventory.")
        flash(msg, "warning")
        return redirect(url_for("invoicing.index"))

    # ---- post the fiscal invoice at ACCEPTED quantities -------------------
    from services import sale_posting
    try:
        invoice = sale_posting.post_sale(order, user_id=current_user.id)
    except sale_posting.SalePostingError as e:
        db.session.rollback()
        flash(f"Invoice could not be posted: {e}", "danger")
        return redirect(url_for("invoicing.invoice_form", order_id=order.id))

    # Fiscalize AFTER commit (post_sale commits): books are safe either way.
    from services import efris
    if efris.try_fiscalize(invoice):
        fis = f" and fiscalized (FDN {invoice.efris_fdn})"
    else:
        fis = "; fiscalization pending — queued for retry"
    var_note = ""
    if returned_lines:
        var_note += f" {len(returned_lines)} short line(s) back into stock."
    if lost_lines:
        var_note += (f" {len(lost_lines)} line(s) short with no goods back — "
                     f"cost carried into COGS.")
    flash(f"Invoice {invoice.invoice_no} posted{fis} for the accepted "
          f"quantities on {order.dnote_number or order.number}.{var_note} "
          f"File the physical signed note when the driver returns it.",
          "success")
    return redirect(url_for("invoicing.index"))


@bp.route("/filing")
def filing():
    """Physical signed delivery notes still with the driver, per driver."""
    open_orders = db.session.scalars(
        db.select(SalesOrder)
        .where(SalesOrder.accepted_recorded_at.is_not(None),
               SalesOrder.dnote_filing_no.is_(None))
        .order_by(SalesOrder.delivered_at.asc())).all()
    filed = db.session.scalars(
        db.select(SalesOrder)
        .where(SalesOrder.dnote_filing_no.is_not(None))
        .order_by(SalesOrder.dnote_filed_at.desc()).limit(30)).all()
    now = datetime.utcnow()

    def hours_since(ts):
        return (now - ts).total_seconds() / 3600.0 if ts else None

    by_driver = {}
    for o in open_orders:
        key = o.assigned_driver.full_name if o.assigned_driver else "Unassigned"
        by_driver.setdefault(key, []).append(o)
    return render_template("invoicing/filing.html", by_driver=by_driver,
                           open_count=len(open_orders), filed=filed,
                           hours_since=hours_since, paper_aging=PAPER_AGING_HOURS)


def _next_filing_no(year):
    prefix = f"FIL-{year}-"
    last = db.session.scalar(
        db.select(db.func.max(SalesOrder.dnote_filing_no))
        .where(SalesOrder.dnote_filing_no.like(prefix + "%")))
    seq = 0
    if last:
        try:
            seq = int(last.rsplit("-", 1)[1])
        except (ValueError, IndexError):
            seq = 0
    return f"{prefix}{seq + 1:05d}"


@bp.route("/order/<int:order_id>/file", methods=["POST"])
def mark_filed(order_id):
    """The physical signed delivery note is in hand: file it under the next
    filing number. The number is the proof-of-delivery reference on the
    invoice — 'show me the signed paper' becomes one lookup."""
    order = _get_order(order_id)
    if order.accepted_recorded_at is None:
        flash(f"Invoice {order.number} first, then file the signed note.", "warning")
        return redirect(url_for("invoicing.filing"))
    if order.dnote_filing_no:
        flash(f"{order.number} is already filed as {order.dnote_filing_no}.", "info")
        return redirect(url_for("invoicing.filing"))
    year = date.today().year
    for _attempt in (1, 2):     # retry once on a concurrent-number collision
        order.dnote_filing_no = _next_filing_no(year)
        order.dnote_filed_at = datetime.utcnow()
        order.dnote_filed_by_id = current_user.id
        try:
            log("dnote_filed", "sales_order", order.id,
                detail=(f"Signed {order.dnote_number or 'delivery note'} for "
                        f"{order.number} filed as {order.dnote_filing_no}"))
            db.session.commit()
            break
        except IntegrityError:
            db.session.rollback()
    flash(f"Filed: {order.dnote_number or order.number} → "
          f"{order.dnote_filing_no}. Write the number on the paper before "
          f"putting the copy in the file.", "success")
    return redirect(url_for("invoicing.filing"))


# ---------------------------------------------------------------------------
# Discrepancy report (31 Jul 2026)
#
# Reading a shortfall as a fraud signal needs a DENOMINATOR. 200,000 short on
# 40M dispatched is noise; the same 200,000 on 2M is a pattern. Every rollup
# below therefore carries the dispatched value it was measured against, and
# rates come from value, not quantity (a kilo of fillet is not a kilo of
# offal).
#
# The money at risk is the GONE value: goods that left the store, the customer
# did not accept, and the driver did not bring back. Returned goods are back
# in stock and reconcile physically — they are an operations problem, not a
# loss. The report keeps the two apart everywhere.
#
# Flags are arithmetic, not a verdict: a flagged driver has a gone-rate well
# above the company's own average on enough volume to mean something. That is
# a reason to check the paperwork, never proof of theft.
# ---------------------------------------------------------------------------
FLAG_MIN_BASE_UGX = 500_000     # too small a base to rate at all
FLAG_MIN_GONE_UGX = 50_000      # too small a loss to chase
FLAG_RATE_MULTIPLE = 2.0        # gone-rate this many times the company rate
FLAG_RATE_ABSOLUTE = 5.0        # or simply above this percentage
REPEAT_PAIR_MIN = 3             # driver+customer deliveries short, goods gone


def _rate(part, whole):
    return (part / whole * 100.0) if whole else 0.0


def _ugx(amount, order):
    """Money in UGX. Export orders are priced in USD, so a report that summed
    document currencies would add dollars to shillings. Convert at the rate
    stamped on the order — the same rate the ledger booked the sale at — so
    every figure on this page is one currency."""
    a = float(amount or 0)
    if (order.currency or "UGX") == "UGX":
        return a
    return a * float(order.exchange_rate_value or 0)


@bp.route("/variance")
def variance():
    """Delivery discrepancy report: dispatched vs accepted, rated against the
    value dispatched, split gone vs returned, by driver, customer, product and
    driver-customer pair, with a weekly trend and outlier flags."""
    try:
        days = max(int(request.args.get("days", 30)), 1)
    except ValueError:
        days = 30
    since = datetime.utcnow() - timedelta(days=days)
    # Every CHECKED line is the denominator: only deliveries whose accepted
    # quantities were recorded can carry a measurable discrepancy.
    lines = db.session.scalars(
        db.select(SalesOrderLine).join(SalesOrder)
        .where(SalesOrder.accepted_recorded_at >= since,
               SalesOrderLine.accepted_qty.is_not(None))
        .order_by(SalesOrder.accepted_recorded_at.desc())).all()
    var_lines = [l for l in lines if (l.variance_qty or 0) > 1e-9]

    def blank(label):
        return {"label": label, "base": 0.0, "value": 0.0, "gone": 0.0,
                "returned": 0.0, "qty": 0.0, "gone_qty": 0.0, "lines": 0,
                "orders": set(), "var_orders": set(), "reasons": {}}

    def finish(rows, company_gone_rate):
        out = []
        for r in rows:
            r["n_orders"] = len(r["orders"])
            r["n_var_orders"] = len(r["var_orders"])
            r.pop("orders"); r.pop("var_orders")
            r["rate"] = _rate(r["value"], r["base"])
            r["gone_rate"] = _rate(r["gone"], r["base"])
            r["hit_rate"] = _rate(r["n_var_orders"], r["n_orders"])
            r["rated"] = r["base"] >= FLAG_MIN_BASE_UGX
            r["flag"] = bool(
                r["rated"] and r["gone"] >= FLAG_MIN_GONE_UGX
                and (r["gone_rate"] >= FLAG_RATE_ABSOLUTE
                     or (company_gone_rate > 0
                         and r["gone_rate"] >= company_gone_rate * FLAG_RATE_MULTIPLE)))
            r["top_reason"] = (max(r["reasons"].items(), key=lambda kv: kv[1])[0]
                               if r["reasons"] else None)
            out.append(r)
        return sorted(out, key=lambda r: (-r["gone"], -r["value"]))

    def rollup(keyfn, labelfn, company_gone_rate):
        agg = {}
        for l in lines:                       # ALL checked lines build the base
            k = keyfn(l)
            if k is None:
                continue
            row = agg.setdefault(k, blank(labelfn(l)))
            row["base"] += _ugx(l.dispatched_total, l.order)
            row["orders"].add(l.order_id)
            v = l.variance_qty or 0
            if v <= 1e-9:
                continue
            val = _ugx(l.variance_value, l.order)
            row["value"] += val
            row["qty"] += v
            row["lines"] += 1
            row["var_orders"].add(l.order_id)
            if l.variance_returns_goods:
                row["returned"] += val
            else:
                row["gone"] += val
                row["gone_qty"] += v
            lbl = l.variance_reason_label or "—"
            row["reasons"][lbl] = row["reasons"].get(lbl, 0) + 1
        return finish(list(agg.values()), company_gone_rate)

    # ---- company totals first: they set the bar every group is judged on ----
    base_total = sum(_ugx(l.dispatched_total, l.order) for l in lines)
    total_value = sum(_ugx(l.variance_value, l.order) for l in var_lines)
    gone_total = sum(_ugx(l.variance_value, l.order) for l in var_lines
                     if not l.variance_returns_goods)
    returned_total = total_value - gone_total
    company_gone_rate = _rate(gone_total, base_total)
    totals = {
        "base": base_total, "value": total_value, "gone": gone_total,
        "returned": returned_total,
        "rate": _rate(total_value, base_total),
        "gone_rate": company_gone_rate,
        "n_checked": len({l.order_id for l in lines}),
        "n_var": len({l.order_id for l in var_lines}),
        "n_lines": len(lines), "n_var_lines": len(var_lines),
    }
    totals["hit_rate"] = _rate(totals["n_var"], totals["n_checked"])
    # Annualised only as an order-of-magnitude read on "is this a big issue".
    totals["annualised_gone"] = gone_total / days * 365 if days else 0

    by_driver = rollup(lambda l: l.order.assigned_driver_id or 0,
                       lambda l: (l.order.assigned_driver.full_name
                                  if l.order.assigned_driver else "Unassigned"),
                       company_gone_rate)
    by_customer = rollup(lambda l: l.order.customer_id,
                         lambda l: l.order.customer.name if l.order.customer else "—",
                         company_gone_rate)
    by_product = rollup(lambda l: l.product_id or l.description,
                        lambda l: l.description or "—",
                        company_gone_rate)
    by_pair = rollup(
        lambda l: (l.order.assigned_driver_id or 0, l.order.customer_id),
        lambda l: (f"{l.order.assigned_driver.full_name if l.order.assigned_driver else 'Unassigned'}"
                   f"  →  {l.order.customer.name if l.order.customer else '—'}"),
        company_gone_rate)
    # A pair only earns attention when the SAME driver came up short at the
    # SAME customer repeatedly with no goods returned. One bad scale reading
    # is life; three is a habit worth explaining.
    pairs_repeat = [p for p in by_pair
                    if p["n_var_orders"] >= REPEAT_PAIR_MIN and p["gone"] > 0]

    # ---- weekly trend: is the problem growing, flat, or shrinking? ---------
    weeks = {}
    for l in lines:
        ts = l.order.accepted_recorded_at
        if not ts:
            continue
        wk = (ts.date() - timedelta(days=ts.weekday()))     # Monday of that week
        row = weeks.setdefault(wk, {"base": 0.0, "value": 0.0, "gone": 0.0,
                                    "orders": set()})
        row["base"] += _ugx(l.dispatched_total, l.order)
        row["orders"].add(l.order_id)
        if (l.variance_qty or 0) > 1e-9:
            val = _ugx(l.variance_value, l.order)
            row["value"] += val
            if not l.variance_returns_goods:
                row["gone"] += val
    trend = []
    for wk in sorted(weeks):
        r = weeks[wk]
        trend.append({"week": wk, "base": r["base"], "value": r["value"],
                      "gone": r["gone"], "n_orders": len(r["orders"]),
                      "rate": _rate(r["value"], r["base"]),
                      "gone_rate": _rate(r["gone"], r["base"])})

    # ---- coverage: the report's own blind spot ---------------------------
    # Everything above measures deliveries that were CHECKED. The way to stay
    # out of a discrepancy report is to make sure nobody ever checks: no
    # signed note uploaded, no paper returned. Coverage per driver turns that
    # escape route into its own visible number.
    delivered = db.session.scalars(_external(
        db.select(SalesOrder).where(SalesOrder.delivered_at >= since)
        .order_by(SalesOrder.delivered_at.desc()))).all()
    cov = {}
    for o in delivered:
        key = o.assigned_driver_id or 0
        row = cov.setdefault(key, {
            "label": (o.assigned_driver.full_name if o.assigned_driver
                      else "Unassigned"),
            "delivered": 0, "checked": 0, "filed": 0, "no_pod": 0,
            "value": 0.0, "unchecked_value": 0.0})
        row["delivered"] += 1
        row["value"] += _ugx(o.dispatched_total, o)
        if o.accepted_recorded_at is not None:
            row["checked"] += 1
        else:
            row["unchecked_value"] += _ugx(o.dispatched_total, o)
        if o.dnote_filing_no:
            row["filed"] += 1
        if not o.pod_filename:
            row["no_pod"] += 1
    coverage = []
    for r in cov.values():
        r["checked_rate"] = _rate(r["checked"], r["delivered"])
        r["filed_rate"] = _rate(r["filed"], r["delivered"])
        r["gap"] = r["delivered"] - r["filed"]
        coverage.append(r)
    coverage.sort(key=lambda r: (-r["unchecked_value"], -r["gap"]))
    cov_totals = {
        "delivered": sum(r["delivered"] for r in coverage),
        "checked": sum(r["checked"] for r in coverage),
        "filed": sum(r["filed"] for r in coverage),
        "no_pod": sum(r["no_pod"] for r in coverage),
        "unchecked_value": sum(r["unchecked_value"] for r in coverage),
    }
    cov_totals["checked_rate"] = _rate(cov_totals["checked"], cov_totals["delivered"])
    cov_totals["filed_rate"] = _rate(cov_totals["filed"], cov_totals["delivered"])

    flagged = ([r for r in by_driver if r["flag"]]
               + [r for r in by_customer if r["flag"]])
    return render_template(
        "invoicing/variance.html", days=days, totals=totals, trend=trend,
        by_driver=by_driver, by_customer=by_customer, by_product=by_product,
        by_pair=by_pair, pairs_repeat=pairs_repeat,
        coverage=coverage, cov_totals=cov_totals,
        n_flagged=len(flagged), detail=var_lines,
        min_base=FLAG_MIN_BASE_UGX, rate_multiple=FLAG_RATE_MULTIPLE,
        rate_absolute=FLAG_RATE_ABSOLUTE, repeat_min=REPEAT_PAIR_MIN)


@bp.route("/variance.csv")
def variance_csv():
    """The discrepancy detail as CSV — the CFO's own cut of the same data."""
    import csv
    from io import StringIO
    try:
        days = max(int(request.args.get("days", 30)), 1)
    except ValueError:
        days = 30
    since = datetime.utcnow() - timedelta(days=days)
    lines = db.session.scalars(
        db.select(SalesOrderLine).join(SalesOrder)
        .where(SalesOrder.accepted_recorded_at >= since,
               SalesOrderLine.accepted_qty.is_not(None))
        .order_by(SalesOrder.accepted_recorded_at.desc())).all()
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(["Recorded", "Delivered", "Order", "Delivery note", "Filing no",
                "Customer", "Driver", "Recorded by", "Item", "Dispatched",
                "Accepted", "Short", "Reason", "Goods returned",
                "Dispatched value", "Short value", "Currency",
                "Dispatched value UGX", "Short value UGX"])
    for l in lines:
        if (l.variance_qty or 0) <= 1e-9:
            continue
        o = l.order
        w.writerow([
            o.accepted_recorded_at.strftime("%Y-%m-%d %H:%M") if o.accepted_recorded_at else "",
            o.delivered_at.strftime("%Y-%m-%d %H:%M") if o.delivered_at else "",
            o.number, o.dnote_number or "", o.dnote_filing_no or "",
            o.customer.name if o.customer else "",
            o.assigned_driver.full_name if o.assigned_driver else "",
            o.accepted_recorded_by.full_name if o.accepted_recorded_by else "",
            l.description or "", f"{l.dispatched_qty:g}", f"{l.accepted_qty:g}",
            f"{l.variance_qty:g}", l.variance_reason_label,
            "yes" if l.variance_returns_goods else "no",
            f"{float(l.dispatched_total or 0):.0f}",
            f"{float(l.variance_value or 0):.0f}", o.currency or "UGX",
            f"{_ugx(l.dispatched_total, o):.0f}", f"{_ugx(l.variance_value, o):.0f}"])
    from flask import Response
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=delivery_discrepancies_{days}d.csv"})


@bp.route("/order/<int:order_id>/pod")
def pod(order_id):
    """The uploaded signed delivery note, viewable by the clerk (the orders
    blueprint's copy is scoped to sales visibility, which finance lacks)."""
    order = _get_order(order_id)
    if not order.pod_filename:
        abort(404)
    folder = os.path.join(current_app.config["UPLOAD_DIR"], "pod")
    return send_from_directory(folder, order.pod_filename)


@bp.route("/order/<int:order_id>/dnote.pdf")
def dnote_pdf(order_id):
    """The delivery note as dispatched — reference while keying quantities."""
    from io import BytesIO
    from services import exports
    order = _get_order(order_id)
    pdf = exports.delivery_note_to_pdf(order)
    return send_file(BytesIO(pdf), mimetype="application/pdf",
                     download_name=f"{order.dnote_number or order.number}.pdf")
