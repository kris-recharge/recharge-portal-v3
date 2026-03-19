"""Utility data collection scheduler.

Runs once daily at 04:00 Alaska time via APScheduler.
Reads enabled accounts from Supabase, instantiates the right collector
for each, and collects the past 2 days of data (overlap prevents gaps).

Adding a new utility
--------------------
1. Add a row to utility_credentials (utility, username, password).
2. Add rows to utility_accounts (one per account/meter).
3. If it's a new *platform* (not SmartHub or mymeterQ), subclass
   AbstractCollector and register it in COLLECTOR_MAP below.

That's it — no code changes needed for new accounts on existing platforms.
"""

from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from ..db import get_pool
from .mymeterq import MyMeterQCollector
from .smarthub import SmartHubCollector

logger = logging.getLogger("rca.collectors.scheduler")

_AK_TZ = ZoneInfo("America/Anchorage")

# Map utility name → collector class.
# Add new platforms here when needed.
COLLECTOR_MAP = {
    "gvea": SmartHubCollector,
    "cvea": SmartHubCollector,
    "cea":  MyMeterQCollector,
    # "mea": SmartHubCollector,   # future: Matanuska Electric
}


# ── Core collection logic ─────────────────────────────────────────────────────

async def run_all_collectors(days_back: int = 2) -> None:
    """Fetch all enabled utility accounts and run their collectors.

    Called by the scheduler and exposed for manual trigger via the API.
    """
    pool = get_pool()

    # Load enabled accounts + their credentials in one query
    async with pool.acquire() as conn:
        accounts = await conn.fetch(
            """
            SELECT a.id, a.utility, a.account_number, a.display_name,
                   a.service_location_number, a.customer_number,
                   a.system_of_record, a.meter_group_id, a.enabled
            FROM   utility_accounts a
            WHERE  a.enabled = TRUE
            ORDER  BY a.utility, a.account_number
            """
        )
        creds_rows = await conn.fetch(
            "SELECT utility, username, password FROM utility_credentials"
        )

    # Build credentials lookup: utility → {username, password}
    creds: dict[str, dict] = {
        r["utility"]: {"username": r["username"], "password": r["password"]}
        for r in creds_rows
    }

    if not accounts:
        logger.info("Collector run: no enabled utility accounts found")
        return

    logger.info(
        "Collector run starting: %d accounts, days_back=%d",
        len(accounts), days_back,
    )

    # Group accounts by utility so we log + handle errors per-account
    for row in accounts:
        utility = row["utility"]

        collector_cls = COLLECTOR_MAP.get(utility)
        if not collector_cls:
            logger.warning(
                "No collector registered for utility '%s' — skipping account %s. "
                "Add it to COLLECTOR_MAP in scheduler.py.",
                utility, row["account_number"],
            )
            continue

        credential = creds.get(utility)
        if not credential:
            logger.warning(
                "No credentials found for utility '%s' — skipping account %s. "
                "Add a row to utility_credentials via the Admin Tab.",
                utility, row["account_number"],
            )
            continue

        try:
            collector = collector_cls(dict(row), credential)
            n = await collector.collect(pool, days_back=days_back)
            logger.info(
                "Collected %s/%s ('%s'): %d rows",
                utility, row["account_number"], row.get("display_name", ""), n,
            )
        except Exception as exc:
            logger.error(
                "Unhandled error collecting %s/%s: %s",
                utility, row["account_number"], exc,
                exc_info=True,
            )

    logger.info("Collector run complete")


# ── Scheduler setup ───────────────────────────────────────────────────────────

_scheduler: AsyncIOScheduler | None = None


def start_collector_scheduler() -> None:
    """Create and start the APScheduler instance.

    Called once from the FastAPI lifespan startup hook.
    Schedule: every day at 04:00 Alaska time.
    """
    global _scheduler

    _scheduler = AsyncIOScheduler(timezone=_AK_TZ)
    _scheduler.add_job(
        run_all_collectors,
        trigger=CronTrigger(hour=4, minute=0, timezone=_AK_TZ),
        id="utility_collect_daily",
        name="Daily utility kWh collection",
        replace_existing=True,
        misfire_grace_time=3600,  # run up to 1h late if server was down
    )
    _scheduler.start()
    logger.info(
        "Utility collector scheduler started — runs daily at 04:00 AK time"
    )


def stop_collector_scheduler() -> None:
    """Shutdown the scheduler gracefully on app teardown."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Utility collector scheduler stopped")
