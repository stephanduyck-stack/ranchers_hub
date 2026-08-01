"""Database models for the Ranchers Finest pricing application.

Design notes
------------
* **One pricelist engine for both generic and customer lists.** A ``Pricelist``
  row with ``is_customer=False`` is a generic list; with ``is_customer=True`` it
  belongs to exactly one ``Customer`` and is shown under the Customer Pricelists
  tab. This keeps tiers, lines, prices and exports identical for both.
* **Flexible tiers.** The source workbooks do not share columns, so price tiers
  are modelled as named ``PricelistTier`` rows per list and ``LinePrice`` values
  per line/tier rather than fixed columns.
* **UGX is the base of value.** Local lines store UGX. Export lines carry their
  authored USD amounts (treated as pinned foreign prices). USD figures for offers
  built from UGX lists are computed at the exchange rate in force and stamped onto
  the offer so a later rate change never restates an issued offer.
"""
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP

from flask_login import UserMixin
from sqlalchemy import UniqueConstraint

from extensions import db

# ---------------------------------------------------------------------------
# Money boundary (QA audit 5 Jul 2026 H2). Line totals used to be computed as
# float(unit_price) * qty * discount, so representation error accumulated into
# order totals, VAT, and the ledger conversion. Every line total is now
# computed in Decimal and rounded HALF_UP to 2 dp at this single boundary.
# services/ledger.to_minor (also Decimal, HALF_UP) then converts document
# totals to integer minor units — float never propagates between the two.
# ---------------------------------------------------------------------------
_TWO_DP = Decimal("0.01")


def line_money(unit_price, qty, discount_pct):
    """Decimal line total = price x qty x (1 - discount%), rounded per line."""
    up = Decimal(str(unit_price or 0))
    q = Decimal(str(qty or 0))
    disc = Decimal(str(discount_pct or 0))
    total = up * q * (Decimal(1) - disc / Decimal(100))
    return total.quantize(_TWO_DP, rounding=ROUND_HALF_UP)


def vat_money(net_amount, vat_rate):
    """Decimal VAT on a net amount, rounded HALF_UP to 2 dp."""
    net = Decimal(str(net_amount or 0))
    rate = Decimal(str(vat_rate or 0))
    return (net * rate / Decimal(100)).quantize(_TWO_DP, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Association tables
# ---------------------------------------------------------------------------
customer_reps = db.Table(
    "customer_reps",
    db.Column("customer_id", db.Integer, db.ForeignKey("customer.id", ondelete="CASCADE"), primary_key=True),
    db.Column("user_id", db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
)

# Outlets a single portal login may order for (one login -> several customers).
portal_customer_link = db.Table(
    "portal_customer_link",
    db.Column("user_id", db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
    db.Column("customer_id", db.Integer, db.ForeignKey("customer.id", ondelete="CASCADE"), primary_key=True),
)

# Which generic pricelists a customer may be quoted/ordered from.
customer_pricelist_alloc = db.Table(
    "customer_pricelist_alloc",
    db.Column("customer_id", db.Integer, db.ForeignKey("customer.id", ondelete="CASCADE"), primary_key=True),
    db.Column("pricelist_id", db.Integer, db.ForeignKey("pricelist.id", ondelete="CASCADE"), primary_key=True),
)


# ---------------------------------------------------------------------------
# Users & access control
# ---------------------------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(128), nullable=False)
    email = db.Column(db.String(128))
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(16), nullable=False, default="rep")  # admin | manager | rep | order_manager | customer
    can_edit = db.Column(db.Boolean, nullable=False, default=False)  # independent edit/pricing right
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))  # for role 'customer'
    # QA audit 5 Jul 2026 M1: optional reference — losing the manager must not
    # block deleting the manager's user row.
    manager_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))  # rep -> their sales manager
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    failed_attempts = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime)
    # Auto-provisioned portal logins start with the default password and must
    # set their own before using the app (enforced app-wide in auth).
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)

    assigned_customers = db.relationship(
        "Customer", secondary=customer_reps, back_populates="reps"
    )
    customer = db.relationship("Customer", foreign_keys=[customer_id])
    manager = db.relationship("User", remote_side=[id], foreign_keys=[manager_id],
                              backref="managed_reps")

    @property
    def managed_customers(self):
        """Customers assigned to any rep reporting to this sales manager."""
        out = {}
        for rep in getattr(self, "managed_reps", []):
            for c in rep.assigned_customers:
                out[c.id] = c
        return list(out.values())
    # Extra outlets a portal login may also order for (besides its primary customer).
    portal_customers = db.relationship("Customer", secondary=portal_customer_link)

    @property
    def is_customer_user(self):
        return self.role == "customer"

    @property
    def portal_outlets(self):
        """All customers this portal login may act for: primary + linked outlets."""
        seen, out = set(), []
        for c in ([self.customer] if self.customer else []) + list(self.portal_customers):
            if c and c.id not in seen and not c.archived:
                seen.add(c.id)
                out.append(c)
        return out

    # ---- role helpers ----
    @property
    def is_admin(self):
        """The CEO carries full admin rights, so admin gates treat them alike."""
        return self.role in ("admin", "ceo")

    @property
    def is_ceo(self):
        return self.role == "ceo"

    @property
    def is_manager(self):
        return self.role == "manager"

    @property
    def is_sales_manager(self):
        return self.role == "sales_manager"

    @property
    def is_sales_director(self):
        return self.role == "sales_director"

    @property
    def is_rep(self):
        return self.role == "rep"

    @property
    def can_manage_all(self):
        """Admins and managers see and edit every list and customer."""
        return self.role in ("admin", "ceo", "manager")

    @property
    def is_order_manager(self):
        return self.role == "order_manager"

    @property
    def is_fulfilment_officer(self):
        return self.role == "fulfillment_officer"

    @property
    def is_telesales(self):
        return self.role == "telesales"

    @property
    def is_pricing_officer(self):
        return self.role == "pricing_officer"

    @property
    def is_dispatch_officer(self):
        return self.role == "dispatch_officer"

    @property
    def is_driver(self):
        return self.role == "delivery"

    @property
    def is_store_manager(self):
        return self.role == "store_manager"

    @property
    def is_stock_auditor(self):
        return self.role == "stock_auditor"

    @property
    def is_production_manager(self):
        return self.role == "production_manager"

    @property
    def is_finance(self):
        """Any finance-module role (rights themselves come from has_perm)."""
        return self.role in ("cfo", "finance_manager", "finance_clerk",
                             "cashier", "finance_viewer")

    @property
    def can_see_stock(self):
        """Staff who take orders or run/audit the store see stock levels."""
        return self.role in ("store_manager", "stock_auditor", "manager", "admin",
                             "ceo", "order_manager", "rep", "fulfillment_officer",
                             "production_manager")

    @property
    def can_dispatch(self):
        """Who assigns deliveries: the dispatch officer day to day; manager/admin
        keep it only as cover. Order managers are deliberately excluded so accepting
        and dispatching stay separate duties."""
        return self.role in ("dispatch_officer", "manager", "admin", "ceo")

    @property
    def sees_all_customers(self):
        """Roles that work the whole customer base (not just assigned).
        The CFO reads the whole base like they read every order —
        oversight, not workflow (added 8 Jul 2026)."""
        return self.role in ("admin", "ceo", "manager", "order_manager", "telesales",
                             "pricing_officer", "sales_director", "cfo")

    @property
    def can_fulfill(self):
        """Who fills delivered quantities and completes fulfilment — the fulfilment
        officer (and managers/admins). Gated by the 'fulfil_orders' permission."""
        from services.permissions import has_perm
        return has_perm(self, "fulfil_orders")

    @property
    def can_accept_orders(self):
        """Who reviews and accepts orders (stock + credit check): order managers,
        managers, admins. Kept separate from fulfilment."""
        return self.role in ("order_manager", "manager", "admin", "ceo")

    @property
    def can_clear_credit(self):
        """Who ticks the finance credit clearance on a customer profile and
        decides credit alerts: CEO, CFO, finance manager (and admin)."""
        return self.role in ("ceo", "cfo", "finance_manager", "admin")

    @property
    def sees_all_orders(self):
        """Fulfilment/dispatch roles and managers/admins see every order.
        The CFO reads every order too — oversight, not workflow."""
        return self.role in ("order_manager", "fulfillment_officer", "dispatch_officer",
                             "manager", "admin", "ceo", "cfo")

    @property
    def may_edit_prices(self):
        """Edit/pricing right is a separate flag; admins and pricing officers
        always have it."""
        return self.can_edit or self.role in ("admin", "ceo", "pricing_officer")

    def get_id(self):
        return str(self.id)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
class Category(db.Model):
    __tablename__ = "category"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    # QA audit 5 Jul 2026 M1: deleting a parent orphans children to top level.
    parent_id = db.Column(db.Integer, db.ForeignKey("category.id", ondelete="SET NULL"))
    sort_order = db.Column(db.Integer, default=0)

    parent = db.relationship("Category", remote_side=[id], backref="children")
    products = db.relationship("Product", back_populates="category")

    @property
    def full_name(self):
        return f"{self.parent.name} – {self.name}" if self.parent else self.name


