# =====================================================================
#  CONFIG — edit this ONCE.
#  Save as many SUBJECTS, BODIES and ATTACHMENTS as you want below.
#  On the web page you pick any of each from a dropdown — or write /
#  upload your own at send time. They are independent: mix and match.
# =====================================================================

# ---- Your email account (SMTP) ----
SMTP_HOST = "smtp.gmail.com"     # Gmail: smtp.gmail.com | Outlook: smtp.office365.com
SMTP_PORT = 587                  # 587 = STARTTLS, 465 = SSL
SECURITY  = "tls"                # "tls" | "ssl" | "none"

USERNAME  = "yourmail@gmail.com"   # login email
PASSWORD  = "App Password"     # Gmail/Outlook: use an APP PASSWORD

FROM_NAME = "Your Name "             # default sender name (overridable on the page)
FROM_ADDR = ""                      # leave "" to use USERNAME


# ---- SAVED SUBJECTS ----  (just a list of strings)
SUBJECTS = [
    "Welcome aboard!",
    "A special offer just for you",
    "Your invoice",
    "Friendly reminder",
]


# ---- SAVED BODIES ----  (label  ->  message text)
# Set the matching HTML flag in BODIES_HTML below if a body uses HTML tags.
BODIES = {

    "Welcome message": """Hi there,

Thanks for joining us. We're glad to have you on board.

Regards,
The Team
""",

    "Offer message": """Hello,

Here's a special offer we think you'll love.

Cheers,
The Team
""",

    "Invoice note": """Hi,

Please find your invoice attached.

Thank you,
Accounts
""",
}

# Names of bodies above that are HTML (leave empty if all are plain text)
BODIES_HTML = [
    # "Some HTML body label",
]


# ---- SAVED ATTACHMENTS ----  (file paths)
# Drop files into the attachments/ folder, then list them here.
ATTACHMENTS = [
    # "attachments/brochure.pdf",
    # "attachments/pricelist.pdf",
    # "attachments/logo.png",
]
