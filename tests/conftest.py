"""Test setup.

The repo's .env points DATABASE_URL at PRODUCTION and turns on
DEV_BYPASS_AUTH. app.config calls load_dotenv(), which never overrides a
variable that is already set — so everything is pinned here, before any app
module is imported, and the DB tests refuse to run against a Supabase host.

DB-backed tests need a throwaway Postgres, e.g.:
    docker run -d --rm --name rca_test -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres pytest tests
Without TEST_DATABASE_URL they are skipped; the pure-logic tests always run.
"""

import os

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

os.environ.update({
    "APP_ENV": "test",
    "DEV_BYPASS_AUTH": "false",
    "SUPABASE_URL": "http://supabase.invalid",
    "SUPABASE_ANON_KEY": "test",
    "SUPABASE_SERVICE_ROLE_KEY": "test",
    "SMTP_USER": "test@example.invalid",
    "SMTP_PASSWORD": "test",
    # Never the real one, even when DB tests are skipped.
    "DATABASE_URL": TEST_DATABASE_URL or "postgresql://nobody@db.invalid:5432/none",
})

if TEST_DATABASE_URL and "supabase" in TEST_DATABASE_URL:
    raise RuntimeError("TEST_DATABASE_URL must be a throwaway local database, not Supabase")


# ── Shared DB fixtures ────────────────────────────────────────────────────────

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "app/migrations"
# Applied in order, exactly as they are run by hand on production.
MIGRATIONS = [_MIGRATIONS_DIR / n for n in (
    "20261120_admin_deadlines.sql",
    "20261121_deadline_business_day_roll.sql",
)]

# Just enough of the portal's existing tables (and Supabase's auth schema) for
# the deadline features to run against.
_SUPPORT_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE auth.users (id uuid PRIMARY KEY, email text NOT NULL);
CREATE TABLE portal_users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), email text NOT NULL,
    active boolean NOT NULL DEFAULT true, banner_all_alert_types boolean NOT NULL DEFAULT true);
CREATE TABLE alert_subscriptions (
    user_id uuid NOT NULL, alert_type text NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    email_enabled boolean NOT NULL DEFAULT true, push_enabled boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (user_id, alert_type));
CREATE TABLE push_subscriptions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL,
    endpoint text NOT NULL UNIQUE, p256dh text NOT NULL, auth text NOT NULL,
    user_agent text NOT NULL DEFAULT '', device_label text,
    created_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now());
"""


@pytest.fixture(scope="session")
def db():
    """Fresh schema, set up the way Supabase is: anon/authenticated roles that
    get ALL on new tables by default. The migration has to take that back."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    import psycopg
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "DROP TABLE IF EXISTS admin_deadline_alerts, admin_deadlines, portal_users, "
            "alert_subscriptions, push_subscriptions; DROP SCHEMA IF EXISTS auth CASCADE"
        )
        for role in ("anon", "authenticated"):
            conn.execute(
                f"DO $$ BEGIN CREATE ROLE {role} NOLOGIN; "
                f"EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            )
        conn.execute("GRANT USAGE ON SCHEMA public TO anon, authenticated")
        conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated")
        conn.execute(_SUPPORT_SCHEMA)
        for m in MIGRATIONS:
            conn.execute(m.read_text())
        yield conn


def make_client(email: str | None, user_id: str = "00000000-0000-0000-0000-0000000000aa"):
    """A TestClient for the deadline + me + alert-config routers, signed in as `email`."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import db as app_db
    from app.auth import PortalUser, get_current_user
    from app.routers import alerts_config, deadlines, me

    # asyncpg pools are bound to the event loop that made them; every
    # TestClient runs its own loop, so start each one with a fresh pool.
    app_db._pool = None
    app = FastAPI()
    for r in (deadlines.router, me.router, alerts_config.router):
        app.include_router(r)
    if email:
        app.dependency_overrides[get_current_user] = lambda: PortalUser(
            email=email, user_id=user_id,
            allowed_evse_ids=None if email.lower().endswith("@rechargealaska.net") else ["X"],
        )
    return TestClient(app)
