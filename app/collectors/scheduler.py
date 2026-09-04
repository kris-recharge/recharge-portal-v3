"""Background collection scheduler.

Currently schedules the Payter CCR transaction import every 15 minutes.

Utility kWh collection does NOT run here
----------------------------------------
Utility meter data (GVEA, CVEA, CEA) is collected by the standalone scrapers on
the Mac mini, not by this app:

    ~/CEA Scrapper/cea_scraper.py               (CEA  — MyMeter)
    ~/CEA Scrapper/GVEA Scraper/gvea_scraper.py (GVEA — SmartHub)
    ~/CEA Scrapper/CVEA Scraper/cvea_scraper.py (CVEA — SmartHub)

Those write to utility_usage directly and are scheduled by launchd. The VPS-side
collectors (smarthub.py / mymeterq.py) were removed on 2026-09-04: they had been
failing on every utility (SmartHub 403, mymeterQ captcha) and duplicated the Mac
mini's work against the same table. Running both risked double-counting, since
this app collected GVEA at 5-minute granularity while the scraper writes daily
totals into the same (utility, account_number, interval_start) key.

This app still *reads* utility_usage and manages utility_accounts — see
routers/utility.py. Only collection moved.
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..db import get_pool
from .payter import collect_payter

logger = logging.getLogger("rca.collectors.scheduler")

_AK_TZ = ZoneInfo("America/Anchorage")


# ── Core collection logic ─────────────────────────────────────────────────────

async def run_payter_collector() -> None:
    """Incremental Payter CCR transaction import (see payter.py)."""
    pool = await get_pool()
    try:
        n = await collect_payter(pool)
        logger.info("Payter collector run complete: %d transactions upserted", n)
    except Exception:
        logger.error("Payter collector run failed", exc_info=True)


# ── Scheduler setup ───────────────────────────────────────────────────────────

_scheduler: AsyncIOScheduler | None = None


def start_collector_scheduler() -> None:
    """Create and start the APScheduler instance.

    Called once from the FastAPI lifespan startup hook.
    """
    global _scheduler

    _scheduler = AsyncIOScheduler(timezone=_AK_TZ)
    _scheduler.add_job(
        run_payter_collector,
        # 15-min polling ≈ near-real-time: committed cost lands minutes after
        # the CC session closes. Each poll is one cheap "anything new?" request.
        trigger=IntervalTrigger(minutes=15),
        id="payter_collect_15min",
        name="Payter CCR transaction import (15 min)",
        replace_existing=True,
        misfire_grace_time=600,
    )
    _scheduler.start()
    logger.info("Collector scheduler started — Payter every 15 min")


def stop_collector_scheduler() -> None:
    """Shutdown the scheduler gracefully on app teardown."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Collector scheduler stopped")
