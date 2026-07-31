"""Branded HTML email templates. Email HTML is its own dialect: tables for
layout, inline styles only, no external CSS, no web fonts. Colours follow
brand.css (orange #F47A21, ink #0E0E0E, charcoal #2B2B2B, cream #FDF1E5)."""
from html import escape

ORANGE = "#F47A21"
AMBER = "#F9A03F"
INK = "#0E0E0E"
CHARCOAL = "#2B2B2B"
CREAM = "#FDF1E5"
PAPER = "#F5EFE6"
BORDER = "#EAD9C3"
MUTED = "#8A7E72"

# The six-step guide, shared with the plain-text mail and the welcome PDF.
GUIDE_STEPS = [
    ("Your pricelist",
     "My Pricelist shows your agreed prices — always current, updated the "
     "moment new lists take effect."),
    ("Placing an order",
     "Click New Order, pick products and quantities, submit. You receive an "
     "order number immediately and we confirm before fulfilment."),
    ("Tracking orders",
     "Follow every order from confirmation to delivery on your Home page, "
     "and download the order PDF."),
    ("Messages",
     "Questions or changes go through Messages. We reply in the portal."),
    ("Offers & promotions",
     "Offers we issue appear on your Home page — accept online and they "
     "convert into orders."),
    ("Your account",
     "Change your password any time under Account."),
]


def message_notification_html(company, from_name, body, cta_url,
                              cta_label="Open in your portal", logo_cid="rflogo"):
    """New-message notification: the message text in a panel plus one button
    into the portal. Replies happen in the portal, not by email."""
    company = escape(company or "Ranchers Finest")
    from_name = escape(from_name or company)
    cta_url = escape(cta_url or "")
    cta_label = escape(cta_label)
    body_html = escape(body or "").replace("\n", "<br>")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{company} — New message</title>
