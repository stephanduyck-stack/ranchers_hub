"""Cron entry point for the message-notification sweep.

The sweep normally piggybacks on web traffic; this script covers quiet hours.
Suggested cron (cPanel), every 10 minutes:

    */10 * * * * cd /path/to/ranchers_pricing && \
        APP_BASE_URL=https://your-portal-domain SECRET_KEY=... \
        python3 notify_sweep.py

APP_BASE_URL (or the app_base_url setting) supplies the link base for the
email buttons, since no web request is around to provide it.
"""
import os

from app import create_app


def main():
    app = create_app()
    with app.app_context():
        from services import notify
        base = os.environ.get("APP_BASE_URL")
        sent = notify.sweep(base_url=base)
        print(f"notify sweep: {sent} email(s) sent")


if __name__ == "__main__":
    main()