class Product(db.Model):
    __tablename__ = "product"
    id = db.Column(db.Integer, primary_key=True)
    article_no = db.Column(db.String(32), unique=True, nullable=False, index=True)
    barcode = db.Column(db.String(32), index=True)
    description = db.Column(db.String(255), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"))
    pack_size = db.Column(db.String(64))          # e.g. "1kg", "500G", "5 x 200 Gr"
    unit_of_measure = db.Column(db.String(16))    # e.g. "kg", "pack", "pcs"
    vat_applicable = db.Column(db.Boolean, default=False)  # only processed items carry VAT
    # Cost floor (QA audit 5 Jul 2026): UGX cost PER KG — all costing is per
    # kg (the costing sheets produce cost/kg; pack selling prices derive from
    # pack weight). services/cost_guard converts per pricelist line and blocks
    # any price entered below the floor. Null = no guard.
    unit_cost = db.Column(db.Numeric(16, 4))
    stock_on_hand = db.Column(db.Float, default=0)         # current quantity in stock (product unit)
    low_stock_level = db.Column(db.Float, default=0)       # warn at or below this (0 = no warning)
    status = db.Column(db.String(16), nullable=False, default="active")  # active | inactive
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    category = db.relationship("Category", back_populates="products")
    lines = db.relationship("PricelistLine", back_populates="product")

    @property
    def is_active(self):
        return self.status == "active"

    @property
    def is_low_stock(self):
        return (self.low_stock_level or 0) > 0 and (self.stock_on_hand or 0) <= self.low_stock_level

    @property
    def is_out_of_stock(self):
        return (self.stock_on_hand or 0) <= 0


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------
class CustomerCategory(db.Model):
    """Trade category for customers (Supermarket, Hotel, Café, ...). Editable."""
    __tablename__ = "customer_category"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    sort_order = db.Column(db.Integer, default=0)


class Customer(db.Model):
    __tablename__ = "customer"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    contact_name = db.Column(db.String(128))
    email = db.Column(db.String(128))
    phone = db.Column(db.String(64))
    market = db.Column(db.String(16), default="local")        # local | export
    default_currency = db.Column(db.String(8), default="UGX")
    segment = db.Column(db.String(16), default="customer")    # customer | distributor
    payment_terms = db.Column(db.String(64))   # set once per customer; orders inherit it
    category_id = db.Column(db.Integer, db.ForeignKey("customer_category.id"))
    area = db.Column(db.String(128))     # operating/geographical area (distributors)
    address = db.Column(db.Text)         # physical/site address
    latitude = db.Column(db.Float)       # pinned map location
    longitude = db.Column(db.Float)
    # Onboarding pipeline: a rep creates -> 'pending' -> pricing officer approves
    onboarding_status = db.Column(db.String(12), default="approved", index=True)
    proposed_payment_terms = db.Column(db.String(64))   # what the rep suggests
    credit_approved = db.Column(db.Boolean, default=False)
    account_status = db.Column(db.String(12), default="ok")   # ok | on_hold | blocked
    account_note = db.Column(db.String(255))                  # reason for hold/block
    # Finance credit clearance (20 Jul 2026): tick set only by CEO/CFO/finance
    # manager (User.can_clear_credit) allowing this customer's orders to be
    # accepted for fulfilment. Manual control until the accounting AR link
    # exists. Distinct from credit_approved, which is the pricing officer's
    # one-time onboarding approval of the credit terms.
    credit_cleared = db.Column(db.Boolean, nullable=False, default=False)
    credit_cleared_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    credit_cleared_at = db.Column(db.DateTime)
    # 21 Jul 2026: does this customer accept back orders? Ticked: fulfilment
    # shortfalls raise a confirmed back order automatically. Unticked: no back
    # order is ever created for this customer; the balance is simply dropped.
    backorders_allowed = db.Column(db.Boolean, nullable=False, default=True)
    # Credit limit engine (21 Jul 2026): whole UGX. NULL or 0 = no limit set,
    # no automatic block. Set only by CEO/CFO/finance manager. When set, the
    # account blocks automatically once the outstanding balance (imported
    # unpaid history + accounting-module unpaid invoices) reaches the limit,
    # and acceptance requires headroom for the order's own value. Only the
    # same roles release a held order (per order, from the credit alerts
    # queue).
    credit_limit_ugx = db.Column(db.Integer)
    # Odoo receivable snapshot (27 Jul 2026): the accounting system's own
    # per-customer balance from the res.partner export — the TRUE value
    # outstanding (invoices minus payments minus credits), unlike the stale
    # payment-status flags. Refreshed whenever the contact export is loaded.
    odoo_receivable = db.Column(db.Numeric(18, 2))
    odoo_receivable_at = db.Column(db.Date)
    # Days credit limit (21 Jul 2026): NULL or 0 = none. When set, the account
    # blocks automatically once ANY unpaid invoice is older than this many
    # days — whichever of the two limits trips first kicks in. Same
    # CEO/CFO/finance-manager release applies.
    credit_days = db.Column(db.Integer)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    archived = db.Column(db.Boolean, default=False, index=True)
    # Own-shop flag (accounting Phase 7): set, this "customer" is one of our
    # own retail locations. Fulfilment then moves stock as a TRANSFER to that
    # location instead of raising a fiscal invoice — no revenue, no VAT, no
    # receivable between us and ourselves.
    internal_location_id = db.Column(db.Integer, db.ForeignKey("acc_location.id"))
    notes = db.Column(db.Text)
    # Named contacts (in addition to the main contact above)
    procurement_name = db.Column(db.String(128))
    procurement_phone = db.Column(db.String(64))
    procurement_email = db.Column(db.String(128))
    chef_name = db.Column(db.String(128))
    chef_phone = db.Column(db.String(64))
    chef_email = db.Column(db.String(128))
    other_contact_name = db.Column(db.String(128))
    other_contact_phone = db.Column(db.String(64))
    other_contact_email = db.Column(db.String(128))
    tax_id = db.Column(db.String(40))               # TIN / Tax ID
    # Delivery acceptance window
    delivery_days = db.Column(db.String(64))        # e.g. "Mon,Tue,Wed,Thu,Fri"
    delivery_time_from = db.Column(db.String(8))    # e.g. "08:00"
    delivery_time_to = db.Column(db.String(8))      # e.g. "16:00"
    delivery_notes = db.Column(db.String(255))      # exceptions, e.g. "no deliveries 12-14h"
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    category = db.relationship("CustomerCategory")
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    credit_cleared_by = db.relationship("User", foreign_keys=[credit_cleared_by_id])

    reps = db.relationship("User", secondary=customer_reps, back_populates="assigned_customers")
    pricelists = db.relationship("Pricelist", back_populates="customer",
                                 foreign_keys="Pricelist.customer_id")
    allowed_pricelists = db.relationship("Pricelist", secondary=customer_pricelist_alloc)
    offers = db.relationship("Offer", back_populates="customer")
    contacts = db.relationship("Contact", back_populates="customer",
                               order_by="Contact.is_primary.desc(), Contact.name",
                               cascade="all, delete-orphan")
    activities = db.relationship("Activity", back_populates="customer",
                                 order_by="Activity.occurred_at.desc()",
                                 cascade="all, delete-orphan")
    deals = db.relationship("Deal", back_populates="customer",
                            order_by="Deal.created_at.desc()",
                            cascade="all, delete-orphan")

    def _acts(self, kind=None):
        return [a for a in self.activities if kind is None or a.kind == kind]

    @property
    def last_visit_at(self):
        v = self._acts("visit")
        return v[0].occurred_at if v else None

    @property
    def last_contact_at(self):
        return self.activities[0].occurred_at if self.activities else None

    @property
    def next_followup(self):
        """Earliest open follow-up (activity with a future/!done next_action_date)."""
        pend = [a for a in self.activities
                if a.next_action_date and not a.follow_up_done]
        pend.sort(key=lambda a: a.next_action_date)
        return pend[0] if pend else None


# ---------------------------------------------------------------------------
# Pricelists (generic + customer) and their lines
# ---------------------------------------------------------------------------
class Pricelist(db.Model):
    __tablename__ = "pricelist"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    channel = db.Column(db.String(24))            # horeca | supermarket | mixed
    market = db.Column(db.String(16))             # local | export
    currency = db.Column(db.String(8), nullable=False, default="UGX")
    vat_applicable = db.Column(db.Boolean, default=True)
    vat_rate = db.Column(db.Float, default=18.0)
    effective_date = db.Column(db.Date, default=date.today)
    valid_until = db.Column(db.Date)
    notes = db.Column(db.Text)
    source_file = db.Column(db.String(255))

    is_customer = db.Column(db.Boolean, default=False, index=True)
    # 22 Jul 2026: box quantities are the minimum order quantity. Lines with a
    # Small box size only accept multiples of it — unless this list allows
    # small orders (Meat Supermarkets, Sokoni).
    allow_small_orders = db.Column(db.Boolean, nullable=False, default=False)
    archived = db.Column(db.Boolean, default=False, index=True)
    approval_status = db.Column(db.String(12), default="approved", index=True)  # approved | pending | declined
    group_name = db.Column(db.String(96))   # display grouping on the Pricelists tab
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    base_pricelist_id = db.Column(db.Integer, db.ForeignKey("pricelist.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer", back_populates="pricelists")
    base_pricelist = db.relationship("Pricelist", remote_side=[id])
    tiers = db.relationship(
        "PricelistTier", back_populates="pricelist",
        order_by="PricelistTier.sort_order", cascade="all, delete-orphan",
    )
    lines = db.relationship(
        "PricelistLine", back_populates="pricelist",
        order_by="PricelistLine.sort_order", cascade="all, delete-orphan",
    )

    @property
    def is_zero_rated(self):
        return not self.vat_applicable

    @property
    def price_basis(self):
        """Whether prices on this list are per kg (HORECA/wholesale) or per pack
        (retail/supermarket). None when the list mixes both."""
        if self.channel == "horeca":
            return "kg"
        if self.channel == "supermarket":
            return "pack"
        return None

    def basis_for(self, product=None):
        b = self.price_basis
        if b:
            return b
        uom = (getattr(product, "unit_of_measure", "") or "").lower()
        return "kg" if uom == "kg" else "pack"

    @property
    def basis_label(self):
        b = self.price_basis
        return "per kg" if b == "kg" else ("per pack" if b == "pack" else "per kg or per pack (see Pack)")

    @property
    def is_distributor(self):
        """A wholesale/distributor list (distributors normally get discounted prices)."""
        if any((t.key or "").startswith("dist") for t in self.tiers):
            return True
        return "distributor" in (self.name or "").lower()

    def primary_tier(self):
        """The tier a single entered price should populate."""
        keys = ("excl_vat", "dist_price", "price_excl_vat", "price_kg",
                "wholesale", "price")
        by_key = {t.key: t for t in self.tiers}
        for k in keys:
            if k in by_key:
                return by_key[k]
        return self.tiers[0] if self.tiers else None

    def status_for(self, on=None):
        """Return 'valid' | 'expiring' | 'expired' | 'open' for a given date."""
        on = on or date.today()
        if not self.valid_until:
            return "open"
        days = (self.valid_until - on).days
        if days < 0:
            return "expired"
        if days <= 7:
            return "expiring"
        return "valid"


class PricelistTier(db.Model):
    __tablename__ = "pricelist_tier"
    id = db.Column(db.Integer, primary_key=True)
    pricelist_id = db.Column(db.Integer, db.ForeignKey("pricelist.id", ondelete="CASCADE"), nullable=False)
    key = db.Column(db.String(48), nullable=False)     # stable machine key e.g. "excl_vat"
    label = db.Column(db.String(96), nullable=False)   # display e.g. "Price Excl VAT"
    sort_order = db.Column(db.Integer, default=0)

    pricelist = db.relationship("Pricelist", back_populates="tiers")


class PricelistLine(db.Model):
    __tablename__ = "pricelist_line"
    id = db.Column(db.Integer, primary_key=True)
    pricelist_id = db.Column(db.Integer, db.ForeignKey("pricelist.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    section = db.Column(db.String(160))        # the in-sheet section header it sat under
    pack_size = db.Column(db.String(64))       # per-line pack size (may differ from product default)
    units_per_pack = db.Column(db.String(64))  # e.g. "4 x 250 Gr."
    box_small = db.Column(db.Float)
    box_medium = db.Column(db.Float)
    box_large = db.Column(db.Float)
    sort_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    pricelist = db.relationship("Pricelist", back_populates="lines")
    product = db.relationship("Product", back_populates="lines")
    prices = db.relationship("LinePrice", back_populates="line", cascade="all, delete-orphan")
    override = db.relationship("FixedPriceOverride", back_populates="line",
                              uselist=False, cascade="all, delete-orphan")
    promos = db.relationship("PromoPrice", back_populates="line", cascade="all, delete-orphan")

    def price_for(self, tier_key):
        for p in self.prices:
            if p.tier.key == tier_key:
                return p.amount
        return None

    def price_map(self):
        return {p.tier.key: p.amount for p in self.prices}


class LinePrice(db.Model):
    __tablename__ = "line_price"
    __table_args__ = (
        UniqueConstraint("line_id", "tier_id", name="uq_line_price_line_tier"),
    )
    id = db.Column(db.Integer, primary_key=True)
    line_id = db.Column(db.Integer, db.ForeignKey("pricelist_line.id", ondelete="CASCADE"), nullable=False, index=True)
    tier_id = db.Column(db.Integer, db.ForeignKey("pricelist_tier.id", ondelete="CASCADE"), nullable=False, index=True)
    amount = db.Column(db.Numeric(16, 4))             # the live, approved price
    pending_amount = db.Column(db.Numeric(16, 4))     # staged change awaiting approval

    line = db.relationship("PricelistLine", back_populates="prices")
    tier = db.relationship("PricelistTier")


class PromoPrice(db.Model):
    """A temporary promotional price on one pricelist line + tier. Ends on the
    earlier of an end date or a quantity cap, then the normal price returns."""
    __tablename__ = "promo_price"
    id = db.Column(db.Integer, primary_key=True)
    line_id = db.Column(db.Integer, db.ForeignKey("pricelist_line.id", ondelete="CASCADE"), nullable=False, index=True)
    tier_id = db.Column(db.Integer, db.ForeignKey("pricelist_tier.id", ondelete="CASCADE"), nullable=False)
    promo_amount = db.Column(db.Numeric(16, 4), nullable=False)
    start_date = db.Column(db.Date, default=date.today)
    end_date = db.Column(db.Date)                 # optional
    qty_cap = db.Column(db.Float)                 # optional total quantity at promo
    status = db.Column(db.String(12), default="pending", index=True)  # pending|active|ended|declined
    note = db.Column(db.String(255))
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    approved_at = db.Column(db.DateTime)

    line = db.relationship("PricelistLine", back_populates="promos")
    tier = db.relationship("PricelistTier")
    created_by = db.relationship("User", foreign_keys=[created_by_id])


class FixedPriceOverride(db.Model):
    """A pinned standalone foreign-currency price on a pricelist line.

    When active and in-window the line uses this value and ignores the live rate.
    """
    __tablename__ = "fixed_price_override"
    id = db.Column(db.Integer, primary_key=True)
    line_id = db.Column(db.Integer, db.ForeignKey("pricelist_line.id", ondelete="CASCADE"), nullable=False)
    currency = db.Column(db.String(8), nullable=False)
    amount = db.Column(db.Numeric(16, 4), nullable=False)
    valid_from = db.Column(db.Date, default=date.today)
    valid_until = db.Column(db.Date)
    note = db.Column(db.String(255))
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    line = db.relationship("PricelistLine", back_populates="override")

    def is_in_window(self, on=None):
        on = on or date.today()
        if self.valid_from and on < self.valid_from:
            return False
        if self.valid_until and on > self.valid_until:
            return False
        return True


# ---------------------------------------------------------------------------
# Exchange rates
# ---------------------------------------------------------------------------
class ExchangeRate(db.Model):
    """Rate expressed as: 1 unit of ``quote_ccy`` = ``rate`` units of ``base_ccy`` (UGX)."""
    __tablename__ = "exchange_rate"
    id = db.Column(db.Integer, primary_key=True)
    base_ccy = db.Column(db.String(8), nullable=False, default="UGX")
    quote_ccy = db.Column(db.String(8), nullable=False)        # USD, TZS, ...
    rate = db.Column(db.Numeric(16, 6), nullable=False)        # UGX per 1 quote unit
    effective_date = db.Column(db.Date, nullable=False, default=date.today)
    expiry_date = db.Column(db.Date)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def is_in_window(self, on=None):
        on = on or date.today()
        if on < self.effective_date:
            return False
        if self.expiry_date and on > self.expiry_date:
            return False
        return True

    def status_for(self, on=None):
        on = on or date.today()
        if not self.expiry_date:
            return "open"
        days = (self.expiry_date - on).days
        if days < 0:
            return "expired"
        if days <= 7:
            return "expiring"
        return "valid"


# ---------------------------------------------------------------------------
# Offers / quotes
# ---------------------------------------------------------------------------
class Offer(db.Model):
    __tablename__ = "offer"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(32), unique=True, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False)
    source_pricelist_id = db.Column(db.Integer, db.ForeignKey("pricelist.id"))
    currency = db.Column(db.String(8), nullable=False, default="UGX")
    market = db.Column(db.String(16), default="local")
    vat_applicable = db.Column(db.Boolean, default=True)
    vat_rate = db.Column(db.Float, default=18.0)
    # Stamped rate that applied when the offer was created (UGX per 1 unit currency).
    exchange_rate_value = db.Column(db.Numeric(16, 6))
    exchange_rate_id = db.Column(db.Integer, db.ForeignKey("exchange_rate.id"))
    # draft | issued | converted | not_ordered | archived
    status = db.Column(db.String(16), default="draft")
    valid_from = db.Column(db.Date, default=date.today)
    valid_until = db.Column(db.Date)
    notes = db.Column(db.Text)
    converted_order_id = db.Column(db.Integer, db.ForeignKey("sales_order.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer", back_populates="offers")
    source_pricelist = db.relationship("Pricelist")
    converted_order = db.relationship("SalesOrder", foreign_keys=[converted_order_id])
    lines = db.relationship("OfferLine", back_populates="offer",
                            order_by="OfferLine.sort_order", cascade="all, delete-orphan")

    @property
    def subtotal(self):
        return sum((l.line_total or 0) for l in self.lines)

    @property
    def vatable_subtotal(self):
        if not self.vat_applicable:
            return 0
        return sum((l.line_total or 0) for l in self.lines if l.is_vatable)

    @property
    def vat_amount(self):
        return vat_money(self.vatable_subtotal, self.vat_rate)

    @property
    def total(self):
        return self.subtotal + self.vat_amount

    def status_for(self, on=None):
        on = on or date.today()
        if not self.valid_until:
            return "open"
        days = (self.valid_until - on).days
        if days < 0:
            return "expired"
        if days <= 7:
            return "expiring"
        return "valid"


class OfferLine(db.Model):
    __tablename__ = "offer_line"
    id = db.Column(db.Integer, primary_key=True)
    offer_id = db.Column(db.Integer, db.ForeignKey("offer.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"))
    description = db.Column(db.String(255))     # snapshot
    article_no = db.Column(db.String(32))       # snapshot
    pack_size = db.Column(db.String(64))
    tier_label = db.Column(db.String(96))
    quantity = db.Column(db.Float, default=1)
    unit_price = db.Column(db.Numeric(16, 4))   # in offer currency, stamped
    discount_pct = db.Column(db.Float, default=0)
    is_fixed = db.Column(db.Boolean, default=False)
    fixed_note = db.Column(db.String(255))
    # VAT status snapshotted at line creation (M8) so toggling the product flag
    # later does not restate an issued offer. Null on old rows -> fall back live.
    vat_applicable = db.Column(db.Boolean, nullable=True)
    sort_order = db.Column(db.Integer, default=0)

    offer = db.relationship("Offer", back_populates="lines")
    product = db.relationship("Product")

    @property
    def is_vatable(self):
        if self.vat_applicable is not None:
            return bool(self.vat_applicable)
        return bool(self.product and self.product.vat_applicable)

    @property
    def line_total(self):
        return line_money(self.unit_price, self.quantity, self.discount_pct)


# ---------------------------------------------------------------------------
# Sales orders (placed by reps for customers)
# ---------------------------------------------------------------------------
class SalesOrder(db.Model):
    __tablename__ = "sales_order"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(32), unique=True, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), nullable=False, index=True)
    source_pricelist_id = db.Column(db.Integer, db.ForeignKey("pricelist.id"))
    currency = db.Column(db.String(8), nullable=False, default="UGX")
    market = db.Column(db.String(16), default="local")
    vat_applicable = db.Column(db.Boolean, default=True)
    vat_rate = db.Column(db.Float, default=18.0)
    exchange_rate_value = db.Column(db.Numeric(16, 6))
    exchange_rate_id = db.Column(db.Integer, db.ForeignKey("exchange_rate.id"))
    # draft | submitted | placed | in_fulfillment | pending | ready_for_dispatch
    # | out_for_delivery | delivered | fulfilled | cancelled
    status = db.Column(db.String(20), default="draft", index=True)
    lpo_filename = db.Column(db.String(255))   # uploaded customer LPO (optional)
    submitted_at = db.Column(db.DateTime)
    order_date = db.Column(db.Date, default=date.today, index=True)
    delivery_date = db.Column(db.Date)
    delivery_time = db.Column(db.String(8))   # requested delivery time "HH:MM"
    delivery_address = db.Column(db.Text)
    customer_po = db.Column(db.String(64))
    payment_terms = db.Column(db.String(64))
    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    placed_at = db.Column(db.DateTime)
    accepted_at = db.Column(db.DateTime)
    accepted_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))  # M1: optional ref
    credit_checked = db.Column(db.Boolean, default=False)
    fulfilment_started_at = db.Column(db.DateTime)
    dispatched_at = db.Column(db.DateTime)     # = out for delivery (driver en route)
    fulfilled_at = db.Column(db.DateTime)      # fulfilment completed (delivery note made)
    delivered_at = db.Column(db.DateTime)      # driver confirmed delivery
    # delivery note
    dnote_number = db.Column(db.String(32))
    dnote_at = db.Column(db.DateTime)
    # dispatch / driver
    assigned_driver_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))  # M1: optional ref
    assigned_at = db.Column(db.DateTime)
    driver_accepted_at = db.Column(db.DateTime)
    pod_filename = db.Column(db.String(255))   # signed delivery-note photo (proof of delivery)
    # Mobile money on delivery (26 Jul 2026): no cash on trucks. For COD
    # orders the customer pays by mobile money and the driver enters the
    # transaction reference BEFORE handing over the goods; delivery cannot be
    # confirmed without it. Finance matches references to the MoMo statement.
    momo_ref = db.Column(db.String(40))
    momo_ref_at = db.Column(db.DateTime)
    backorder_of_id = db.Column(db.Integer, db.ForeignKey("sales_order.id"), index=True)
    bo_confirm_state = db.Column(db.String(12))  # for backorders: proposed | confirmed | declined
    stock_deducted = db.Column(db.Boolean, default=False)  # stock taken off at fulfilment
    # Credit-limit release (21 Jul 2026): CEO/CFO/finance manager may release
    # ONE held order past the customer's credit limit from the alerts queue.
    credit_override_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    credit_override_at = db.Column(db.DateTime)
    # Invoice-after-delivery flow (31 Jul 2026): the invoicing clerk records
    # the quantities the customer accepted (from the signed delivery note)
    # and only then posts the fiscal invoice. Kills the credit-note volume
    # that direct invoicing at fulfilment produced.
    accepted_recorded_at = db.Column(db.DateTime)
    accepted_recorded_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    # Physical signed delivery note filed in finance: the filing number is the
    # proof-of-delivery reference, linked from the invoice.
    dnote_filing_no = db.Column(db.String(20), unique=True)
    dnote_filed_at = db.Column(db.DateTime)
    dnote_filed_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    # customer delivery feedback (rated from the portal after delivery)
    rating = db.Column(db.Integer)              # 1-5 stars
    rating_comment = db.Column(db.Text)
    rated_at = db.Column(db.DateTime)
    feedback_ack = db.Column(db.Boolean, default=False)   # reviewed by staff (clears the dashboard)
    feedback_ack_at = db.Column(db.DateTime)

    customer = db.relationship("Customer")
    accepted_by = db.relationship("User", foreign_keys=[accepted_by_id])
    accepted_recorded_by = db.relationship("User", foreign_keys=[accepted_recorded_by_id])
    dnote_filed_by = db.relationship("User", foreign_keys=[dnote_filed_by_id])
    assigned_driver = db.relationship("User", foreign_keys=[assigned_driver_id])
    source_pricelist = db.relationship("Pricelist")
    parent_order = db.relationship("SalesOrder", remote_side=[id], backref="backorders")
    lines = db.relationship("SalesOrderLine", back_populates="order",
                            order_by="SalesOrderLine.sort_order",
                            cascade="all, delete-orphan")

    def outstanding_items(self):
        """Lines with an undelivered balance: list of (line, outstanding_qty)."""
        out = []
        for l in self.lines:
            delivered = l.fulfilled_qty if l.fulfilled_qty is not None else (l.quantity or 0)
            short = (l.quantity or 0) - delivered
            if short > 0:
                out.append((l, short))
        return out

    @property
    def has_outstanding(self):
        return bool(self.outstanding_items())

    @property
    def has_shortfall_now(self):
        """Will completing now leave a shortfall? (under-delivered or out of stock)"""
        for l in self.lines:
            delivered = l.fulfilled_qty if l.fulfilled_qty is not None else (l.quantity or 0)
            if (l.quantity or 0) - delivered > 1e-9 or l.availability == "out_of_stock":
                return True
        return False

    @property
    def backorder(self):
        return self.backorders[0] if self.backorders else None

    @property
    def is_cod(self):
        """Cash on delivery (26 Jul 2026): the payment terms read cash/COD,
        or the customer runs on the 1-day cash rule from the credit load.
        Drives the collect banner on the driver's screen and the payment
        line on the delivery note. Prepaid terms are NOT cod — that money
        moved before the truck did."""
        t = (self.payment_terms
             or (self.customer.payment_terms if self.customer else "") or "").lower()
        if "prepaid" in t or "pre-paid" in t:
            return False
        if "cash" in t or "cod" in t:
            return True
        return bool(self.customer and (self.customer.credit_days or 0) == 1)

    @property
    def short_stock_lines(self):
        """Lines whose ordered quantity exceeds current stock on hand.
        Guides the order manager at acceptance; this is not a reservation."""
        return [l for l in self.lines
                if l.product is not None
                and (l.quantity or 0) > (l.product.stock_on_hand or 0)]

    @property
    def subtotal(self):
        return sum((l.line_total or 0) for l in self.lines)

    @property
    def vatable_subtotal(self):
        """Net value of the lines that actually carry VAT (export = none)."""
        if not self.vat_applicable:
            return 0
        return sum((l.line_total or 0) for l in self.lines if l.is_vatable)

    @property
    def nonvat_subtotal(self):
        return self.subtotal - self.vatable_subtotal

    @property
    def vat_amount(self):
        return vat_money(self.vatable_subtotal, self.vat_rate)

    @property
    def total(self):
        return self.subtotal + self.vat_amount

    # ---- dispatched view (31 Jul 2026): what physically left on the truck.
    # The delivery note prints these; once the clerk records accepted
    # quantities, subtotal/total switch to the accepted (billed) view while
    # the delivery note keeps showing what was sent.
    @property
    def dispatched_subtotal(self):
        return sum((l.dispatched_total or 0) for l in self.lines)

    @property
    def dispatched_vatable_subtotal(self):
        if not self.vat_applicable:
            return 0
        return sum((l.dispatched_total or 0) for l in self.lines if l.is_vatable)

    @property
    def dispatched_vat_amount(self):
        return vat_money(self.dispatched_vatable_subtotal, self.vat_rate)

    @property
    def dispatched_total(self):
        return self.dispatched_subtotal + self.dispatched_vat_amount

    @property
    def has_variance(self):
        return any((l.variance_qty or 0) > 1e-9 for l in self.lines)

    @property
    def variance_lines(self):
        return [l for l in self.lines if (l.variance_qty or 0) > 1e-9]

    @property
    def is_editable(self):
        return self.status == "draft"

    @property
    def is_amendable(self):
        """Staff may amend order details (delivery date, address, PO, notes)
        until the order leaves for delivery."""
        return self.status in ("draft", "submitted", "placed",
                               "in_fulfillment", "pending")

    @property
    def is_lines_amendable(self):
        """Lines are frozen at acceptance (21 Jul 2026): once the order
        manager accepts, no item is added, removed, or re-quantified. Under-
        delivery goes through the To deliver column and the back-order route,
        keeping every ordered item on record."""
        return self.status in ("draft", "submitted", "placed")


