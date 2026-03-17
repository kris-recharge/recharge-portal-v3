"""Central configuration — reads from environment / .env file."""

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

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
SECRET_KEY      = os.getenv("SECRET_KEY", "dev-secret-change-me")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")]
DEV_BYPASS_AUTH = os.getenv("DEV_BYPASS_AUTH", "false").lower() in ("1", "true", "yes")
