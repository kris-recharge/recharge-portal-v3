"""ReCharge Alaska Portal v3 — FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .alerts import start_alert_thread
from .collectors.scheduler import start_collector_scheduler, stop_collector_scheduler
from .config import ALLOWED_ORIGINS
from .db import close_pool, create_pool
from .routers import (
    admin, alerts_config, alerts_sse, analytics,
    connectivity, export, sessions, status,
)
from .routers import utility

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("rca.main")


_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS alert_subscriptions (
    user_id     UUID        NOT NULL,
    alert_type  TEXT        NOT NULL,
    enabled     BOOLEAN     NOT NULL DEFAULT false,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, alert_type)
);

CREATE TABLE IF NOT EXISTS fired_alerts (
    id          UUID        NOT NULL DEFAULT gen_random_uuid(),
    fired_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    alert_type  TEXT        NOT NULL,
    asset_id    TEXT        NOT NULL,
    evse_name   TEXT        NOT NULL,
    message     TEXT        NOT NULL,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_fired_alerts_fired_at ON fired_alerts (fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_fired_alerts_asset_id  ON fired_alerts (asset_id);

-- ── Utility data collection tables ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS utility_accounts (
    id                      SERIAL      PRIMARY KEY,
    utility                 TEXT        NOT NULL,
    account_number          TEXT        NOT NULL,
    display_name            TEXT        NOT NULL DEFAULT '',
    service_location_number TEXT,
    customer_number         TEXT,
    system_of_record        TEXT        NOT NULL DEFAULT 'UTILITY',
    meter_group_id          TEXT,
    enabled                 BOOLEAN     NOT NULL DEFAULT TRUE,
    last_collected          TIMESTAMPTZ,
    last_error              TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (utility, account_number)
);

CREATE TABLE IF NOT EXISTS utility_credentials (
    id         SERIAL      PRIMARY KEY,
    utility    TEXT        NOT NULL UNIQUE,
    username   TEXT        NOT NULL,
    password   TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS utility_usage (
    id              BIGSERIAL   PRIMARY KEY,
    utility         TEXT        NOT NULL,
    account_number  TEXT        NOT NULL,
    meter_id        TEXT,
    interval_start  TIMESTAMPTZ NOT NULL,
    interval_end    TIMESTAMPTZ NOT NULL,
    kwh             NUMERIC(10, 4),
    is_estimated    BOOLEAN     NOT NULL DEFAULT FALSE,
    granularity_min INTEGER     NOT NULL,
    collected_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (utility, account_number, interval_start)
);

CREATE INDEX IF NOT EXISTS idx_utility_usage_lookup
    ON utility_usage (utility, account_number, interval_start DESC);

-- ── RLS: lock down credentials table ─────────────────────────────────────────
-- Enable RLS — service role bypasses automatically; anon/authenticated are denied.
ALTER TABLE utility_credentials ENABLE ROW LEVEL SECURITY;
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    logger.info("Starting ReCharge Alaska Portal v3")
    pool = await create_pool()
    async with pool.acquire() as conn:
        await conn.execute(_MIGRATION_SQL)
    logger.info("DB migration complete")
    start_alert_thread()
    start_collector_scheduler()
    yield
    # ── Shutdown ─────────────────────────────────────────────────────────────
    stop_collector_scheduler()
    await close_pool()
    logger.info("Shutdown complete")


app = FastAPI(
    title="ReCharge Alaska Portal API",
    version="3.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)

app.include_router(sessions.router)
app.include_router(analytics.router)
app.include_router(status.router)
app.include_router(connectivity.router)
app.include_router(export.router)
app.include_router(admin.router)
app.include_router(alerts_sse.router)
app.include_router(alerts_config.router)
app.include_router(utility.router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "3.0.0"}