# Delivery variance reasons (31 Jul 2026). The bool says whether goods
# physically come back to the store with the driver: True re-adds the
# shortfall to stock at invoicing; False means the goods are gone (scale
# difference at the customer, or a loss) and the shortfall stays out of
# stock but carries its cost into COGS so the margin stays honest.
VARIANCE_REASONS = {
    "weighed_short":    ("Weighed short at customer — no goods returned", False),
    "rejected_quality": ("Rejected on quality — returned to store", True),
    "damaged":          ("Damaged in transit — returned to store", True),
    "short_loaded":     ("Short loaded — never left the store", True),
    "not_ordered":      ("Refused, not ordered — returned to store", True),
    "other_loss":       ("Other — no goods returned", False),
}


class SalesOrderLine(db.Model):
    __tablename__ = "sales_order_line"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("sales_order.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), index=True)
    description = db.Column(db.String(255))
    article_no = db.Column(db.String(32))
    pack_size = db.Column(db.String(64))
    tier_label = db.Column(db.String(96))
    quantity = db.Column(db.Float, default=1)            # ordered quantity
    fulfilled_qty = db.Column(db.Float)                  # dispatched quantity (set in fulfilment)
    # Invoice-after-delivery (31 Jul 2026): quantity the customer accepted on
    # the signed delivery note, keyed by the invoicing clerk. The invoice
    # bills this; the gap to fulfilled_qty is the delivery variance.
    accepted_qty = db.Column(db.Float)
    variance_reason = db.Column(db.String(24))           # key into VARIANCE_REASONS
    unit_price = db.Column(db.Numeric(16, 4))
    discount_pct = db.Column(db.Float, default=0)
    is_fixed = db.Column(db.Boolean, default=False)
    fixed_note = db.Column(db.String(255))
    # VAT status snapshotted at line creation (M8). Null on old rows -> live.
    vat_applicable = db.Column(db.Boolean, nullable=True)
    # Per-line tax category chosen at order entry (21 Jul 2026):
    # vat (18%) | exempt (–) | zero (0%, export orders only). NULL = derive
    # from the export flag / product VAT flag (see effective_tax_category).
    tax_category = db.Column(db.String(8))
    # available | out_of_stock | not_delivered  (set during fulfilment)
    availability = db.Column(db.String(16), default="available")
    expected_restock = db.Column(db.Date)            # optional: when an OOS item is expected back
    customer_notified_at = db.Column(db.DateTime)    # when the customer was told it is out of stock
    sort_order = db.Column(db.Integer, default=0)

    order = db.relationship("SalesOrder", back_populates="lines")
    product = db.relationship("Product")

    @property
    def effective_tax_category(self):
        """The line's tax category: 'vat' (18%), 'exempt' (–) or 'zero' (0%,
        export). Explicit per-line choice first (21 Jul 2026 — products are
        auto-created from pricelists, so their VAT flags are not gospel);
        otherwise derived: zero on export orders, else the snapshot/product
        VAT flag decides between vat and exempt."""
        if self.tax_category in ("vat", "exempt", "zero"):
            return self.tax_category
        o = self.order
        if o is not None and not o.vat_applicable:
            return "zero" if (o.market or "local") == "export" else "exempt"
        if self.vat_applicable is not None:
            return "vat" if self.vat_applicable else "exempt"
        return "vat" if (self.product and self.product.vat_applicable) else "exempt"

    @property
    def is_vatable(self):
        return self.effective_tax_category == "vat"

    @property
    def delivered_qty(self):
        """Quantity used for value: accepted (once the clerk records it from
        the signed delivery note), else dispatched, else ordered."""
        if self.accepted_qty is not None:
            return self.accepted_qty
        return self.fulfilled_qty if self.fulfilled_qty is not None else (self.quantity or 0)

    @property
    def dispatched_qty(self):
        """What physically left on the truck — the delivery note prints this,
        whether or not accepted quantities were recorded later."""
        return self.fulfilled_qty if self.fulfilled_qty is not None else (self.quantity or 0)

    @property
    def variance_qty(self):
        """Dispatched minus accepted; 0 until the clerk records acceptance."""
        if self.accepted_qty is None:
            return 0
        return max((self.dispatched_qty or 0) - (self.accepted_qty or 0), 0)

    @property
    def variance_returns_goods(self):
        entry = VARIANCE_REASONS.get(self.variance_reason or "")
        return bool(entry and entry[1])

    @property
    def variance_reason_label(self):
        entry = VARIANCE_REASONS.get(self.variance_reason or "")
        return entry[0] if entry else (self.variance_reason or "")

    @property
    def cogs_extra_qty(self):
        """Variance the customer never accepted AND the driver never brought
        back (weighed short, other loss): the goods are gone, so their cost
        joins the COGS of the sale instead of pretending they still exist."""
        v = self.variance_qty or 0
        if v <= 0 or self.variance_returns_goods:
            return 0
        return v

    @property
    def line_total(self):
        return line_money(self.unit_price, self.delivered_qty, self.discount_pct)

    @property
    def dispatched_total(self):
        return line_money(self.unit_price, self.dispatched_qty, self.discount_pct)

    @property
    def variance_value(self):
        return line_money(self.unit_price, self.variance_qty, self.discount_pct)

    @property
    def ordered_total(self):
        return line_money(self.unit_price, self.quantity, self.discount_pct)


# ---------------------------------------------------------------------------
# Audit log & settings
# ---------------------------------------------------------------------------
class AuditLog(db.Model):
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    username = db.Column(db.String(64))
    action = db.Column(db.String(48))          # price_change | rate_change | login | ...
    entity_type = db.Column(db.String(48))
    entity_id = db.Column(db.Integer)
    field = db.Column(db.String(64))
    old_value = db.Column(db.String(255))
    new_value = db.Column(db.String(255))
    detail = db.Column(db.String(512))

    user = db.relationship("User")


class Setting(db.Model):
    __tablename__ = "setting"
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(255))


