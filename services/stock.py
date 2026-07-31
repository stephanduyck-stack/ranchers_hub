"""Stock movements. Every change to a product's on-hand quantity goes through
apply_movement, which updates the balance and records an audit row."""
import logging

from extensions import db
from models import Product, StockMovement, Store

log = logging.getLogger(__name__)


DEFAULT_STORES = [
    ("Coldroom", "sellable", 0),
    ("Dry Store", "materials", 1),
    ("Blast Freezer", "production", 2),
    ("Mini Blast Store", "production", 3),
    ("Carcus Chiller", "production", 4),
]


def ensure_stores():
    """Seed the standard stores once."""
    if db.session.scalar(db.select(db.func.count(Store.id))):
        return
    for name, kind, order in DEFAULT_STORES:
        db.session.add(Store(name=name, kind=kind, sort_order=order))
    db.session.commit()


def sellable_store():
    return db.session.scalar(db.select(Store).filter_by(kind="sellable"))


def apply_movement(product, qty_delta, kind, user_id=None, note=None, order_id=None,
                   lot_number=None, expiry=None):
    """Adjust a product's stock by qty_delta (signed) and log it. Returns the
    movement. Does not commit — the caller commits.

    Unit convention (decided 21 Jul 2026, replaces the M16 TODO): a product's
    stock unit IS its selling unit — items sold per kg are stocked in kg,
    items sold per piece in pieces (product.unit_of_measure). No conversion
    happens at deduction; order lines and stock speak the same unit. The
    sales path is blocked from exceeding stock by the stock_guard checks in
    orders.save_fulfilled and orders.complete; the negative-balance warning
    below stays as a net for non-sales movements."""
    if product is None or not qty_delta:
        return None
    new_balance = (product.stock_on_hand or 0) + qty_delta
    if new_balance < 0:
        log.warning("Stock for product %s (%s) goes negative: %s -> %s (%s%s)",
                    getattr(product, "id", "?"), getattr(product, "article_no", "?"),
                    product.stock_on_hand, new_balance, kind,
                    f", order {order_id}" if order_id else "")
    product.stock_on_hand = new_balance
    mv = StockMovement(product_id=product.id, qty=qty_delta, kind=kind,
                       balance_after=product.stock_on_hand, note=note,
                       lot_number=(lot_number or None), expiry=expiry,
                       order_id=order_id, user_id=user_id)
    db.session.add(mv)
    return mv


def deduct_order(order, user_id=None, already_flagged=False):
    """Take delivered quantities off stock when an order is fulfilled. Runs once
    per order. Callers that flip ``stock_deducted`` atomically in SQL (H7) pass
    ``already_flagged=True`` so this does not re-check or re-set the flag."""
    if not already_flagged and getattr(order, "stock_deducted", False):
        return 0
    n = 0
    for line in order.lines:
        if not line.product_id:
            continue
        qty = line.delivered_qty or 0
        if qty <= 0:
            continue
        apply_movement(line.product, -qty, "sale", user_id=user_id,
                       note=f"Order {order.number}", order_id=order.id)
        n += 1
    if not already_flagged:
        order.stock_deducted = True
    return n
