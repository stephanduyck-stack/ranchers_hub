"""Authentication: login, logout, password change, with lockout protection."""
from datetime import datetime, timedelta
from urllib.parse import urlparse

from flask import (Blueprint, render_template, redirect, url_for, request,
                   flash, current_app)
from flask_login import login_user, logout_user, login_required, current_user

from extensions import db
from models import User
from services.security import verify_password, hash_password
from services.audit import log

bp = Blueprint("auth", __name__)

# Endpoints reachable while a forced password change is pending.
_FORCE_PW_EXEMPT = {"auth.account", "auth.login", "auth.logout",
                    "auth.login_image", "auth.activate", "portal.account",
                    "static"}


@bp.before_app_request
def _force_password_change():
    """Accounts provisioned with a temporary password (new customer/distributor
    portal logins) go nowhere until they set their own password."""
    if not current_user.is_authenticated:
        return None
    if not getattr(current_user, "must_change_password", False):
        return None
    ep = request.endpoint or ""
    if ep in _FORCE_PW_EXEMPT or ep == "static" or ep.endswith(".static"):
        return None
    flash("Welcome. Set your own password to continue.", "warning")
    target = ("portal.account" if getattr(current_user, "is_customer_user", False)
              else "auth.account")
    return redirect(url_for(target))