class Announcement(db.Model):
    """A promotion/update posted to the customer portal, audience-targeted."""
    __tablename__ = "announcement"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.Text)
    image_filename = db.Column(db.String(255))
    audience = db.Column(db.String(16), default="all")   # all | customers | distributors
    is_active = db.Column(db.Boolean, default=True)
    valid_from = db.Column(db.Date)
    valid_until = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))

    def is_live(self, on=None):
        on = on or date.today()
        if not self.is_active:
            return False
        if self.valid_from and on < self.valid_from:
            return False
        if self.valid_until and on > self.valid_until:
            return False
        return True

    def matches(self, segment):
        if self.audience == "all":
            return True
        if self.audience == "distributors":
            return segment == "distributor"
        return segment != "distributor"   # 'customers'


class Message(db.Model):
    """A message in a customer's conversation thread with the company."""
    __tablename__ = "message"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_type = db.Column(db.String(10), nullable=False)   # customer | staff
    sender_user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    sender_name = db.Column(db.String(128))
    body = db.Column(db.Text, nullable=False)
    # M1: a message outlives its order; the link nulls if the order goes.
    order_id = db.Column(db.Integer, db.ForeignKey("sales_order.id", ondelete="SET NULL"))  # optional: links to an order
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    read_by_customer = db.Column(db.Boolean, default=False)
    read_by_staff = db.Column(db.Boolean, default=False)
    # When this staff message was covered by a notification email (sent or
    # folded into one). NULL = not yet notified; the sweep in services/notify
    # emails messages still unread ~10 minutes after creation.
    emailed_at = db.Column(db.DateTime)

    customer = db.relationship("Customer")
    order = db.relationship("SalesOrder")