</head>
<body style="margin:0; padding:0; background:{PAPER};">
  <div style="display:none; max-height:0; overflow:hidden; mso-hide:all;">
    New message from {company} in your customer portal.
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER}; border-collapse:collapse;">
    <tr><td align="center" style="padding:28px 12px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="border-collapse:collapse; max-width:600px; width:100%;">
        <tr><td style="background:{INK}; border-radius:12px 12px 0 0; padding:26px 24px 20px 24px;" align="center">
          <img src="cid:{logo_cid}" alt="{company}" width="130" style="display:block; width:130px; max-width:130px; height:auto; margin:0 auto;">
          <div style="font-family:Arial,Helvetica,sans-serif; color:{AMBER}; font-size:12px; font-weight:bold; letter-spacing:4px; padding-top:14px;">NEW&nbsp;MESSAGE</div>
        </td></tr>
        <tr><td style="background:{ORANGE}; height:5px; font-size:0; line-height:0;">&nbsp;</td></tr>
        <tr><td style="background:#ffffff; padding:28px 36px 6px 36px; font-family:Arial,Helvetica,sans-serif;">
          <div style="font-size:14px; color:{CHARCOAL}; line-height:22px;">
            <b style="color:{INK};">{from_name}</b> sent you a message on the {company} Customer Portal:
          </div>
        </td></tr>
        <tr><td style="background:#ffffff; padding:16px 36px 6px 36px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:separate; background:{CREAM}; border:1px solid {BORDER}; border-left:4px solid {ORANGE}; border-radius:8px;">
            <tr><td style="padding:16px 20px; font-family:Arial,Helvetica,sans-serif; font-size:14px; color:{INK}; line-height:21px;">{body_html}</td></tr>
          </table>
        </td></tr>
        <tr><td style="background:#ffffff; padding:22px 36px 8px 36px;" align="center">
          <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:separate;">
            <tr><td style="background:{ORANGE}; border-radius:8px;" align="center">
              <a href="{cta_url}" style="display:inline-block; padding:13px 40px; font-family:Arial,Helvetica,sans-serif; font-size:15px; font-weight:bold; color:#ffffff; text-decoration:none;">{cta_label}</a>
            </td></tr>
          </table>
        </td></tr>
        <tr><td style="background:#ffffff; padding:14px 36px 24px 36px; font-family:Arial,Helvetica,sans-serif;" align="center">
          <div style="font-size:12px; color:{MUTED}; line-height:18px;">
            Reply in the portal so the whole conversation stays in one place.
          </div>
        </td></tr>
        <tr><td style="background:{INK}; border-radius:0 0 12px 12px; padding:18px 36px; font-family:Arial,Helvetica,sans-serif;" align="center">
          <div style="font-size:12px; font-weight:bold; color:#ffffff; letter-spacing:1px;">{company}</div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def password_reset_html(company, reset_url, username, logo_cid="rflogo"):
    """Password reset email: one button, two-hour link, ignore-if-not-you note."""
    company = escape(company or "Ranchers Finest")
    reset_url = escape(reset_url or "")
    username = escape(username or "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{company} — Password reset</title>
</head>
<body style="margin:0; padding:0; background:{PAPER};">
  <div style="display:none; max-height:0; overflow:hidden; mso-hide:all;">
    Reset your {company} portal password — the link works for 2 hours.
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER}; border-collapse:collapse;">
    <tr><td align="center" style="padding:28px 12px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="border-collapse:collapse; max-width:600px; width:100%;">
        <tr><td style="background:{INK}; border-radius:12px 12px 0 0; padding:26px 24px 20px 24px;" align="center">
          <img src="cid:{logo_cid}" alt="{company}" width="130" style="display:block; width:130px; max-width:130px; height:auto; margin:0 auto;">
          <div style="font-family:Arial,Helvetica,sans-serif; color:{AMBER}; font-size:12px; font-weight:bold; letter-spacing:4px; padding-top:14px;">PASSWORD&nbsp;RESET</div>
        </td></tr>
        <tr><td style="background:{ORANGE}; height:5px; font-size:0; line-height:0;">&nbsp;</td></tr>
        <tr><td style="background:#ffffff; padding:30px 36px 8px 36px; font-family:Arial,Helvetica,sans-serif;">
          <div style="font-size:14px; color:{CHARCOAL}; line-height:22px;">
            We received a request to reset the password for the account
            <span style="font-family:'Courier New',Courier,monospace; font-weight:bold; color:{INK};">{username}</span>
            on the {company} Customer Portal.
          </div>
        </td></tr>
        <tr><td style="background:#ffffff; padding:22px 36px 8px 36px;" align="center">
          <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:separate;">
            <tr><td style="background:{ORANGE}; border-radius:8px;" align="center">
              <a href="{reset_url}" style="display:inline-block; padding:14px 44px; font-family:Arial,Helvetica,sans-serif; font-size:15px; font-weight:bold; color:#ffffff; text-decoration:none;">Choose a new password</a>
            </td></tr>
          </table>
          <div style="font-family:Arial,Helvetica,sans-serif; font-size:12px; color:{MUTED}; padding-top:12px; line-height:18px;">
            The link works for 2 hours and can be used once.
          </div>
        </td></tr>
        <tr><td style="background:#ffffff; padding:14px 36px 26px 36px; font-family:Arial,Helvetica,sans-serif;">
          <div style="font-size:12px; color:{MUTED}; line-height:18px; border-top:1px solid {BORDER}; padding-top:14px;">
            Didn't ask for this? Ignore this email — your password stays as it is.
          </div>
        </td></tr>
        <tr><td style="background:{INK}; border-radius:0 0 12px 12px; padding:18px 36px; font-family:Arial,Helvetica,sans-serif;" align="center">
          <div style="font-size:12px; font-weight:bold; color:#ffffff; letter-spacing:1px;">{company}</div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def portal_welcome_html(company, portal_url, activate_url, username, who,
                        logo_cid="rflogo"):
    """The RANCHERS CUSTOMER PORTAL welcome email. No password travels by
    mail: the activation button carries a signed, expiring link where the
    customer sets their own password (better deliverability, better security)."""
    company = escape(company or "Ranchers Finest")
    portal_url = escape(portal_url or "")
    activate_url = escape(activate_url or "")
    username = escape(username or "")
    who = escape(who or "Customer")

    steps_html = ""
    for i, (title, body) in enumerate(GUIDE_STEPS, 1):
        steps_html += f"""
      <tr>
        <td style="padding:0 0 14px 0; vertical-align:top; width:36px;">
          <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
            <tr><td style="width:26px; height:26px; background:{ORANGE}; border-radius:13px; text-align:center; vertical-align:middle; color:#ffffff; font-family:Arial,Helvetica,sans-serif; font-size:13px; font-weight:bold; line-height:26px;">{i}</td></tr>
          </table>
        </td>
        <td style="padding:0 0 14px 10px; vertical-align:top; font-family:Arial,Helvetica,sans-serif;">
          <div style="font-size:14px; font-weight:bold; color:{INK}; padding-bottom:2px;">{escape(title)}</div>
          <div style="font-size:13px; color:{CHARCOAL}; line-height:19px;">{escape(body)}</div>
        </td>
      </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{company} — Customer Portal</title>
</head>
<body style="margin:0; padding:0; background:{PAPER};">
  <!-- preheader (hidden preview text) -->
  <div style="display:none; max-height:0; overflow:hidden; mso-hide:all;">
    Your {company} portal account is ready — sign in and set your own password.
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER}; border-collapse:collapse;">
    <tr><td align="center" style="padding:28px 12px;">

      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="border-collapse:collapse; max-width:600px; width:100%;">

        <!-- header -->
        <tr><td style="background:{INK}; border-radius:12px 12px 0 0; padding:30px 24px 24px 24px;" align="center">
          <img src="cid:{logo_cid}" alt="{company}" width="150" style="display:block; width:150px; max-width:150px; height:auto; margin:0 auto;">
          <div style="font-family:Arial,Helvetica,sans-serif; color:#ffffff; font-size:20px; font-weight:bold; letter-spacing:3px; padding-top:18px;">RANCHERS</div>
          <div style="font-family:Arial,Helvetica,sans-serif; color:{AMBER}; font-size:13px; font-weight:bold; letter-spacing:5px; padding-top:4px;">CUSTOMER&nbsp;PORTAL</div>
        </td></tr>
        <tr><td style="background:{ORANGE}; height:5px; font-size:0; line-height:0;">&nbsp;</td></tr>

        <!-- body -->
        <tr><td style="background:#ffffff; padding:34px 36px 8px 36px; font-family:Arial,Helvetica,sans-serif;">
          <div style="font-size:22px; font-weight:bold; color:{INK}; padding-bottom:12px;">Welcome, {who}</div>
          <div style="font-size:14px; color:{CHARCOAL}; line-height:22px;">
            Your account on the {company} Customer Portal is ready. Order at your
            agreed prices, follow every delivery, and reach our team — all in one
            place, any time.
          </div>
        </td></tr>

        <!-- credentials panel -->
        <tr><td style="background:#ffffff; padding:22px 36px 6px 36px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:separate; background:{CREAM}; border:1px solid {BORDER}; border-radius:10px;">
            <tr><td style="padding:18px 22px 6px 22px; font-family:Arial,Helvetica,sans-serif; font-size:11px; font-weight:bold; letter-spacing:2px; color:{MUTED};">YOUR LOGIN DETAILS</td></tr>
            <tr><td style="padding:0 22px 16px 22px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse; font-family:Arial,Helvetica,sans-serif;">
                <tr>
                  <td style="padding:6px 0; font-size:13px; color:{MUTED}; width:150px;">Portal</td>
                  <td style="padding:6px 0; font-size:14px; color:{INK};"><a href="{portal_url}" style="color:{ORANGE}; font-weight:bold; text-decoration:none;">{portal_url}</a></td>
                </tr>
                <tr>
                  <td style="padding:6px 0; font-size:13px; color:{MUTED}; border-top:1px solid {BORDER};">Username</td>
                  <td style="padding:6px 0; font-size:15px; color:{INK}; font-weight:bold; border-top:1px solid {BORDER}; font-family:'Courier New',Courier,monospace;">{username}</td>
                </tr>
              </table>
            </td></tr>
          </table>
        </td></tr>

        <!-- CTA -->
        <tr><td style="background:#ffffff; padding:24px 36px 10px 36px;" align="center">
          <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:separate;">
            <tr><td style="background:{ORANGE}; border-radius:8px;" align="center">
              <a href="{activate_url}" style="display:inline-block; padding:14px 44px; font-family:Arial,Helvetica,sans-serif; font-size:15px; font-weight:bold; color:#ffffff; text-decoration:none;">Activate your account</a>
            </td></tr>
          </table>
          <div style="font-family:Arial,Helvetica,sans-serif; font-size:12px; color:{MUTED}; padding-top:12px; line-height:18px;">
            The button opens a secure page where you choose your own password.<br>
            The link works for 72 hours — after that, ask us for a fresh one.
          </div>
        </td></tr>

        <!-- divider -->
        <tr><td style="background:#ffffff; padding:18px 36px 0 36px;">
          <div style="border-top:1px solid {BORDER}; font-size:0; line-height:0;">&nbsp;</div>
        </td></tr>

        <!-- guide -->
        <tr><td style="background:#ffffff; padding:10px 36px 10px 36px;">
          <div style="font-family:Arial,Helvetica,sans-serif; font-size:16px; font-weight:bold; color:{INK}; padding-bottom:14px;">How the portal works</div>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">{steps_html}
          </table>
          <div style="font-family:Arial,Helvetica,sans-serif; font-size:12px; color:{MUTED}; padding-top:2px;">The full guide lives on the Help page inside the portal.</div>
        </td></tr>

        <!-- footer -->
        <tr><td style="background:{INK}; border-radius:0 0 12px 12px; padding:22px 36px; font-family:Arial,Helvetica,sans-serif;" align="center">
          <div style="font-size:13px; font-weight:bold; color:#ffffff; letter-spacing:1px;">{company}</div>
          <div style="font-size:11px; color:#B8AC9F; padding-top:6px; line-height:17px;">
            Need a hand? Reply to this email or contact your sales representative.<br>
            You received this email because a portal account was created for you.
          </div>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""
