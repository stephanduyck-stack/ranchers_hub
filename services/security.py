"""Server-side access control: password hashing and route guards.

Authorization is enforced here on every protected route, not only hidden in the
UI. A read-only user is blocked from any write even via a direct request.
"""
from functools import wraps

import bcrypt
from flask import abort
from flask_login import current_user


# ---- passwords ----
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, AttributeError):
        return False


# ---- portal activation tokens ----
def _activation_serializer():
    from flask import current_app
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"],
                                  salt="portal-activate")


def make_activation_token(user):
    """Signed, expiring token for the welcome email's activation link. The
    token binds to a fragment of the current password hash, so it dies the
    moment the password changes (first activation, PDF reset, or manual)."""
    return _activation_serializer().dumps(
        {"uid": user.id, "h": (user.password_hash or "")[-12:]})


def verify_activation_token(token, max_age=72 * 3600):
    """Return the User for a valid, unexpired, unused token; else None."""
    from itsdangerous import BadSignature, SignatureExpired
    try:
        data = _activation_serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    from extensions import db
    from models import User
    u = db.session.get(User, data.get("uid"))
    if (u and u.is_active and u.role == "customer"
            and (u.password_hash or "")[-12:] == data.get("h")):
        return u
    return None


# ---- password reset tokens (any active user, shorter life) ----
def _reset_serializer():
    from flask import current_app
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"],
                                  salt="password-reset")


def make_reset_token(user):
    """Signed token for the forgot-password email. Bound to the current
    password hash, so setting a new password kills every earlier link."""
    return _reset_serializer().dumps(
        {"uid": user.id, "h": (user.password_hash or "")[-12:]})


def verify_reset_token(token, max_age=2 * 3600):
    """Return the User for a valid, unexpired, unused reset token; else None."""
    from itsdangerous import BadSignature, SignatureExpired
    try:
        data = _reset_serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    from extensions import db
    from models import User
    u = db.session.get(User, data.get("uid"))
    if (u and u.is_active
            and (u.password_hash or "")[-12:] == data.get("h")):
        return u
    return None


# ---- route guards ----
def roles_required(*roles):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if current_user.role not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def manager_required(fn):
    """Catalogue management — gated by the 'manage_catalogue' role permission."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        from services.permissions import has_perm
        if not current_user.is_authenticated or not has_perm(current_user, "manage_catalogue"):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def capability_required(cap):
    """Generic guard for a named role capability (see services.permissions)."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            from services.permissions import has_perm
            if not current_user.is_authenticated or not has_perm(current_user, cap):
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def edit_required(fn):
    """Block users without the independent edit/pricing right from any write."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.may_edit_prices:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


# ---- assignment-based visibility ----
def can_see_customer(user, customer):
    # Admins, managers, order managers and telesales see every customer; reps only assigned.
    if getattr(user, "sees_all_customers", False) or user.can_manage_all:
        return True
    # A sales manager sees the customers of the reps allocated to them.
    if getattr(user, "is_sales_manager", False):
        rep_ids = {r.id for r in getattr(user, "managed_reps", [])}
        if any(rp.id in rep_ids for rp in customer.reps):
            return True
    return any(c.id == customer.id for c in user.assigned_customers)


def can_see_customer_pricelist(user, pricelist):
    """Generic lists are visible to everyone. Tailor-made (customer) lists follow
    customer visibility: admins/managers, the pricing officer and other
    whole-base roles see all; reps see only their assigned customers' lists."""
    if not pricelist.is_customer:
        return True
    if user.can_manage_all or getattr(user, "sees_all_customers", False):
        return True
    if pricelist.customer is None:
        return False
    return can_see_customer(user, pricelist.customer)


def can_allocate_pricelists(user):
    """Only the pricing officer (and admin) may link pricelists to customers."""
    return bool(user and (user.is_admin or getattr(user, "is_pricing_officer", False)))


def assert_can_see_customer(user, customer):
    if not can_see_customer(user, customer):
        abort(403)


def assert_can_see_pricelist(user, pricelist):
    if not can_see_customer_pricelist(user, pricelist):
        abort(403)