class CreditAlert(db.Model):
    """An order held for finance because the customer's account is blocked or
    not credit-cleared. One open alert per order. Finance (User.can_clear_credit)
    decides: unblock (account back to ok and cleared, order may be accepted) or
    keep blocked (the customer and the reps are told the order is on hold)."""
    __tablename__ = "credit_alert"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("sales_order.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    reason = db.Column(db.String(24), nullable=False)   # blocked | not_cleared
    status = db.Column(db.String(16), nullable=False, default="open", index=True)
    # open | unblocked | kept_blocked
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    emailed_at = db.Column(db.DateTime)                 # finance alert mail sent
    decided_by_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    decided_at = db.Column(db.DateTime)
    note = db.Column(db.String(255))                    # finance's reason/comment

    order = db.relationship("SalesOrder")
    customer = db.relationship("Customer")
    decided_by = db.relationship("User", foreign_keys=[decided_by_id])


class Contact(db.Model):
    """A named person at a customer (for reps and telesales to call/visit)."""
    __tablename__ = "contact"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    name = db.Column(db.String(128), nullable=False)
    title = db.Column(db.String(96))        # role / job title
    phone = db.Column(db.String(64))
    email = db.Column(db.String(128))
    is_primary = db.Column(db.Boolean, default=False)
    notes = db.Column(db.Text)
    archived = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer", back_populates="contacts")


class Activity(db.Model):
    """One logged interaction with a customer: visit, call, email, note, meeting."""
    __tablename__ = "activity"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    contact_id = db.Column(db.Integer, db.ForeignKey("contact.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))   # who logged it
    kind = db.Column(db.String(16), default="visit")           # visit|call|email|note|meeting
    direction = db.Column(db.String(10), default="outbound")    # outbound|inbound (calls)
    occurred_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    outcome = db.Column(db.String(40))                          # disposition
    summary = db.Column(db.Text)                                # what transpired
    next_action = db.Column(db.String(255))
    next_action_date = db.Column(db.Date)
    follow_up_done = db.Column(db.Boolean, default=False)
    latitude = db.Column(db.Float)                              # GPS check-in (visits)
    longitude = db.Column(db.Float)
    checkin_at = db.Column(db.DateTime)
    recording_url = db.Column(db.String(500))                   # optional call recording link
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    customer = db.relationship("Customer", back_populates="activities")
    contact = db.relationship("Contact")
    user = db.relationship("User")

    KIND_LABELS = {"visit": "Visit", "call": "Call", "email": "Email", "sms": "SMS",
                   "note": "Note", "meeting": "Meeting"}
    KIND_ICONS = {"visit": "bi-geo-alt", "call": "bi-telephone", "email": "bi-envelope",
                  "sms": "bi-chat-text", "note": "bi-sticky", "meeting": "bi-people"}

    @property
    def kind_label(self):
        return self.KIND_LABELS.get(self.kind, self.kind.title())

    @property
    def kind_icon(self):
        return self.KIND_ICONS.get(self.kind, "bi-dot")

    @property
    def has_gps(self):
        return self.latitude is not None and self.longitude is not None


class Deal(db.Model):
    """A sales opportunity in the pipeline for a customer."""
    __tablename__ = "deal"
    STAGES = ["Lead", "Contacted", "Quoted", "Negotiation", "Won", "Lost"]
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    title = db.Column(db.String(160), nullable=False)
    value = db.Column(db.Numeric(16, 2))
    currency = db.Column(db.String(8), default="UGX")
    stage = db.Column(db.String(24), default="Lead", index=True)
    status = db.Column(db.String(10), default="open")   # open | won | lost
    expected_close = db.Column(db.Date)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    closed_at = db.Column(db.DateTime)

    customer = db.relationship("Customer", back_populates="deals")
    owner = db.relationship("User")


class CallList(db.Model):
    """A scheduled list of customers for a telesales shift."""
    __tablename__ = "call_list"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    due_date = db.Column(db.Date)
    notes = db.Column(db.Text)
    archived = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    assigned_to = db.relationship("User", foreign_keys=[assigned_to_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    items = db.relationship("CallListItem", back_populates="call_list",
                            order_by="CallListItem.sort_order",
                            cascade="all, delete-orphan")

    @property
    def done_count(self):
        return sum(1 for i in self.items if i.status == "done")

    @property
    def total_count(self):
        return len(self.items)


class CallListItem(db.Model):
    __tablename__ = "call_list_item"
    id = db.Column(db.Integer, primary_key=True)
    call_list_id = db.Column(db.Integer, db.ForeignKey("call_list.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    status = db.Column(db.String(10), default="pending")   # pending | done | skipped
    sort_order = db.Column(db.Integer, default=0)
    activity_id = db.Column(db.Integer, db.ForeignKey("activity.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    call_list = db.relationship("CallList", back_populates="items")
    customer = db.relationship("Customer")
    activity = db.relationship("Activity")


class Store(db.Model):
    """A storage location. The 'sellable' store (Coldroom) holds catalogue stock
    that sales deduct from; other stores hold production/materials items."""
    __tablename__ = "store"
    KINDS = {"sellable": "Sellable (Coldroom)", "production": "Production",
             "materials": "Materials / Dry"}
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(96), nullable=False)
    kind = db.Column(db.String(16), default="production")
    sort_order = db.Column(db.Integer, default=0)
    archived = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship("StoreItem", back_populates="store",
                            order_by="StoreItem.name", cascade="all, delete-orphan")

    @property
    def is_sellable(self):
        return self.kind == "sellable"

    @property
    def kind_label(self):
        return self.KINDS.get(self.kind, self.kind.title())


class StoreItem(db.Model):
    """A free-form item held in a non-sellable store (raw cuts, packaging, spices)."""
    __tablename__ = "store_item"
    id = db.Column(db.Integer, primary_key=True)
    store_id = db.Column(db.Integer, db.ForeignKey("store.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(96))
    pack_size = db.Column(db.String(64))
    uom = db.Column(db.String(24))
    origin = db.Column(db.String(48))
    quantity = db.Column(db.Float, default=0)
    low_level = db.Column(db.Float, default=0)
    note = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    store = db.relationship("Store", back_populates="items")

    @property
    def is_low(self):
        return (self.low_level or 0) > 0 and (self.quantity or 0) <= self.low_level


class StockMovement(db.Model):
    """Every change to a product's stock: receipt, wastage, adjustment, sale, return."""
    __tablename__ = "stock_movement"
    KINDS = {"receipt": "Stock in", "wastage": "Wastage / loss",
             "adjustment": "Adjustment", "sale": "Sale (delivered)",
             "return": "Return to stock", "production": "Production (made in)"}
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    qty = db.Column(db.Float, nullable=False)        # signed: + in, - out
    kind = db.Column(db.String(16), nullable=False)
    balance_after = db.Column(db.Float)              # on-hand after this movement
    note = db.Column(db.String(255))
    # Batch/lot traceability (QA audit 5 Jul 2026): which lot moved, and its
    # expiry. Set on receipts and production; recall = query by lot_number.
    lot_number = db.Column(db.String(64), index=True)
    expiry = db.Column(db.Date)
    order_id = db.Column(db.Integer, db.ForeignKey("sales_order.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    product = db.relationship("Product")
    user = db.relationship("User")

    @property
    def kind_label(self):
        return self.KINDS.get(self.kind, self.kind.title())


class StockCount(db.Model):
    """A stock take (spot / daily / weekly / monthly). Holds counted lines and,
    when posted, adjusts product stock to the physical figures."""
    __tablename__ = "stock_count"
    KINDS = {"spot": "Spot check", "daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(12), default="spot")
    status = db.Column(db.String(10), default="open")   # open | posted
    note = db.Column(db.String(255))
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    posted_at = db.Column(db.DateTime)

    created_by = db.relationship("User")
    lines = db.relationship("StockCountLine", back_populates="count",
                            order_by="StockCountLine.id", cascade="all, delete-orphan")

    @property
    def kind_label(self):
        return self.KINDS.get(self.kind, self.kind.title())

    @property
    def discrepancy_lines(self):
        return [l for l in self.lines if l.discrepancy not in (0, None)]


class StockCountLine(db.Model):
    __tablename__ = "stock_count_line"
    id = db.Column(db.Integer, primary_key=True)
    count_id = db.Column(db.Integer, db.ForeignKey("stock_count.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    system_qty = db.Column(db.Float)     # on-hand snapshot when the line was entered
    counted_qty = db.Column(db.Float)    # physical count
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    count = db.relationship("StockCount", back_populates="lines")
    product = db.relationship("Product")

    @property
    def discrepancy(self):
        if self.counted_qty is None or self.system_qty is None:
            return None
        return round(self.counted_qty - self.system_qty, 4)


class ImportReport(db.Model):
    """Stored summary of an import run, including rows that failed to parse."""
    __tablename__ = "import_report"
    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.DateTime, default=datetime.utcnow)
    source = db.Column(db.String(255))
    rows_ok = db.Column(db.Integer, default=0)
    rows_failed = db.Column(db.Integer, default=0)
    detail = db.Column(db.Text)   # newline-joined failure descriptions


class SalesHistory(db.Model):
    """Historical invoiced sales loaded from the yearly pivot export
    (per customer -> product -> year). Untaxed UGX, net of returns. Used for
    analytics only; it does not create orders."""
    __tablename__ = "sales_history"
    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), index=True)  # matched, may be null
    customer_name = db.Column(db.String(180), index=True)   # raw name from the export
    product = db.Column(db.String(200))
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), index=True)  # linked catalogue product (null = off-catalogue)
    year = db.Column(db.Integer, index=True)
    month = db.Column(db.Integer, index=True)   # 1-12 (monthly grain); null = annual only
    revenue = db.Column(db.Numeric(18, 2))   # untaxed total, net
    quantity = db.Column(db.Float)
    is_return = db.Column(db.Boolean, default=False)   # net-negative line (credit/return)

    customer = db.relationship("Customer")
    linked_product = db.relationship("Product")


class CustomerPayment(db.Model):
    """Customer payments imported from the Odoo 'Payments (account.payment)'
    export (27 Jul 2026). One row per posted payment, upserted by number.
    Statement companion to Invoice: together they carry both sides of the
    customer ledger. Analytics/statements only — the app's own receipts live
    in AccReceipt."""
    __tablename__ = "customer_payment"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), unique=True, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), index=True)
    customer_name = db.Column(db.String(180), index=True)
    payment_date = db.Column(db.Date, index=True)
    amount = db.Column(db.Numeric(18, 2))
    journal = db.Column(db.String(80))       # bank / cash / mobile money book
    method = db.Column(db.String(40))
    status = db.Column(db.String(20))

    customer = db.relationship("Customer")


class Invoice(db.Model):
    """Historical invoice headers imported from the accounting export (Odoo).
    One row per posted invoice. Analytics only — does not create orders."""
    __tablename__ = "invoice"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), index=True)        # Odoo invoice number
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), index=True)  # matched, may be null
    customer_name = db.Column(db.String(180), index=True)
    salesperson = db.Column(db.String(160), index=True)
    invoice_date = db.Column(db.Date, index=True)
    due_date = db.Column(db.Date)
    untaxed = db.Column(db.Numeric(18, 2))   # untaxed amount, original currency
    total = db.Column(db.Numeric(18, 2))     # taxed total
    currency = db.Column(db.String(8), default="UGX")
    payment_status = db.Column(db.String(20), index=True)  # Paid | Not Paid | Partially Paid | Reversed | In Payment
    company_type = db.Column(db.String(20))               # Business | Consumer | Government | Foreigner
    efris = db.Column(db.String(40))

    customer = db.relationship("Customer")
    lines = db.relationship("InvoiceLine", back_populates="invoice",
                            cascade="all, delete-orphan",
                            order_by="InvoiceLine.id")

    @property
    def is_outstanding(self):
        return self.payment_status in ("Not Paid", "Partially Paid", "In Payment")


class InvoiceLine(db.Model):
    """One product line of an imported invoice (Odoo itemized export,
    6 Jul 2026). Amounts are NET (excl VAT) in the invoice currency, sign
    already normalised (sales positive, credits negative). product_id is
    matched from the Odoo product label via the curated sales_history
    linkage where possible; the raw label is always kept."""
    __tablename__ = "invoice_line"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer,
                           db.ForeignKey("invoice.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    product_name = db.Column(db.String(255), index=True)   # Odoo label, as exported
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"))  # matched, may be null
    quantity = db.Column(db.Float)
    amount = db.Column(db.Numeric(18, 2))                  # net, invoice currency

    invoice = db.relationship("Invoice", back_populates="lines")
    product = db.relationship("Product")


class RepTarget(db.Model):
    """Monthly sales targets a Sales Manager sets for a rep. Three optional
    levels: an overall monthly total, per-customer, and per-product. A level
    that is not set simply does not appear on the rep's dashboard."""
    __tablename__ = "rep_target"
    __table_args__ = (
        UniqueConstraint("rep_id", "year", "month", "scope", "customer_id",
                         "product_id", name="uq_rep_target_scope"),
    )
    id = db.Column(db.Integer, primary_key=True)
    rep_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True, nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    scope = db.Column(db.String(12), nullable=False, default="total")  # total | customer | product
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"))
    amount = db.Column(db.Numeric(18, 2), default=0)   # UGX target
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    rep = db.relationship("User")
    customer = db.relationship("Customer")
    product = db.relationship("Product")


class ApiKey(db.Model):
    """A token for machine-to-machine API access (accounting/ERP, mobile, scripts).

    The raw key is shown once at creation and never stored; only a bcrypt hash is
    kept, exactly like a user password. ``scope`` is 'read' or 'read_write'. Each
    call carries the key in the ``Authorization: Bearer <key>`` header. Actions are
    attributed to ``acts_as_user`` so audit trails and created_by stay meaningful.
    """
    __tablename__ = "api_key"
    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(120), nullable=False)
    prefix = db.Column(db.String(12), nullable=False, index=True)   # first chars, for lookup
    key_hash = db.Column(db.String(255), nullable=False)            # bcrypt of the full key
    scope = db.Column(db.String(16), nullable=False, default="read")  # read | read_write
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    acts_as_user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    last_used_at = db.Column(db.DateTime)
    request_count = db.Column(db.Integer, nullable=False, default=0)

    acts_as_user = db.relationship("User", foreign_keys=[acts_as_user_id])
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    @property
    def can_write(self):
        return self.scope == "read_write"


class PriceApproval(db.Model):
    """A pending change a pricing officer made that needs sign-off before going
    live. kind = price (a pricelist's staged price changes) | product (a new
    product) | pricelist (a new/uploaded/duplicated pricelist)."""
    __tablename__ = "price_approval"
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(12), nullable=False, index=True)
    status = db.Column(db.String(12), nullable=False, default="pending", index=True)
    summary = db.Column(db.String(255))
    pricelist_id = db.Column(db.Integer, db.ForeignKey("pricelist.id", ondelete="CASCADE"))
    product_id = db.Column(db.Integer, db.ForeignKey("product.id", ondelete="CASCADE"))
    promo_id = db.Column(db.Integer, db.ForeignKey("promo_price.id", ondelete="CASCADE"))
    requested_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    requested_at = db.Column(db.DateTime, default=datetime.utcnow)
    decided_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    decided_at = db.Column(db.DateTime)
    decision_note = db.Column(db.String(255))

    pricelist = db.relationship("Pricelist")
    product = db.relationship("Product")
    promo = db.relationship("PromoPrice")
    requested_by = db.relationship("User", foreign_keys=[requested_by_id])
    decided_by = db.relationship("User", foreign_keys=[decided_by_id])
# ---------------------------------------------------------------------------
# Production planning (Phase 1) — additive only. Production is REPLENISHMENT:
# orders are served from stock, and production tops stock back up. The system
# computes, per product, demand from open orders versus stock on hand and
# suggests a quantity to produce when stock falls short. There is no per-order
# allocation. Produced goods go into general stock. See SCHEMA_MAP.md.
# ---------------------------------------------------------------------------
class ProdProduction(db.Model):
    """A record of finished goods produced into stock. One row per recorded
    production. Each row links to the StockMovement (kind 'production') that
    added the quantity to the product's on-hand. This is the production history;
    the shortfall to produce is always computed live from orders versus stock."""
    __tablename__ = "prod_production"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"),
                           nullable=False, index=True)
    qty = db.Column(db.Float, nullable=False, default=0)   # produced (product unit, catch weight)
    note = db.Column(db.String(255))
    # Batch/lot traceability (QA audit 5 Jul 2026): every production run gets
    # a lot number (auto-generated when not supplied) and an optional expiry.
    lot_number = db.Column(db.String(64), index=True)
    expiry = db.Column(db.Date)
    recorded_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    stock_movement_id = db.Column(db.Integer, db.ForeignKey("stock_movement.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    product = db.relationship("Product")
    recorded_by = db.relationship("User")
    stock_movement = db.relationship("StockMovement")


# ---------------------------------------------------------------------------
# Phase 2 — product-to-recipe mapping (pricing.db) and READ-ONLY views of the
# costing app's recipe tables (bind 'costing'). The costing models are never
# written to. They mirror only the columns the production layer reads.
# ---------------------------------------------------------------------------
class ProdRecipeMap(db.Model):
    """Links a sellable pricing product to its recipe in the costing app.

    The link does not exist in either app's own data, so it is built here once,
    proposed by name match and confirmed by a manager. recipe_id refers to
    costing.recipes.id (a different database); it is stored as a plain integer,
    not a foreign key, because the two databases are separate files."""
    __tablename__ = "prod_recipe_map"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"),
                           nullable=False, unique=True, index=True)
    recipe_id = db.Column(db.Integer, nullable=False)        # costing.recipes.id
    recipe_name = db.Column(db.String(255))                  # snapshot for display
    match_method = db.Column(db.String(12), default="manual")  # auto | manual
    confirmed = db.Column(db.Boolean, default=True)
    confirmed_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    confirmed_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    product = db.relationship("Product")
    confirmed_by = db.relationship("User")


# ---------------------------------------------------------------------------
# COSTING MODULE (native) — ported from the standalone meat-costing-app.
# These are the EDITABLE costing tables, now living inside pricing.db. The
# costing engine (services/costing_engine.py) computes every cost live from
# these rows; recipe_sync copies them into the prod_* tables the production
# and inventory layers read. Original table names kept so the one-time data
# import from costing.db is a straight row copy with ids preserved.
# ---------------------------------------------------------------------------
def utcnow():
    return datetime.utcnow()


COSTING_SPECIES = ["Pork", "Beef", "Chicken", "Lamb", "Goat"]


class CostCategory(db.Model):
    __tablename__ = "categories"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    display_order = db.Column(db.Integer, nullable=False, default=0)
    recipes = db.relationship("CostRecipe", back_populates="category")

    @property
    def active_recipe_count(self):
        return sum(1 for r in self.recipes if r.status == "active")


class CostSetting(db.Model):
    """Key/value store for costing globals (overhead, VAT, margins...)."""
    __tablename__ = "settings"
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.String(255), nullable=False)

    DEFAULTS = {
        "overhead_per_kg": "900",
        "vat_rate": "0.18",
        "wholesale_margin": "0.47",
        "rrp_margin": "0.15",
        "default_packaging": "1405.69",
        "margin_threshold_low": "25",
        "margin_threshold_high": "40",
        "currency": "UGX",
    }

    @staticmethod
    def get(key, default=None):
        row = db.session.get(CostSetting, key)
        if row is not None:
            return row.value
        return CostSetting.DEFAULTS.get(key, default)

    @staticmethod
    def get_float(key, default=0.0):
        try:
            return float(CostSetting.get(key, default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def set(key, value):
        row = db.session.get(CostSetting, key)
        if row is None:
            row = CostSetting(key=key, value=str(value))
            db.session.add(row)
        else:
            row.value = str(value)
        return row


# Money columns in the costing module (QA audit 5 Jul 2026 H2): declared
# NUMERIC(16,4) so the schema states the precision and a PostgreSQL move gets
# true fixed-point storage. asdecimal=False keeps the Python interface float
# for the costing engine's ratio arithmetic (yields, scaling, margins); money
# rounds at the defined boundaries — models.line_money per document line and
# services/ledger.to_minor at posting — both Decimal HALF_UP. Masses,
# percentages and weights stay Float: they are measures, not money.
_MONEY = db.Numeric(16, 4, asdecimal=False)


class CostIngredient(db.Model):
    __tablename__ = "ingredients"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    alias1 = db.Column(db.String(160))
    alias2 = db.Column(db.String(160))
    uom = db.Column(db.String(20), nullable=False, default="kg")
    base_cost = db.Column(_MONEY, nullable=False, default=0.0)
    tax_value = db.Column(_MONEY, nullable=False, default=0.0)
    clearance = db.Column(_MONEY, nullable=False, default=0.0)
    freight = db.Column(_MONEY, nullable=False, default=0.0)
    active = db.Column(db.Boolean, nullable=False, default=True)
    excel_row = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    history = db.relationship("CostPriceHistory", back_populates="ingredient",
                              cascade="all, delete-orphan",
                              order_by="CostPriceHistory.changed_at.desc()")

    @property
    def total_cost(self):
        """Total landed cost / kg = base + tax + clearance + freight."""
        return (self.base_cost or 0) + (self.tax_value or 0) + \
            (self.clearance or 0) + (self.freight or 0)


class CostPriceHistory(db.Model):
    __tablename__ = "price_history"
    id = db.Column(db.Integer, primary_key=True)
    ingredient_id = db.Column(db.Integer, db.ForeignKey("ingredients.id"), nullable=False)
    old_cost = db.Column(_MONEY)
    new_cost = db.Column(_MONEY)
    old_total = db.Column(_MONEY)
    new_total = db.Column(_MONEY)
    changed_by = db.Column(db.String(80))
    changed_at = db.Column(db.DateTime, default=utcnow)
    ingredient = db.relationship("CostIngredient", back_populates="history")


class CostSpiceMix(db.Model):
    __tablename__ = "spice_mixes"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), unique=True, nullable=False)
    note = db.Column(db.Text)
    excel_sheet = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=utcnow)
    lines = db.relationship("CostSpiceMixLine", back_populates="spice_mix",
                            cascade="all, delete-orphan",
                            order_by="CostSpiceMixLine.position")


