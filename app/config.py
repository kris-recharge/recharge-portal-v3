"""Central configuration — reads from environment / .env file."""

import logging
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

_log = logging.getLogger("rca.config")

# ── Timezone ──────────────────────────────────────────────────────────────────
AK_TZ = ZoneInfo("America/Anchorage")
UTC   = ZoneInfo("UTC")

# ── Supabase ──────────────────────────────────────────────────────────────────
SUPABASE_URL              = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY         = os.environ["SUPABASE_ANON_KEY"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
DATABASE_URL              = os.environ["DATABASE_URL"]

# ── Email ─────────────────────────────────────────────────────────────────────
SMTP_HOST        = os.getenv("SMTP_HOST", "smtp.office365.com")
SMTP_PORT        = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER        = os.environ["SMTP_USER"]
SMTP_PASSWORD    = os.environ["SMTP_PASSWORD"]
ALERT_EMAIL_TO   = os.getenv("ALERT_EMAIL_TO", "info@rechargealaska.net")
ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM", "info@rechargealaska.net")

# ── App ───────────────────────────────────────────────────────────────────────
APP_MODE        = os.getenv("APP_MODE", "web").lower()
# APP_ENV defaults to "production" so the safe behaviour (real auth) is the
# default whenever the variable is absent or misconfigured.
APP_ENV         = os.getenv("APP_ENV", "production").lower()
SECRET_KEY      = os.getenv("SECRET_KEY", "dev-secret-change-me")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")]

# DEV_BYPASS_AUTH disables Supabase auth and treats EVERY request as the admin
# user (allowed_evse_ids=None → sees all EVSEs). It is a local-development-only
# escape hatch and must NEVER be active in production, where it silently defeats
# per-user EVSE filtering. As defense in depth it is honoured ONLY when
# APP_ENV=development — so a stray DEV_BYPASS_AUTH=true (e.g. a dev .env shipped
# or baked into an image) cannot disable tenant isolation on a production server.
_dev_bypass_requested = os.getenv("DEV_BYPASS_AUTH", "false").lower() in ("1", "true", "yes")
DEV_BYPASS_AUTH = _dev_bypass_requested and APP_ENV == "development"

if _dev_bypass_requested and not DEV_BYPASS_AUTH:
    _log.warning(
        "DEV_BYPASS_AUTH=true was requested but IGNORED because APP_ENV=%r "
        "(bypass is honoured only when APP_ENV=development). Real auth is enforced.",
        APP_ENV,
    )
if DEV_BYPASS_AUTH:
    _log.warning(
        "DEV_BYPASS_AUTH is ACTIVE — Supabase auth is disabled and every request "
        "is treated as admin. This must only ever happen in local development."
    )