def _safe_next(nxt):
    """Return nxt only if it is a relative, same-site path; else None.

    Blocks open-redirect phishing via ?next=https://evil.example.com. A valid
    target is a path beginning with a single "/" and carrying no scheme or
    network location (netloc). Protocol-relative "//host" URLs are rejected
    because their netloc is truthy.
    """
    if not nxt:
        return None
    parsed = urlparse(nxt)
    if parsed.scheme or parsed.netloc:
        return None
    if not nxt.startswith("/") or nxt.startswith("//"):
        return None
    return nxt


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.home"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        remember = bool(request.form.get("remember"))
        user = db.session.scalar(db.select(User).filter_by(username=username))

        now = datetime.utcnow()
        if user and user.locked_until and user.locked_until > now:
            mins = int((user.locked_until - now).total_seconds() // 60) + 1
            flash(f"Account locked. Try again in {mins} minute(s).", "danger")
            return render_template("login.html")

        if user and not user.is_active:
            flash("This account is disabled. Contact an administrator.", "danger")
            return render_template("login.html")

        if user and verify_password(password, user.password_hash):
            user.failed_attempts = 0
            user.locked_until = None
            user.last_login = now
            db.session.commit()
            login_user(user, remember=remember,
                       duration=current_app.config["REMEMBER_COOKIE_DURATION"] if remember else None)
            log("login", "user", user.id, detail="successful login", commit=True)
            nxt = _safe_next(request.args.get("next"))
            if nxt:
                return redirect(nxt)
            if user.is_customer_user:
                return redirect(url_for("portal.home"))
            return redirect(url_for("dashboard.home"))

        # Failed login. Keep the lockout counter running for real accounts, but
        # show one generic message for every wrong-credential case (unknown user,
        # wrong password, and lockout counting) so the message text cannot be
        # used to enumerate which usernames exist. The distinct locked/disabled
        # messages above are account states the user already knows about.
        if user:
            user.failed_attempts = (user.failed_attempts or 0) + 1
            if user.failed_attempts >= current_app.config["MAX_LOGIN_ATTEMPTS"]:
                user.locked_until = now + timedelta(minutes=current_app.config["LOCKOUT_MINUTES"])
                user.failed_attempts = 0
            db.session.commit()
        flash("Invalid username or password.", "danger")
        return render_template("login.html")

    return render_template("login.html")


@bp.route("/login-image")
def login_image():
    """Serve the admin-uploaded login image (public)."""
    import os
    from flask import current_app, send_file, abort
    from services import settings as settings_svc
    name = settings_svc.get("login_image", None)
    if not name:
        abort(404)
    path = os.path.join(current_app.config["UPLOAD_DIR"], "branding", name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path)


def _send_reset_email(u):
    """Best-effort reset email to the user's email (portal logins fall back
    to the linked customer's address). Returns (ok, reason)."""
    import os
    from flask import current_app
    from services import comms
    from services import settings as settings_svc
    from services.security import make_reset_token
    from services.email_templates import password_reset_html
    email = (u.email or "").strip()
    if not email and u.customer:
        email = (u.customer.email or "").strip()
    if not email:
        return (False, "no email address on file")
    company = settings_svc.get("company_name") or "Ranchers Finest"
    reset_url = (request.url_root.rstrip("/")
                 + url_for("auth.reset_password", token=make_reset_token(u)))
    body = (
        f"We received a request to reset the password for the account "
        f"{u.username} on the {company} Customer Portal.\n"
        f"\n"
        f"Choose a new password here (the link works for 2 hours, once):\n"
        f"{reset_url}\n"
        f"\n"
        f"Didn't ask for this? Ignore this email — your password stays as "
        f"it is.\n"
        f"\n"
        f"{company}\n"
    )
    html = password_reset_html(company, reset_url, u.username)
    logo = os.path.join(current_app.static_folder, "img", "ranchers-logo.png")
    ok, reason = comms.send_email(email, f"Reset your {company} portal password",
                                  body, html=html,
                                  inline_images={"rflogo": logo})
    log("pw_reset_email", "user", u.id,
        detail=f"reset email to {email}: {'sent' if ok else reason}",
        commit=True)
    return ok, reason


@bp.route("/forgot", methods=["GET", "POST"])
def forgot():
    """Forgot-password request. The response is identical whether the account
    exists or not, so the form cannot be used to enumerate usernames."""
    if request.method == "POST":
        needle = (request.form.get("username") or "").strip()
        if needle:
            u = db.session.scalar(db.select(User).filter(
                db.func.lower(User.username) == needle.lower()))
            if u is None and "@" in needle:
                u = db.session.scalar(db.select(User).filter(
                    db.func.lower(User.email) == needle.lower()))
            if u is not None and u.is_active:
                _send_reset_email(u)
        # Full confirmation page (not a toast): identical whether the account
        # exists or not, so the form stays enumeration-safe.
        return render_template("forgot_sent.html")
    return render_template("forgot.html")


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    """Set a new password from the emailed reset link (2 hours, single-use)."""
    from services.security import verify_reset_token
    u = verify_reset_token(token)
    if u is None:
        flash("This reset link is no longer valid. Request a new one below.",
              "warning")
        return redirect(url_for("auth.forgot"))
    if request.method == "POST":
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if len(new) < 8:
            flash("Password must be at least 8 characters.", "danger")
        elif new != confirm:
            flash("Passwords do not match.", "danger")
        else:
            u.password_hash = hash_password(new)
            u.must_change_password = False
            u.failed_attempts = 0
            u.locked_until = None
            db.session.commit()
            log("password_change", "user", u.id,
                detail=f"{u.username} reset via emailed link", commit=True)
            flash("Password set. Sign in with your new password.", "success")
            return redirect(url_for("auth.login"))
    return render_template("reset.html", user=u, token=token)


@bp.route("/activate/<token>", methods=["GET", "POST"])
def activate(token):
    """Set-your-password page reached from the welcome email's activation
    link. No credentials travel by mail: the signed token (72h, single-use —
    it binds to the current password hash) identifies the account."""
    from services.security import verify_activation_token
    u = verify_activation_token(token)
    if u is None:
        flash("This activation link is no longer valid. Ask us for a new "
              "welcome email, or sign in if you already set your password.",
              "warning")
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if len(new) < 8:
            flash("Password must be at least 8 characters.", "danger")
        elif new != confirm:
            flash("Passwords do not match.", "danger")
        else:
            u.password_hash = hash_password(new)
            u.must_change_password = False
            u.failed_attempts = 0
            u.locked_until = None
            u.last_login = datetime.utcnow()
            db.session.commit()
            log("portal_activate", "user", u.id,
                detail=f"{u.username} activated via welcome link", commit=True)
            login_user(u)
            flash("Welcome! Your password is set.", "success")
            return redirect(url_for("portal.home"))
    return render_template("activate.html", user=u, token=token)


@bp.route("/logout")
@login_required
def logout():
    log("logout", "user", current_user.id, commit=True)
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        current = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if not verify_password(current, current_user.password_hash):
            flash("Current password is incorrect.", "danger")
        elif len(new) < 8:
            flash("New password must be at least 8 characters.", "danger")
        elif new != confirm:
            flash("New passwords do not match.", "danger")
        else:
            was_forced = bool(getattr(current_user, "must_change_password", False))
            current_user.password_hash = hash_password(new)
            current_user.must_change_password = False
            db.session.commit()
            log("password_change", "user", current_user.id, commit=True)
            flash("Password updated.", "success")
            if was_forced:
                return redirect(url_for("portal.home")
                                if current_user.is_customer_user
                                else url_for("dashboard.home"))
        return redirect(url_for("auth.account"))
    return render_template("account.html")