class CostSpiceMixLine(db.Model):
    __tablename__ = "spice_mix_lines"
    id = db.Column(db.Integer, primary_key=True)
    spice_mix_id = db.Column(db.Integer, db.ForeignKey("spice_mixes.id"), nullable=False)
    position = db.Column(db.Integer, default=0)
    ingredient_id = db.Column(db.Integer, db.ForeignKey("ingredients.id"))
    display_name = db.Column(db.String(160), nullable=False)
    mass_kg = db.Column(db.Float, nullable=False, default=0.0)
    cost_override = db.Column(_MONEY)
    spice_mix = db.relationship("CostSpiceMix", back_populates="lines")
    ingredient = db.relationship("CostIngredient")


class CostRecipe(db.Model):
    __tablename__ = "recipes"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    status = db.Column(db.String(12), nullable=False, default="active")
    batch_label = db.Column(db.String(120))
    casing_type = db.Column(db.String(120))
    casing_cpk = db.Column(_MONEY, nullable=False, default=0.0)
    casing_pct = db.Column(db.Float, nullable=False, default=0.0)
    overhead_override = db.Column(_MONEY)
    packaging_cpk = db.Column(_MONEY, nullable=False, default=0.0)
    note = db.Column(db.Text)
    deactivate_reason = db.Column(db.Text)
    deactivated_at = db.Column(db.DateTime)
    last_cost_per_kg = db.Column(_MONEY)
    excel_sheet = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    category = db.relationship("CostCategory", back_populates="recipes")
    lines = db.relationship("CostRecipeLine", back_populates="recipe",
                            cascade="all, delete-orphan",
                            order_by="CostRecipeLine.position")
    extras = db.relationship("CostRecipeExtra", back_populates="recipe",
                             cascade="all, delete-orphan")
    pack_sizes = db.relationship("CostPackSize", back_populates="recipe",
                                 cascade="all, delete-orphan")


class CostRecipeLine(db.Model):
    __tablename__ = "recipe_lines"
    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey("recipes.id"), nullable=False)
    position = db.Column(db.Integer, default=0)
    ingredient_id = db.Column(db.Integer, db.ForeignKey("ingredients.id"))
    spice_mix_id = db.Column(db.Integer, db.ForeignKey("spice_mixes.id"))
    display_name = db.Column(db.String(200), nullable=False)
    mass_kg = db.Column(db.Float, nullable=False, default=0.0)
    cost_override = db.Column(_MONEY)

    recipe = db.relationship("CostRecipe", back_populates="lines")
    ingredient = db.relationship("CostIngredient")
    spice_mix = db.relationship("CostSpiceMix")


class CostRecipeExtra(db.Model):
    __tablename__ = "recipe_extras"
    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey("recipes.id"), nullable=False)
    name = db.Column(db.String(160), nullable=False)
    value_per_kg = db.Column(_MONEY, nullable=False, default=0.0)
    recipe = db.relationship("CostRecipe", back_populates="extras")


class CostPackSize(db.Model):
    __tablename__ = "pack_sizes"
    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey("recipes.id"), nullable=False)
    label = db.Column(db.String(80), nullable=False)
    pack_weight_kg = db.Column(db.Float, nullable=False, default=1.0)
    pieces = db.Column(db.Integer)
    packing_cost = db.Column(_MONEY, nullable=False, default=0.0)
    recipe = db.relationship("CostRecipe", back_populates="pack_sizes")


class CostPackagingConfig(db.Model):
    __tablename__ = "packaging_configs"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    note = db.Column(db.String(255))
    items = db.relationship("CostPackagingItem", back_populates="config",
                            cascade="all, delete-orphan")

    @property
    def total_per_kg(self):
        return sum((it.unit_price or 0) for it in self.items)


class CostPackagingItem(db.Model):
    __tablename__ = "packaging_items"
    id = db.Column(db.Integer, primary_key=True)
    config_id = db.Column(db.Integer, db.ForeignKey("packaging_configs.id"), nullable=False)
    material = db.Column(db.String(120), nullable=False)
    unit_price = db.Column(_MONEY, nullable=False, default=0.0)
    note = db.Column(db.String(120))
    config = db.relationship("CostPackagingConfig", back_populates="items")


class Carcass(db.Model):
    """A carcass breakdown / yield test — the cost source for fresh cuts."""
    __tablename__ = "carcasses"
    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(160), nullable=False)
    species = db.Column(db.String(20), nullable=False, default="Beef")
    carcass_weight_kg = db.Column(db.Float, nullable=False, default=0.0)
    purchase_cost = db.Column(_MONEY, nullable=False, default=0.0)
    processing_fee_per_kg = db.Column(_MONEY, nullable=False, default=0.0)
    injection_pct = db.Column(db.Float, nullable=False, default=0.0)
    allocation_method = db.Column(db.String(10), nullable=False, default="value")
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    costs = db.relationship("CarcassCost", back_populates="carcass",
                            cascade="all, delete-orphan")
    cuts = db.relationship("Cut", back_populates="carcass",
                           cascade="all, delete-orphan", order_by="Cut.position")


class CarcassCost(db.Model):
    __tablename__ = "carcass_costs"
    id = db.Column(db.Integer, primary_key=True)
    carcass_id = db.Column(db.Integer, db.ForeignKey("carcasses.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    amount = db.Column(_MONEY, nullable=False, default=0.0)
    carcass = db.relationship("Carcass", back_populates="costs")


class Cut(db.Model):
    __tablename__ = "cuts"
    id = db.Column(db.Integer, primary_key=True)
    carcass_id = db.Column(db.Integer, db.ForeignKey("carcasses.id"), nullable=False)
    position = db.Column(db.Integer, default=0)
    name = db.Column(db.String(160), nullable=False)
    weight_kg = db.Column(db.Float, nullable=False, default=0.0)
    selling_price = db.Column(_MONEY, nullable=False, default=0.0)
    export_price = db.Column(_MONEY, nullable=False, default=0.0)
    injectable = db.Column(db.Boolean, nullable=False, default=False)
    ingredient_id = db.Column(db.Integer, db.ForeignKey("ingredients.id"))
    carcass = db.relationship("Carcass", back_populates="cuts")
    ingredient = db.relationship("CostIngredient")


# ---------------------------------------------------------------------------
# Phase 2 (integration) — NATIVE recipe tables in pricing.db. The recipe data is
# imported from the costing app into these tables so the pricing app stands
# alone. The Cost* models above stay only as the read-only source for the
# import/re-sync. Ids are preserved from costing so cross-references and the
# product-to-recipe map stay stable across re-syncs.
# ---------------------------------------------------------------------------
class ProdRecipe(db.Model):
    __tablename__ = "prod_recipe"
    id = db.Column(db.Integer, primary_key=True)   # = costing.recipes.id
    name = db.Column(db.String(255), index=True)
    status = db.Column(db.String(16))
    batch_label = db.Column(db.String(255))
    casing_type = db.Column(db.String(64))
    casing_cpk = db.Column(_MONEY)
    casing_pct = db.Column(db.Float)
    packaging_cpk = db.Column(_MONEY)
    last_cost_per_kg = db.Column(_MONEY)
    synced_at = db.Column(db.DateTime)

    lines = db.relationship("ProdRecipeLine", backref="recipe",
                            cascade="all, delete-orphan")
    pack_sizes = db.relationship("ProdPackSize", backref="recipe",
                                 cascade="all, delete-orphan")


class ProdIngredient(db.Model):
    __tablename__ = "prod_ingredient"
    id = db.Column(db.Integer, primary_key=True)   # = costing.ingredients.id
    name = db.Column(db.String(255), index=True)
    alias1 = db.Column(db.String(255))
    alias2 = db.Column(db.String(255))
    uom = db.Column(db.String(24))
    base_cost = db.Column(_MONEY)


class ProdSpiceMix(db.Model):
    __tablename__ = "prod_spice_mix"
    id = db.Column(db.Integer, primary_key=True)   # = costing.spice_mixes.id
    name = db.Column(db.String(255))


class ProdSpiceMixLine(db.Model):
    __tablename__ = "prod_spice_mix_line"
    id = db.Column(db.Integer, primary_key=True)
    spice_mix_id = db.Column(db.Integer, db.ForeignKey("prod_spice_mix.id", ondelete="CASCADE"), index=True)
    position = db.Column(db.Integer)
    ingredient_id = db.Column(db.Integer)
    display_name = db.Column(db.String(255))
    mass_kg = db.Column(db.Float)


class ProdRecipeLine(db.Model):
    __tablename__ = "prod_recipe_line"
    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey("prod_recipe.id", ondelete="CASCADE"), index=True)
    position = db.Column(db.Integer)
    ingredient_id = db.Column(db.Integer)
    spice_mix_id = db.Column(db.Integer)
    display_name = db.Column(db.String(255))
    mass_kg = db.Column(db.Float)


class ProdPackSize(db.Model):
    __tablename__ = "prod_pack_size"
    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey("prod_recipe.id", ondelete="CASCADE"), index=True)
    label = db.Column(db.String(255))
    pack_weight_kg = db.Column(db.Float)
    pieces = db.Column(db.Integer)
    packing_cost = db.Column(_MONEY)


class ProdRecipeExtra(db.Model):
    __tablename__ = "prod_recipe_extra"
    id = db.Column(db.Integer, primary_key=True)
    recipe_id = db.Column(db.Integer, db.ForeignKey("prod_recipe.id", ondelete="CASCADE"), index=True)
    name = db.Column(db.String(255))
    value_per_kg = db.Column(_MONEY)


# ---------------------------------------------------------------------------
# ACCOUNTING MODULE (Phase 1) — double-entry ledger. All tables prefixed acc_.
#
# Money rule: every ledger amount is an INTEGER in minor units — UGX in whole
# shillings, USD in cents. Never float. The conversion from the app's Numeric/
# float world happens once, in services/ledger.py, at posting time.
#
# Append-only rule: posted entries and their lines are immutable. Corrections
# post reversing entries. SQLite triggers (migrations/acc_001_triggers.sql)
# enforce this physically; the application checks are the friendly layer.
#
# Two-phase posting: an entry is created with posted=0 (draft, invisible to all
# reports), lines are inserted, then posted flips to 1. The flip is guarded by
# a trigger which aborts unless debits equal credits, at least two lines exist,
# and every line is single-sided. SQLite has no deferred constraints, so this
# post-flip is where balance is enforced at the database level.
# ---------------------------------------------------------------------------
class AccAccount(db.Model):
    """One account in the chart of accounts.

    ``system_key`` marks control accounts the posting rules must find without
    hard-coding ids (ar_control, vat_output, inv_finished, ...). ``is_postable``
    False marks header/grouping accounts that never carry lines."""
    __tablename__ = "acc_account"
    TYPES = ["asset", "liability", "equity", "income", "cogs", "expense"]
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(8), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    type = db.Column(db.String(12), nullable=False, index=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("acc_account.id"))
    is_postable = db.Column(db.Boolean, nullable=False, default=True)
    system_key = db.Column(db.String(24), unique=True)   # nullable; control accounts only
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    parent = db.relationship("AccAccount", remote_side=[id], backref="children")

    # Debit-normal account types; the rest are credit-normal. Used by screens
    # to show balances the way an accountant expects (no negative clutter).
    DEBIT_NORMAL = ("asset", "cogs", "expense")

    @property
    def is_debit_normal(self):
        return self.type in self.DEBIT_NORMAL


class AccJournalEntry(db.Model):
    """A balanced double-entry posting. Header only; amounts live on lines.

    ``source_type``/``source_id`` tie automatic postings back to their business
    document (order, invoice, payment, production run...). Manual journals use
    source_type='manual'. ``reversal_of_id`` links a correcting entry to the
    entry it reverses; the reversed entry stays untouched (append-only)."""
    __tablename__ = "acc_journal_entry"
    SOURCES = ["manual", "opening", "order", "invoice", "credit_note",
               "payment", "purchase", "production", "adjustment"]
    id = db.Column(db.Integer, primary_key=True)
    entry_no = db.Column(db.String(32), unique=True, index=True)   # JE-2026-00001
    entry_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    memo = db.Column(db.String(255))
    source_type = db.Column(db.String(16), nullable=False, default="manual", index=True)
    source_id = db.Column(db.Integer)          # id in the source table (nullable)
    channel = db.Column(db.String(24))         # optional reporting dimension
    posted = db.Column(db.Boolean, nullable=False, default=False, index=True)
    posted_at = db.Column(db.DateTime)
    reversal_of_id = db.Column(db.Integer, db.ForeignKey("acc_journal_entry.id"))
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    reversal_of = db.relationship("AccJournalEntry", remote_side=[id],
                                  backref="reversals")
    created_by = db.relationship("User")
    lines = db.relationship("AccJournalLine", back_populates="entry",
                            order_by="AccJournalLine.id",
                            cascade="all, delete-orphan")

    @property
    def total_debit(self):
        return sum(l.debit or 0 for l in self.lines)

    @property
    def total_credit(self):
        return sum(l.credit or 0 for l in self.lines)

    @property
    def is_balanced(self):
        return self.total_debit == self.total_credit and len(self.lines) >= 2


