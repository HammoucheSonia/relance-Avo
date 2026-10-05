import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-change-me")

    BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR      = os.path.join(BASE_DIR, "data")
    UPLOAD_DIR    = os.path.join(DATA_DIR, "uploads")
    JOBS_DIR      = os.path.join(DATA_DIR, "jobs")
    FACTURES_DIR  = os.path.join(DATA_DIR, "factures")
    CONTACTS_FILE = os.path.join(DATA_DIR, "contacts.csv")
    HISTORY_FILE  = os.path.join(DATA_DIR, "history.json")
    PAIEMENTS_FILE = os.path.join(DATA_DIR, "paiements.json")
    LOG_DIR       = os.path.join(BASE_DIR, "logs")
    LOG_FILE      = os.path.join(LOG_DIR, "reminders.log")
    SENT_LOG      = os.path.join(LOG_DIR, "sent.csv")

    # ─── SMTP ────────────────────────────────────────────
    SMTP_HOST     = os.environ.get("SMTP_HOST", "")
    SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER     = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_USE_TLS  = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
    FROM_EMAIL    = os.environ.get("FROM_EMAIL", "compta@avogreen.fr")
    FROM_NAME     = os.environ.get("FROM_NAME", "AVO GREEN - Comptabilité")
    REPLY_TO      = os.environ.get("REPLY_TO", "")

    # ─── Modes ───────────────────────────────────────────
    DRY_RUN   = os.environ.get("DRY_RUN", "true").lower() == "true"
    AUTO_SEND = os.environ.get("AUTO_SEND", "false").lower() == "true"

    # ─── Stripe (optionnel) ──────────────────────────────
    STRIPE_ENABLED         = os.environ.get("STRIPE_ENABLED", "false").lower() == "true"
    STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_SEUIL_PETIT     = float(os.environ.get("STRIPE_SEUIL_PETIT", "200"))
    BASE_URL               = os.environ.get("BASE_URL", "http://localhost:5000")

    # ─── Récap hebdo automatique (vendredi) ─────────────
    RECAP_HEBDO_AUTO  = os.environ.get("RECAP_HEBDO_AUTO", "false").lower() == "true"
    RECAP_HEBDO_HEURE = int(os.environ.get("RECAP_HEBDO_HEURE", "17"))

    # ─── Société ─────────────────────────────────────────
    COMPANY_NAME    = "AVO GREEN"
    COMPANY_ADDRESS = "1 Allée des Violettes - Fleurs 452 - Bât. C1 - 94638 RUNGIS CEDEX"
    COMPANY_PHONE   = "01 46 86 47 47"
    COMPANY_EMAIL   = "compta@avogreen.fr"
    COMPANY_SIRET   = "511 695 918 000 25"
    COMPANY_TVA     = "FR07 511695918"
    COMPANY_RCS     = "RCS Créteil 511 695 918"

    # ─── Seuils de relance (jours) ──────────────────────
    SEUIL_RAPPEL_1    = 7
    SEUIL_RAPPEL_2    = 15
    SEUIL_RAPPEL_3    = 30
    SEUIL_MED         = 45
    SEUIL_CONTENTIEUX = 90

    MAX_ATTACH_MB = 15