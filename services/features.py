"""Opt-in feature flags. Each module is off until an admin enables it on the
Admin > Features page. Flags are stored as Settings rows 'feature:<name>'."""
from extensions import db
from models import Setting

FEATURES = [
    ("pipeline", "Sales pipeline & deals",
     "Track deals, their value and stage (Lead → Won/Lost) per customer, with a pipeline board."),
    ("call_lists", "Scheduled call lists",
     "Build call lists for telesales shifts, assign them, and work through them with one-click logging."),
    ("reminders", "Automated reminders",
     "Surface follow-ups due and customers gone quiet on the dashboard and in the daily digest."),
    ("email", "Email from records",
     "Send and log emails to contacts. Needs an SMTP mailbox (set below)."),
    ("sms", "SMS from records",
     "Send and log SMS to contacts. Needs an SMS gateway (set below)."),
    ("telephony", "Click-to-dial & call recording",
     "Attach call recordings to logged calls. Click-to-dial via phone links already works."),
    ("stock_guard", "Stock guard at fulfilment",
     "Fulfilment cannot complete an order for more than the stock on hand; only the "
     "store manager adds stock. Untick only while loading opening balances."),
    ("require_lpo", "LPO required on every order",
     "An order cannot be placed by staff or submitted from the portal without "
     "the customer's LPO attached (photo or file). Untick to make the LPO "
     "optional again."),
    ("invoice_after_delivery", "Invoice after delivery (delivery-note flow)",
     "Fulfilment produces only a delivery note; the driver delivers, the customer "
     "confirms quantities and signs, the driver uploads the signed note, and the "
     "invoicing clerk bills the ACCEPTED quantities from the invoicing queue. "
     "Shortfalls carry a reason; returned goods go back to stock. Cuts credit "
     "notes to post-invoice disputes only."),
]

# Flags in this set are ON until an admin explicitly turns them off (controls,
# not opt-in modules). Everything else stays opt-in (off until enabled).
DEFAULT_ON = {"stock_guard", "require_lpo"}


def feature_on(name):
    row = db.session.get(Setting, f"feature:{name}")
    if row is None:
        return name in DEFAULT_ON
    return row.value == "1"


def all_features():
    return {k: feature_on(k) for k, _l, _d in FEATURES}


def set_feature(name, on):
    key = f"feature:{name}"
    row = db.session.get(Setting, key)
    if row is None:
        db.session.add(Setting(key=key, value="1" if on else "0"))
    else:
        row.value = "1" if on else "0"