class AccJournalLine(db.Model):
    """One debit or credit on an entry. Amounts are INTEGER UGX shillings.

    Exactly one of ``debit``/``credit`` is non-zero, both are non-negative
    (trigger-enforced at post). A line born from a foreign-currency document
    also records the original currency, its integer minor amount (USD cents)
    and the UGX-per-unit rate stamped on the document, so the USD story is
    reconstructable while the ledger itself balances in UGX."""
    __tablename__ = "acc_journal_line"
    id = db.Column(db.Integer, primary_key=True)
    entry_id = db.Column(db.Integer,
                         db.ForeignKey("acc_journal_entry.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("acc_account.id"),
                           nullable=False, index=True)
    debit = db.Column(db.Integer, nullable=False, default=0)    # UGX shillings
    credit = db.Column(db.Integer, nullable=False, default=0)   # UGX shillings
    orig_currency = db.Column(db.String(8))                     # e.g. "USD" (nullable)
    orig_amount_minor = db.Column(db.Integer)                   # e.g. USD cents (nullable)
    fx_rate = db.Column(db.Numeric(16, 6))                      # UGX per 1 orig unit
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"))  # AR dimension
    item_id = db.Column(db.Integer)            # future acc_item dimension (Phase 2)
    line_memo = db.Column(db.String(255))

    entry = db.relationship("AccJournalEntry", back_populates="lines")
    account = db.relationship("AccAccount")
    customer = db.relationship("Customer")

    @property
    def signed_amount(self):
        """Debit positive, credit negative — for running balances."""
        return (self.debit or 0) - (self.credit or 0)


# ---------------------------------------------------------------------------
# ACCOUNTING MODULE (Phase 2) — valued inventory. Weighted average cost.
#
# The valuation rule: an item never stores a rounded unit cost. It tracks
# qty_on_hand and value_on_hand (INTEGER UGX). The average is the ratio.
# An issue takes round(qty x value/qty_on_hand) shillings out, so the item
# values always sum exactly to the inventory control accounts in the GL —
# no float drift, ever.
#
# The operational stock world (product.stock_on_hand, stock_movement) keeps
# running untouched; this layer values events in parallel. Units are defined
# HERE (stock_unit + pack_weight_kg), which is the accounting answer to the
# kg-vs-pack ambiguity flagged in the audit (M16): an item whose unit story
# is unresolved is blocked from valuation instead of being costed wrongly.
# ---------------------------------------------------------------------------
class AccItem(db.Model):
    """One valued inventory item.

    stage: raw (livestock/carcass/ingredients, GL 1200) | finished (catalogue
    cuts and processed products, GL 1210) | packaging (GL 1220).
    Finished items link to a catalogue Product; raw/packaging items link to a
    StoreItem. cost basis:
      * recipe  — cost per kg from the linked recipe (processed products);
      * manual  — manual_cost_minor per stock_unit, entered on the worklist;
      * none    — no cost yet; excluded from valuation, shown on the worklist.
    """
    __tablename__ = "acc_item"
    # QA audit 5 Jul 2026 M2: an item must have EXACTLY ONE source — either a
    # catalogue product or a store item. The CHECK applies on fresh installs
    # (SQLite cannot retrofit a table constraint); services/inventory_costing
    # enforces the same rule at creation for every database.
    __table_args__ = (
        db.CheckConstraint(
            "(product_id IS NULL) != (store_item_id IS NULL)",
            name="ck_acc_item_exactly_one_source"),
    )
    STAGES = {"raw": "Raw materials", "finished": "Finished goods",
              "packaging": "Packaging & consumables"}
    STAGE_ACCOUNTS = {"raw": "inv_raw", "finished": "inv_finished",
                      "packaging": "inv_packaging"}
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), unique=True)
    store_item_id = db.Column(db.Integer, db.ForeignKey("store_item.id"), unique=True)
    name = db.Column(db.String(200), nullable=False)
    stage = db.Column(db.String(12), nullable=False, default="finished", index=True)
    stock_unit = db.Column(db.String(12))            # kg | pack | pc (item's qty unit)
    pack_weight_kg = db.Column(db.Float)             # needed when unit != kg and cost is per kg
    qty_on_hand = db.Column(db.Float, nullable=False, default=0)
    value_on_hand = db.Column(db.Integer, nullable=False, default=0)   # UGX shillings
    manual_cost_minor = db.Column(db.Integer)        # UGX per stock_unit (worklist entry)
    cost_source = db.Column(db.String(12), nullable=False, default="none")  # recipe|manual|none
    efris_goods_code = db.Column(db.String(40))      # Phase 3
    efris_commodity_code = db.Column(db.String(40))  # Phase 3
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    product = db.relationship("Product")
    store_item = db.relationship("StoreItem")
    movements = db.relationship("AccInvMovement", back_populates="item",
                                order_by="AccInvMovement.id")

    @property
    def avg_cost(self):
        """UGX per stock_unit, derived — never stored, never rounded away."""
        if not self.qty_on_hand:
            return None
        return self.value_on_hand / self.qty_on_hand

    @property
    def stage_label(self):
        return self.STAGES.get(self.stage, self.stage)


# QA audit 5 Jul 2026 M2: enforce the exactly-one-source rule on EVERY write,
# on every backend — existing SQLite tables cannot gain the CHECK constraint.
from sqlalchemy import event as _sa_event  # noqa: E402


@_sa_event.listens_for(AccItem, "before_insert")
@_sa_event.listens_for(AccItem, "before_update")
def _acc_item_exactly_one_source(mapper, connection, target):
    if (target.product_id is None) == (target.store_item_id is None):
        raise ValueError(
            "acc_item must reference exactly one of product_id or "
            "store_item_id — a sourceless item cannot be costed.")


class AccInvMovement(db.Model):
    """One valued stock movement. Append-only (triggers in acc_002).

    qty and value_ugx are SIGNED: receipts positive, issues negative.
    qty_after/value_after snapshot the item balance after this movement, so
    the history is auditable without replaying. journal_entry_id ties the
    movement to the ledger posting that carries its value."""
    __tablename__ = "acc_inv_movement"
    KINDS = ["opening", "purchase", "production_in", "production_out",
             "sale", "sale_return", "adjustment", "wastage"]
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("acc_item.id"),
                        nullable=False, index=True)
    kind = db.Column(db.String(16), nullable=False, index=True)
    qty = db.Column(db.Float, nullable=False)              # signed, stock_unit
    value_ugx = db.Column(db.Integer, nullable=False)      # signed, UGX shillings
    qty_after = db.Column(db.Float, nullable=False)
    value_after = db.Column(db.Integer, nullable=False)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("acc_journal_entry.id"),
                                 index=True)
    # QA audit 5 Jul 2026 M1: RESTRICT declared on purpose — a valued movement
    # is part of the financial record, so its source documents must never be
    # hard-deleted from under it. Blocking the delete IS the intended outcome.
    order_id = db.Column(db.Integer, db.ForeignKey("sales_order.id", ondelete="RESTRICT"))
    order_line_id = db.Column(db.Integer, db.ForeignKey("sales_order_line.id", ondelete="RESTRICT"))
    production_id = db.Column(db.Integer, db.ForeignKey("prod_production.id", ondelete="RESTRICT"))
    note = db.Column(db.String(255))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    item = db.relationship("AccItem", back_populates="movements")
    journal_entry = db.relationship("AccJournalEntry")
    user = db.relationship("User")


# ---------------------------------------------------------------------------
# ACCOUNTING MODULE (Phase 3) — fiscal invoices and EFRIS.
#
# The rules, physical and procedural:
#   * The ledger never waits for URA. The sale posts, the invoice exists, and
#     fiscalization runs after commit; a failure queues a retry.
#   * A fiscalized invoice is corrected ONLY by a fiscalized credit note whose
#     reversing journal posts in the same transaction. No silent reversals.
#   * Money columns freeze once posted (trigger acc_003); EFRIS result fields
#     stay writable because URA answers after the fact.
#   * The full EFRIS response is stored per invoice for URA audit.
# ---------------------------------------------------------------------------
class AccInvoice(db.Model):
    __tablename__ = "acc_invoice"
    KINDS = ["invoice", "credit_note"]
    EFRIS_STATUSES = ["pending", "fiscalized", "failed", "not_required"]
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(12), nullable=False, default="invoice", index=True)
    invoice_no = db.Column(db.String(32), unique=True, index=True)  # INV-/CN-2026-xxxxx
    order_id = db.Column(db.Integer, db.ForeignKey("sales_order.id"), index=True)
    reverses_invoice_id = db.Column(db.Integer, db.ForeignKey("acc_invoice.id"))
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"), index=True)
    buyer_name = db.Column(db.String(128))          # snapshots — the fiscal document
    buyer_tin = db.Column(db.String(40))            # must not drift with the CRM
    invoice_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    currency = db.Column(db.String(8), nullable=False, default="UGX")
    fx_rate = db.Column(db.Numeric(16, 6))          # UGX per 1 currency unit (USD docs)
    # Totals in the DOCUMENT currency, integer minor units (UGX sh / USD cents).
    net_minor = db.Column(db.Integer, nullable=False, default=0)
    vat_minor = db.Column(db.Integer, nullable=False, default=0)
    gross_minor = db.Column(db.Integer, nullable=False, default=0)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("acc_journal_entry.id"))
    status = db.Column(db.String(12), nullable=False, default="posted", index=True)  # posted | credited
    cogs_minor = db.Column(db.Integer, nullable=False, default=0)   # UGX, valued lines only
    cogs_skipped = db.Column(db.Text)   # names of lines sold without valuation (worklist signal)
    paid_minor = db.Column(db.Integer, nullable=False, default=0)  # Phase 5 receipts
    # EFRIS result
    efris_status = db.Column(db.String(14), nullable=False, default="pending", index=True)
    efris_fdn = db.Column(db.String(40))            # fiscal document number
    efris_verification_code = db.Column(db.String(40))
    efris_qr = db.Column(db.Text)
    efris_invoice_id = db.Column(db.String(40))     # URA internal id
    fiscalized_at = db.Column(db.DateTime)
    efris_response = db.Column(db.Text)             # full JSON, URA audit trail
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    order = db.relationship("SalesOrder")
    customer = db.relationship("Customer")
    journal_entry = db.relationship("AccJournalEntry")
    reverses = db.relationship("AccInvoice", remote_side=[id],
                               backref="credit_notes")
    lines = db.relationship("AccInvoiceLine", back_populates="invoice",
                            order_by="AccInvoiceLine.id",
                            cascade="all, delete-orphan")
    created_by = db.relationship("User")

    @property
    def is_credit_note(self):
        return self.kind == "credit_note"

    @property
    def has_credit_note(self):
        return any(cn.status != "void" for cn in self.credit_notes)

    @property
    def open_minor(self):
        """Unpaid balance in document minor units (credit notes excluded)."""
        if self.kind != "invoice" or self.status == "credited":
            return 0
        return max(self.gross_minor - (self.paid_minor or 0), 0)


class AccInvoiceLine(db.Model):
    __tablename__ = "acc_invoice_line"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer,
                           db.ForeignKey("acc_invoice.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    order_line_id = db.Column(db.Integer, db.ForeignKey("sales_order_line.id"))
    item_id = db.Column(db.Integer, db.ForeignKey("acc_item.id"))
    description = db.Column(db.String(255))
    qty = db.Column(db.Float, nullable=False, default=0)
    unit_price_minor = db.Column(db.Integer, nullable=False, default=0)  # doc ccy
    net_minor = db.Column(db.Integer, nullable=False, default=0)
    vat_rate = db.Column(db.Float, nullable=False, default=0)
    vat_minor = db.Column(db.Integer, nullable=False, default=0)
    efris_goods_code = db.Column(db.String(40))

    invoice = db.relationship("AccInvoice", back_populates="lines")
    item = db.relationship("AccItem")


# ---------------------------------------------------------------------------
# ACCOUNTING MODULE (Phase 4) — purchases, expenses, payables.
#
# The rule the brief made non-negotiable: livestock and carcass buys DEBIT
# INVENTORY, never expense. Only non-stock costs (fuel, repairs, airtime...)
# hit the P&L directly. Stock lines flow through the weighted-average
# receive() so every purchase moves the item's average cost.
# ---------------------------------------------------------------------------
class AccSupplier(db.Model):
    __tablename__ = "acc_supplier"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    tin = db.Column(db.String(40))
    vat_registered = db.Column(db.Boolean, nullable=False, default=False)
    phone = db.Column(db.String(64))
    email = db.Column(db.String(128))
    payment_terms = db.Column(db.String(64))     # e.g. "cash", "14 days"
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    purchases = db.relationship("AccPurchase", back_populates="supplier")

    @property
    def balance_minor(self):
        """Outstanding payables to this supplier (posted, on account, unpaid).
        Payments reduce this from Phase 5 via paid_minor."""
        return sum((p.gross_ugx_minor - (p.paid_minor or 0))
                   for p in self.purchases
                   if p.status == "posted" and p.on_account)


class AccPurchase(db.Model):
    """One supplier bill / purchase. Totals in document currency minor units;
    the ledger books UGX at the stamped rate (same convention as invoices)."""
    __tablename__ = "acc_purchase"
    PAY_FROM = {"account": "On account (payable)", "cash": "Cash",
                "bank_ugx": "Bank — UGX", "momo": "Mobile Money"}
    id = db.Column(db.Integer, primary_key=True)
    purchase_no = db.Column(db.String(32), unique=True, index=True)  # PUR-2026-xxxxx
    supplier_id = db.Column(db.Integer, db.ForeignKey("acc_supplier.id"),
                            nullable=False, index=True)
    bill_ref = db.Column(db.String(64))          # supplier's own invoice number
    purchase_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    due_date = db.Column(db.Date)
    currency = db.Column(db.String(8), nullable=False, default="UGX")
    fx_rate = db.Column(db.Numeric(16, 6))
    pay_from = db.Column(db.String(12), nullable=False, default="account")
    net_minor = db.Column(db.Integer, nullable=False, default=0)
    vat_minor = db.Column(db.Integer, nullable=False, default=0)   # input VAT
    gross_minor = db.Column(db.Integer, nullable=False, default=0)
    paid_minor = db.Column(db.Integer, nullable=False, default=0)  # Phase 5 payments
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("acc_journal_entry.id"))
    status = db.Column(db.String(12), nullable=False, default="posted", index=True)
    # posted | reversed
    reversal_entry_id = db.Column(db.Integer, db.ForeignKey("acc_journal_entry.id"))
    notes = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    supplier = db.relationship("AccSupplier", back_populates="purchases")
    journal_entry = db.relationship("AccJournalEntry",
                                    foreign_keys=[journal_entry_id])
    reversal_entry = db.relationship("AccJournalEntry",
                                     foreign_keys=[reversal_entry_id])
    lines = db.relationship("AccPurchaseLine", back_populates="purchase",
                            order_by="AccPurchaseLine.id",
                            cascade="all, delete-orphan")
    created_by = db.relationship("User")

    @property
    def on_account(self):
        return self.pay_from == "account"

    @property
    def gross_ugx_minor(self):
        if self.currency == "UGX" or not self.fx_rate:
            return self.gross_minor
        return int(round(self.gross_minor / 100 * float(self.fx_rate)))


