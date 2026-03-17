"""ReCharge Alaska Portal v3 — FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .alerts import start_alert_thread
from .config import ALLOWED_ORIGINS
from .db import close_pool, create_pool, get_pool
from .routers import admin, alerts_config, alerts_sse, analytics, connectivity, export, sessions, status

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
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting ReCharge Alaska Portal v3")
    pool = await create_pool()
    async with pool.acquire() as conn:
        await conn.execute(_MIGRATION_SQL)
    logger.info("DB migration complete (alert_subscriptions, fired_alerts)")
    start_alert_thread()
    yield
    # Shutdown
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
    allow_methods=["GET", "POST"],
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


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "3.0.0"}
