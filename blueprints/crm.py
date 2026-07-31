"""CRM: contacts, activity log (visits/calls), follow-ups and CRM reports.

Reps log field visits (with an optional GPS check-in); telesales log calls.
Every interaction is one Activity row tied to a customer (and optionally a
named Contact). Follow-ups are Activities with an open next-action date.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, abort)
from flask_login import login_required, current_user

from extensions import db
from models import Customer, Contact, Activity, User, Deal, CallList, CallListItem
from services.security import assert_can_see_customer, can_see_customer
from services.permissions import has_perm
from services.features import feature_on
from services.audit import log

bp = Blueprint("crm", __name__, url_prefix="/crm")

VISIT_OUTCOMES = ["Met – productive", "Met – routine", "Order taken", "Stock check",
                  "Complaint raised", "Payment collected", "Decision maker absent",
                  "Site closed", "No access"]
CALL_OUTCOMES = ["Answered – interested", "Answered – order taken", "Answered – not now",
                 "Callback requested", "No answer", "Voicemail",
                 "Not interested", "Do not call"]
ALL_OUTCOMES = VISIT_OUTCOMES + CALL_OUTCOMES


def _require_log():
    if not has_perm(current_user, "log_activity"):
        abort(403)


def _get_customer(customer_id):
    c = db.session.get(Customer, customer_id)
    if c is None:
        abort(404)
    assert_can_see_customer(current_user, c)
    return c


def _parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_d(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except ValueError:
        return None


def _f(name):
    v = (request.form.get(name) or "").strip()
    try:
        return float(v) if v else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Contacts
# --------------------------------------------------------------------------- #
@bp.route("/customer/<int:customer_id>/contact/add", methods=["POST"])
@login_required
def add_contact(customer_id):
    _require_log()
    c = _get_customer(customer_id)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("A contact name is required.", "warning")
        return redirect(url_for("customers.info", customer_id=c.id))
    primary = request.form.get("is_primary") == "1"
    if primary:
        for k in c.contacts:
            k.is_primary = False
    db.session.add(Contact(
        customer_id=c.id, name=name, title=request.form.get("title"),
        phone=request.form.get("phone"), email=request.form.get("email"),
        is_primary=primary, notes=request.form.get("notes")))
    log("contact_add", "customer", c.id, detail=f"{c.name}: contact {name}")
    db.session.commit()
    flash("Contact added.", "success")
    return redirect(url_for("customers.info", customer_id=c.id))


# crm.edit_contact removed 3 Jul 2026 (QA audit M2): the customer page offers
# add and delete contact but never had an edit form, so this handler had no
# caller. If in-place editing is wanted, restore the handler (see backups) and
# add the form to templates/customers/detail.html.


@bp.route("/contact/<int:contact_id>/delete", methods=["POST"])
@login_required
def delete_contact(contact_id):
    _require_log()
    k = db.session.get(Contact, contact_id)
    if k is None:
        abort(404)
    c = _get_customer(k.customer_id)
    name = k.name
    db.session.delete(k)
    log("contact_delete", "customer", c.id, detail=f"{c.name}: removed contact {name}")
    db.session.commit()
    flash("Contact removed.", "success")
    return redirect(url_for("customers.info", customer_id=c.id))


# --------------------------------------------------------------------------- #
# Activities (visits, calls, …)
# --------------------------------------------------------------------------- #
@bp.route("/customer/<int:customer_id>/activity/add", methods=["POST"])
@login_required
def add_activity(customer_id):
    _require_log()
    c = _get_customer(customer_id)
    kind = request.form.get("kind", "visit")
    if kind not in ("visit", "call", "email", "sms", "note", "meeting"):
        kind = "visit"
    occurred = _parse_dt(request.form.get("occurred_at")) or datetime.utcnow()
    contact_id = request.form.get("contact_id")
    contact = db.session.get(Contact, int(contact_id)) if contact_id else None
    lat, lng = _f("latitude"), _f("longitude")
    outcome = (request.form.get("outcome") or "").strip() or None
    summary = (request.form.get("summary") or "").strip() or None

    # Optional real send for email / SMS (only if feature on + configured).
    from services.features import feature_on
    from services import comms
    if request.form.get("send_now") == "1" and kind in ("email", "sms"):
        if kind == "email" and feature_on("email"):
            ok, info = comms.send_email(contact.email if contact else None,
                                        request.form.get("subject") or f"Message from {c.name}", summary or "")
            outcome = "Sent" if ok else f"Send failed: {info}"
        elif kind == "sms" and feature_on("sms"):
            ok, info = comms.send_sms(contact.phone if contact else None, summary or "")
            outcome = "Sent" if ok else f"Send failed: {info}"

    act = Activity(
        customer_id=c.id,
        contact_id=contact.id if contact else None,
        user_id=current_user.id, kind=kind,
        direction=request.form.get("direction", "outbound"),
        occurred_at=occurred, outcome=outcome, summary=summary,
        next_action=(request.form.get("next_action") or "").strip() or None,
        next_action_date=_parse_d(request.form.get("next_action_date")),
        latitude=lat, longitude=lng,
        recording_url=(request.form.get("recording_url") or "").strip() or None,
        checkin_at=datetime.utcnow() if (lat is not None and lng is not None) else None)
    db.session.add(act)
    log("activity_add", "customer", c.id,
        detail=f"{c.name}: {kind} logged by {current_user.full_name}")
    db.session.commit()
    flash(f"{act.kind_label} logged.", "success")
    return redirect(url_for("customers.info", customer_id=c.id) + "#activity")


@bp.route("/activity/<int:activity_id>/done", methods=["POST"])
@login_required
def toggle_followup(activity_id):
    _require_log()
    a = db.session.get(Activity, activity_id)
    if a is None:
        abort(404)
    _get_customer(a.customer_id)
    a.follow_up_done = not a.follow_up_done
    db.session.commit()
    flash("Follow-up updated.", "success")
    nxt = request.form.get("next") or url_for("customers.info", customer_id=a.customer_id)
    return redirect(nxt)


@bp.route("/activity/<int:activity_id>/delete", methods=["POST"])
@login_required
def delete_activity(activity_id):
    _require_log()
    a = db.session.get(Activity, activity_id)
    if a is None:
        abort(404)
    cid = a.customer_id
    _get_customer(cid)
    db.session.delete(a)
    log("activity_delete", "customer", cid, detail="activity removed")
    db.session.commit()
    flash("Activity removed.", "success")
    return redirect(url_for("customers.info", customer_id=cid))


# --------------------------------------------------------------------------- #
# Follow-up queue
# --------------------------------------------------------------------------- #
def _visible_customer_ids():
    if getattr(current_user, "sees_all_customers", False) or current_user.can_manage_all:
        return None  # all
    return {c.id for c in current_user.assigned_customers}


@bp.route("/follow-ups")
@login_required
def follow_ups():
    _require_log()
    vis = _visible_customer_ids()
    mine_only = request.args.get("mine") == "1"
    q = db.select(Activity).where(Activity.next_action_date.isnot(None),
                                  Activity.follow_up_done.is_(False))
    acts = db.session.scalars(q.order_by(Activity.next_action_date)).all()
    if vis is not None:
        acts = [a for a in acts if a.customer_id in vis]
    if mine_only:
        acts = [a for a in acts if a.user_id == current_user.id]
    today = date.today()
    overdue = [a for a in acts if a.next_action_date < today]
    due_today = [a for a in acts if a.next_action_date == today]
    upcoming = [a for a in acts if a.next_action_date > today]
    return render_template("crm/follow_ups.html", overdue=overdue,
                           due_today=due_today, upcoming=upcoming,
                           today=today, mine_only=mine_only)


# --------------------------------------------------------------------------- #
# Pipeline & deals  (feature: pipeline)
# --------------------------------------------------------------------------- #
def _require_feature(name):
    if not feature_on(name):
        abort(404)


def _parse_money(name):
    v = (request.form.get(name) or "").replace(",", "").strip()
    try:
        return float(v) if v else None
    except ValueError:
        return None


@bp.route("/pipeline")
@login_required
def pipeline():
    _require_feature("pipeline")
    vis = _visible_customer_ids()
    deals = db.session.scalars(db.select(Deal).order_by(Deal.created_at.desc())).all()
    if vis is not None:
        deals = [d for d in deals if d.customer_id in vis]
    by_stage = {s: [] for s in Deal.STAGES}
    for d in deals:
        by_stage.setdefault(d.stage, []).append(d)
    totals = {s: sum(float(x.value or 0) for x in by_stage.get(s, [])) for s in by_stage}
    return render_template("crm/pipeline.html", stages=Deal.STAGES,
                           by_stage=by_stage, totals=totals)


@bp.route("/customer/<int:customer_id>/deal/add", methods=["POST"])
@login_required
def add_deal(customer_id):
    _require_feature("pipeline")
    _require_log()
    c = _get_customer(customer_id)
    title = (request.form.get("title") or "").strip()
    if not title:
        flash("A deal title is required.", "warning")
        return redirect(url_for("customers.info", customer_id=c.id))
    stage = request.form.get("stage") if request.form.get("stage") in Deal.STAGES else "Lead"
    db.session.add(Deal(
        customer_id=c.id, title=title, value=_parse_money("value"),
        currency=request.form.get("currency") or c.default_currency or "UGX",
        stage=stage, status="open", owner_id=current_user.id,
        expected_close=_parse_d(request.form.get("expected_close")),
        notes=request.form.get("notes")))
    log("deal_add", "customer", c.id, detail=f"{c.name}: deal '{title}'")
    db.session.commit()
    flash("Deal added.", "success")
    return redirect(url_for("customers.info", customer_id=c.id) + "#deals")


@bp.route("/deal/<int:deal_id>/update", methods=["POST"])
@login_required
def update_deal(deal_id):
    _require_feature("pipeline")
    _require_log()
    d = db.session.get(Deal, deal_id)
    if d is None:
        abort(404)
    _get_customer(d.customer_id)
    stage = request.form.get("stage")
    if stage in Deal.STAGES:
        d.stage = stage
        if stage == "Won":
            d.status, d.closed_at = "won", datetime.utcnow()
        elif stage == "Lost":
            d.status, d.closed_at = "lost", datetime.utcnow()
        else:
            d.status, d.closed_at = "open", None
    if "value" in request.form:
        d.value = _parse_money("value")
    if "expected_close" in request.form:
        d.expected_close = _parse_d(request.form.get("expected_close"))
    if "notes" in request.form:
        d.notes = request.form.get("notes")
    log("deal_update", "customer", d.customer_id, detail=f"deal '{d.title}' -> {d.stage}")
    db.session.commit()
    flash("Deal updated.", "success")
    return redirect(request.form.get("next") or
                    url_for("customers.info", customer_id=d.customer_id) + "#deals")


@bp.route("/deal/<int:deal_id>/delete", methods=["POST"])
@login_required
def delete_deal(deal_id):
    _require_feature("pipeline")
    _require_log()
    d = db.session.get(Deal, deal_id)
    if d is None:
        abort(404)
    cid = d.customer_id
    _get_customer(cid)
    db.session.delete(d)
    log("deal_delete", "customer", cid, detail="deal removed")
    db.session.commit()
    flash("Deal removed.", "success")
    return redirect(url_for("customers.info", customer_id=cid) + "#deals")


# --------------------------------------------------------------------------- #
# Scheduled call lists  (feature: call_lists)
# --------------------------------------------------------------------------- #
@bp.route("/call-lists")
@login_required
def call_lists():
    _require_feature("call_lists")
    _require_log()
    lists = db.session.scalars(
        db.select(CallList).where(CallList.archived.is_(False))
        .order_by(CallList.created_at.desc())).all()
    if not (getattr(current_user, "sees_all_customers", False) or current_user.can_manage_all):
        lists = [l for l in lists if l.assigned_to_id == current_user.id
                 or l.created_by_id == current_user.id]
    telesales = db.session.scalars(
        db.select(User).where(User.is_active.is_(True),
                              User.role.in_(("telesales", "rep", "manager")))
        .order_by(User.full_name)).all()
    return render_template("crm/call_lists.html", lists=lists, telesales=telesales)


@bp.route("/call-lists/new", methods=["POST"])
@login_required
def call_list_new():
    _require_feature("call_lists")
    _require_log()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Give the call list a name.", "warning")
        return redirect(url_for("crm.call_lists"))
    cl = CallList(name=name, created_by_id=current_user.id,
                  assigned_to_id=int(request.form["assigned_to"]) if request.form.get("assigned_to") else None,
                  due_date=_parse_d(request.form.get("due_date")),
                  notes=request.form.get("notes"))
    db.session.add(cl)
    db.session.flush()
    # optional bulk add: by category, or 'not contacted in N days'
    added = _populate_call_list(cl, request.form)
    db.session.commit()
    flash(f"Call list created with {added} customer(s).", "success")
    return redirect(url_for("crm.call_list", list_id=cl.id))


def _populate_call_list(cl, form):
    mode = form.get("populate")
    custs = []
    if mode == "category" and form.get("category_id"):
        cid = form.get("category_id", type=int)
        if cid is None:
            return 0
        custs = db.session.scalars(
            db.select(Customer).where(Customer.archived.is_(False),
                                      Customer.category_id == cid)).all()
    elif mode == "quiet":
        try:
            days = int(form.get("quiet_days") or 30)
        except ValueError:
            days = 30
        cutoff = datetime.utcnow() - timedelta(days=days)
        last = {}
        for a in db.session.scalars(db.select(Activity)):
            if a.customer_id not in last or a.occurred_at > last[a.customer_id]:
                last[a.customer_id] = a.occurred_at
        for c in db.session.scalars(db.select(Customer).where(Customer.archived.is_(False))):
            if last.get(c.id) is None or last[c.id] < cutoff:
                custs.append(c)
    for i, c in enumerate(custs):
        db.session.add(CallListItem(call_list_id=cl.id, customer_id=c.id, sort_order=i))
    return len(custs)


@bp.route("/call-list/<int:list_id>")
@login_required
def call_list(list_id):
    _require_feature("call_lists")
    _require_log()
    cl = db.session.get(CallList, list_id)
    if cl is None:
        abort(404)
    return render_template("crm/call_list.html", cl=cl,
                           call_outcomes=CALL_OUTCOMES)


@bp.route("/call-list/item/<int:item_id>/log", methods=["POST"])
@login_required
def call_list_log(item_id):
    _require_feature("call_lists")
    _require_log()
    it = db.session.get(CallListItem, item_id)
    if it is None:
        abort(404)
    c = _get_customer(it.customer_id)
    status = request.form.get("status", "done")
    if status == "skipped":
        it.status = "skipped"
    else:
        act = Activity(customer_id=c.id, user_id=current_user.id, kind="call",
                       direction="outbound", occurred_at=datetime.utcnow(),
                       outcome=(request.form.get("outcome") or "").strip() or None,
                       summary=(request.form.get("summary") or "").strip() or None,
                       next_action=(request.form.get("next_action") or "").strip() or None,
                       next_action_date=_parse_d(request.form.get("next_action_date")))
        db.session.add(act)
        db.session.flush()
        it.status = "done"
        it.activity_id = act.id
    db.session.commit()
    flash("Call logged.", "success")
    return redirect(url_for("crm.call_list", list_id=it.call_list_id))


@bp.route("/call-list/<int:list_id>/archive", methods=["POST"])
@login_required
def call_list_archive(list_id):
    _require_feature("call_lists")
    _require_log()
    cl = db.session.get(CallList, list_id)
    if cl is None:
        abort(404)
    cl.archived = True
    db.session.commit()
    flash("Call list archived.", "success")
    return redirect(url_for("crm.call_lists"))


# --------------------------------------------------------------------------- #
# CRM reports
# --------------------------------------------------------------------------- #
@bp.route("/reports")
@login_required
def reports():
    if not has_perm(current_user, "view_reports"):
        abort(403)
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except ValueError:
        days = 30
    since = datetime.utcnow() - timedelta(days=days)
    vis = _visible_customer_ids()

    acts = db.session.scalars(
        db.select(Activity).where(Activity.occurred_at >= since)).all()
    if vis is not None:
        acts = [a for a in acts if a.customer_id in vis]

    # by rep / kind
    by_user = defaultdict(lambda: Counter())
    for a in acts:
        by_user[a.user_id][a.kind] += 1
    users = {u.id: u for u in db.session.scalars(db.select(User))}
    rep_rows = []
    for uid, kinds in by_user.items():
        u = users.get(uid)
        rep_rows.append({
            "name": u.full_name if u else "—",
            "role": u.role if u else "",
            "visits": kinds.get("visit", 0), "calls": kinds.get("call", 0),
            "other": sum(v for k, v in kinds.items() if k not in ("visit", "call")),
            "total": sum(kinds.values())})
    rep_rows.sort(key=lambda r: r["total"], reverse=True)

    call_outcomes = Counter(a.outcome for a in acts if a.kind == "call" and a.outcome)
    visit_outcomes = Counter(a.outcome for a in acts if a.kind == "visit" and a.outcome)

    # customers not contacted in N days
    custs = db.session.scalars(db.select(Customer).where(Customer.archived.is_(False))).all()
    if vis is not None:
        custs = [c for c in custs if c.id in vis]
    last_map = {}
    for a in db.session.scalars(db.select(Activity)):
        cur = last_map.get(a.customer_id)
        if cur is None or a.occurred_at > cur:
            last_map[a.customer_id] = a.occurred_at
    cutoff = datetime.utcnow() - timedelta(days=days)
    not_contacted = []
    for c in custs:
        last = last_map.get(c.id)
        if last is None or last < cutoff:
            not_contacted.append({"customer": c, "last": last})
    not_contacted.sort(key=lambda r: (r["last"] or datetime.min))

    return render_template(
        "crm/reports.html", days=days, rep_rows=rep_rows,
        call_outcomes=call_outcomes.most_common(),
        visit_outcomes=visit_outcomes.most_common(),
        not_contacted=not_contacted[:200], n_not_contacted=len(not_contacted),
        total_acts=len(acts))