class AccPurchaseLine(db.Model):
    __tablename__ = "acc_purchase_line"
    id = db.Column(db.Integer, primary_key=True)
    purchase_id = db.Column(db.Integer,
                            db.ForeignKey("acc_purchase.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    line_type = db.Column(db.String(8), nullable=False)   # stock | expense
    item_id = db.Column(db.Integer, db.ForeignKey("acc_item.id"))          # stock lines
    expense_account_id = db.Column(db.Integer, db.ForeignKey("acc_account.id"))  # expense lines
    description = db.Column(db.String(255))
    qty = db.Column(db.Float)                    # stock lines, in item's stock_unit
    unit_cost_minor = db.Column(db.Integer)      # doc ccy per unit
    net_minor = db.Column(db.Integer, nullable=False, default=0)
    vat_minor = db.Column(db.Integer, nullable=False, default=0)

    purchase = db.relationship("AccPurchase", back_populates="lines")
    item = db.relationship("AccItem")
    expense_account = db.relationship("AccAccount")


# ---------------------------------------------------------------------------
# ACCOUNTING MODULE (Phase 5) — cash & bank.
#
# Money accounts: 1000 Cash, 1010 Bank UGX, 1020 Bank USD, 1050 Mobile Money.
# Receipts settle customer invoices (WHT 6% honoured: DR 1320 for the slice a
# designated agent withholds). Supplier payments clear the Phase 4 payables.
# Transfers are plain journals (source_type 'transfer'). Reconciliation ticks
# journal lines against a statement without ever mutating them.
# ---------------------------------------------------------------------------
class AccReceipt(db.Model):
    __tablename__ = "acc_receipt"
    METHODS = {"cash": "Cash", "bank_ugx": "Bank — UGX",
               "bank_usd": "Bank — USD", "momo": "Mobile Money"}
    METHOD_ACCOUNTS = {"cash": "cash", "bank_ugx": "bank_ugx",
                       "bank_usd": "bank_usd", "momo": "momo"}
    id = db.Column(db.Integer, primary_key=True)
    receipt_no = db.Column(db.String(32), unique=True, index=True)   # RCT-2026-xxxxx
    customer_id = db.Column(db.Integer, db.ForeignKey("customer.id"),
                            nullable=False, index=True)
    receipt_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    method = db.Column(db.String(12), nullable=False, default="cash")
    currency = db.Column(db.String(8), nullable=False, default="UGX")
    fx_rate = db.Column(db.Numeric(16, 6))       # UGX per unit at RECEIPT date
    amount_minor = db.Column(db.Integer, nullable=False, default=0)  # money received
    wht_minor = db.Column(db.Integer, nullable=False, default=0)     # 6% withheld slice
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("acc_journal_entry.id"))
    status = db.Column(db.String(12), nullable=False, default="posted", index=True)
    reversal_entry_id = db.Column(db.Integer, db.ForeignKey("acc_journal_entry.id"))
    notes = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    customer = db.relationship("Customer")
    journal_entry = db.relationship("AccJournalEntry", foreign_keys=[journal_entry_id])
    allocations = db.relationship("AccReceiptAllocation", back_populates="receipt",
                                  cascade="all, delete-orphan")
    created_by = db.relationship("User")

    @property
    def settled_minor(self):
        """Total AR settled = money received + tax the customer withheld."""
        return (self.amount_minor or 0) + (self.wht_minor or 0)


class AccReceiptAllocation(db.Model):
    __tablename__ = "acc_receipt_allocation"
    id = db.Column(db.Integer, primary_key=True)
    receipt_id = db.Column(db.Integer,
                           db.ForeignKey("acc_receipt.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("acc_invoice.id"),
                           nullable=False, index=True)
    amount_minor = db.Column(db.Integer, nullable=False)   # doc ccy, settles this much

    receipt = db.relationship("AccReceipt", back_populates="allocations")
    invoice = db.relationship("AccInvoice")


class AccSupplierPayment(db.Model):
    __tablename__ = "acc_supplier_payment"
    id = db.Column(db.Integer, primary_key=True)
    payment_no = db.Column(db.String(32), unique=True, index=True)   # PAY-2026-xxxxx
    supplier_id = db.Column(db.Integer, db.ForeignKey("acc_supplier.id"),
                            nullable=False, index=True)
    payment_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    method = db.Column(db.String(12), nullable=False, default="bank_ugx")
    amount_minor = db.Column(db.Integer, nullable=False, default=0)   # UGX
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("acc_journal_entry.id"))
    status = db.Column(db.String(12), nullable=False, default="posted", index=True)
    reversal_entry_id = db.Column(db.Integer, db.ForeignKey("acc_journal_entry.id"))
    notes = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    supplier = db.relationship("AccSupplier")
    journal_entry = db.relationship("AccJournalEntry", foreign_keys=[journal_entry_id])
    allocations = db.relationship("AccPaymentAllocation", back_populates="payment",
                                  cascade="all, delete-orphan")
    created_by = db.relationship("User")


class AccPaymentAllocation(db.Model):
    __tablename__ = "acc_payment_allocation"
    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.Integer,
                           db.ForeignKey("acc_supplier_payment.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    purchase_id = db.Column(db.Integer, db.ForeignKey("acc_purchase.id"),
                            nullable=False, index=True)
    amount_minor = db.Column(db.Integer, nullable=False)   # UGX

    payment = db.relationship("AccSupplierPayment", back_populates="allocations")
    purchase = db.relationship("AccPurchase")


class AccReconciliation(db.Model):
    """One bank/cash reconciliation: statement balance vs cleared GL lines.

    Lines are never mutated (append-only triggers); clearing is recorded in
    acc_recon_line rows pointing at journal lines."""
    __tablename__ = "acc_reconciliation"
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("acc_account.id"),
                           nullable=False, index=True)
    statement_date = db.Column(db.Date, nullable=False, default=date.today)
    statement_balance_minor = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(10), nullable=False, default="open")  # open | closed
    closed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    account = db.relationship("AccAccount")
    lines = db.relationship("AccReconLine", back_populates="recon",
                            cascade="all, delete-orphan")
    created_by = db.relationship("User")


class AccReconLine(db.Model):
    __tablename__ = "acc_recon_line"
    id = db.Column(db.Integer, primary_key=True)
    recon_id = db.Column(db.Integer,
                         db.ForeignKey("acc_reconciliation.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    journal_line_id = db.Column(db.Integer, db.ForeignKey("acc_journal_line.id"),
                                nullable=False, index=True)

    recon = db.relationship("AccReconciliation", back_populates="lines")
    journal_line = db.relationship("AccJournalLine")


# ---------------------------------------------------------------------------
# ACCOUNTING MODULE (Phase 7) — own shops: locations, transfers, shop sales.
#
# Moving stock to an own shop is NEVER a sale: same legal entity, no revenue,
# no VAT, no journal — the goods stay in the same inventory account and only
# the LOCATION changes. The sale (revenue + VAT + COGS) happens at the shop
# till; the till's own EFRIS device fiscalizes, so the daily summary posted
# here carries efris_status 'not_required'.
# ---------------------------------------------------------------------------
class AccLocation(db.Model):
    __tablename__ = "acc_location"
    KINDS = {"plant": "Plant / main store", "shop": "Own shop"}
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    kind = db.Column(db.String(10), nullable=False, default="shop")
    is_main = db.Column(db.Boolean, nullable=False, default=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AccItemLocation(db.Model):
    """Quantity of one item sitting at one SHOP. Plant quantity is implicit:
    the valued item total (entity-wide) minus the shop quantities. Value is
    NOT split per location — one entity, one weighted average."""
    __tablename__ = "acc_item_location"
    __table_args__ = (UniqueConstraint("item_id", "location_id",
                                       name="uq_item_location"),)
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("acc_item.id"),
                        nullable=False, index=True)
    location_id = db.Column(db.Integer, db.ForeignKey("acc_location.id"),
                            nullable=False, index=True)
    qty = db.Column(db.Float, nullable=False, default=0)

    item = db.relationship("AccItem")
    location = db.relationship("AccLocation")


class AccTransfer(db.Model):
    """A stock movement between locations. NO journal entry — the balance
    sheet does not move when goods travel between own premises."""
    __tablename__ = "acc_transfer"
    id = db.Column(db.Integer, primary_key=True)
    transfer_no = db.Column(db.String(32), unique=True, index=True)  # TRF-2026-xxxxx
    from_location_id = db.Column(db.Integer, db.ForeignKey("acc_location.id"),
                                 nullable=False)
    to_location_id = db.Column(db.Integer, db.ForeignKey("acc_location.id"),
                               nullable=False)
    transfer_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    order_id = db.Column(db.Integer, db.ForeignKey("sales_order.id"))  # when born from an order
    notes = db.Column(db.String(255))
    status = db.Column(db.String(12), nullable=False, default="posted")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    from_location = db.relationship("AccLocation", foreign_keys=[from_location_id])
    to_location = db.relationship("AccLocation", foreign_keys=[to_location_id])
    order = db.relationship("SalesOrder")
    lines = db.relationship("AccTransferLine", back_populates="transfer",
                            cascade="all, delete-orphan")
    created_by = db.relationship("User")


class AccTransferLine(db.Model):
    __tablename__ = "acc_transfer_line"
    id = db.Column(db.Integer, primary_key=True)
    transfer_id = db.Column(db.Integer,
                            db.ForeignKey("acc_transfer.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("acc_item.id"), nullable=False)
    qty = db.Column(db.Float, nullable=False)

    transfer = db.relationship("AccTransfer", back_populates="lines")
    item = db.relationship("AccItem")


class AccShopSale(db.Model):
    """One shop's sales for one day, entered as a summary. Posts revenue net
    of VAT, VAT output, COGS at weighted average, and cash — and reduces the
    shop's location quantities. Fiscalization already happened at the till."""
    __tablename__ = "acc_shop_sale"
    id = db.Column(db.Integer, primary_key=True)
    sale_no = db.Column(db.String(32), unique=True, index=True)   # SHS-2026-xxxxx
    location_id = db.Column(db.Integer, db.ForeignKey("acc_location.id"),
                            nullable=False, index=True)
    sale_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    gross_minor = db.Column(db.Integer, nullable=False, default=0)   # UGX, incl VAT
    net_minor = db.Column(db.Integer, nullable=False, default=0)
    vat_minor = db.Column(db.Integer, nullable=False, default=0)
    cogs_minor = db.Column(db.Integer, nullable=False, default=0)
    journal_entry_id = db.Column(db.Integer, db.ForeignKey("acc_journal_entry.id"))
    status = db.Column(db.String(12), nullable=False, default="posted")
    reversal_entry_id = db.Column(db.Integer, db.ForeignKey("acc_journal_entry.id"))
    notes = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    location = db.relationship("AccLocation")
    journal_entry = db.relationship("AccJournalEntry", foreign_keys=[journal_entry_id])
    lines = db.relationship("AccShopSaleLine", back_populates="sale",
                            cascade="all, delete-orphan")
    created_by = db.relationship("User")


class AccShopSaleLine(db.Model):
    __tablename__ = "acc_shop_sale_line"
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer,
                        db.ForeignKey("acc_shop_sale.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    item_id = db.Column(db.Integer, db.ForeignKey("acc_item.id"), nullable=False)
    qty = db.Column(db.Float, nullable=False)
    gross_minor = db.Column(db.Integer, nullable=False)   # takings incl VAT
    net_minor = db.Column(db.Integer, nullable=False)
    vat_minor = db.Column(db.Integer, nullable=False)

    sale = db.relationship("AccShopSale", back_populates="lines")
    item = db.relationship("AccItem")


class AccEfrisQueue(db.Model):
    """Retry queue: one row per outstanding fiscalization or credit-note call.

    Never lose a sale because URA is down: the ledger and invoice commit first,
    the queue carries the URA conversation until a terminal state."""
    __tablename__ = "acc_efris_queue"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("acc_invoice.id"),
                           nullable=False, index=True)
    action = db.Column(db.String(16), nullable=False, default="fiscalize")  # fiscalize | credit_note
    status = db.Column(db.String(12), nullable=False, default="queued", index=True)
    # queued | in_flight | done | failed (failed = gave up; manual retry allowed)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    next_attempt_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    last_error = db.Column(db.String(512))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    done_at = db.Column(db.DateTime)

    invoice = db.relationship("AccInvoice")
