"""Which pricelists a customer may be quoted/ordered from.

A customer's allowed lists = the generic lists explicitly allocated to them in
setup, plus their own customer pricelists. Nothing else is offered. If a customer
has no pricelist linked yet, order/offer/portal screens show none until a manager
links one on the customer record.
"""
from extensions import db
from models import Customer, Pricelist
from services.security import can_see_customer_pricelist


def _live(p):
    return not p.archived and (p.approval_status or "approved") == "approved"


def allowed_pricelists_for(customer):
    own = [p for p in customer.pricelists if _live(p)]
    allocated = [p for p in (customer.allowed_pricelists or []) if _live(p)]
    seen, res = set(), []
    for p in allocated + own:
        if p.id not in seen:
            seen.add(p.id)
            res.append(p)
    return res


def combinable_lists(order):
    """Every pricelist assigned to the order's customer that can price lines
    on THIS order (22 Jul 2026: one order draws from ALL assigned lists).
    Compatible means same currency and the same VAT treatment the order was
    derived with. The order's source list comes first; a product appearing on
    several lists is priced from the first list carrying it."""
    from services import order_vat
    cust = order.customer
    if cust is None:
        return [order.source_pricelist] if order.source_pricelist else []
    src = order.source_pricelist
    ordered = ([src] if src else []) + [
        p for p in allowed_pricelists_for(cust) if src is None or p.id != src.id]
    out = []
    for p in ordered:
        if p is None or p.currency != order.currency:
            continue
        va, vr = order_vat.derive_vat(p, cust)
        if bool(va) != bool(order.vat_applicable):
            continue
        if va and float(vr or 0) != float(order.vat_rate or 0):
            continue
        out.append(p)
    return out


def selectable_customers(user):
    custs = [c for c in db.session.scalars(db.select(Customer).order_by(Customer.name))
             if not c.archived]
    if user.can_manage_all or getattr(user, "is_order_manager", False):
        return custs
    assigned = {c.id for c in user.assigned_customers}
    return [c for c in custs if c.id in assigned]


def build_allocation(user, customers):
    """Return (alloc_map, lists): a {customer_id: [pricelist_id,...]} map and the
    union of pricelists to render as <option>s, filtered by what the user can see."""
    alloc, union = {}, {}
    for c in customers:
        allowed = [p for p in allowed_pricelists_for(c)
                   if can_see_customer_pricelist(user, p)]
        alloc[c.id] = [p.id for p in allowed]
        for p in allowed:
            union[p.id] = p
    lists = sorted(union.values(), key=lambda p: (p.is_customer, p.name.lower()))
    return alloc, lists


def is_allowed(user, customer, pricelist):
    if not can_see_customer_pricelist(user, pricelist):
        return False
    return pricelist.id in {p.id for p in allowed_pricelists_for(customer)}
