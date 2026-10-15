"""GET /api/analytics — daily session totals and start-time density heatmap data."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from ..auth import CurrentUser, filter_evse_ids
from ..constants import get_all_station_ids
from ..db import acquire
from ..models import AnalyticsResponse, DailyTotal, DensityPoint

router = APIRouter(prefix="/api/analytics", tags=["analytics"])
log = logging.getLogger(__name__)

_AK = ZoneInfo("America/Anchorage")


# ── YTD peak cache ────────────────────────────────────────────────────────────
# The Max Dispensed YTD KPI used to re-aggregate every meter value since 1-Jan
# on every Dashboard load (~4 s by October, and growing until New Year). Days
# before today barely move, so their per-station totals are cached fleet-wide —
# one query serves every user's allowlist and EVSE filter, since a day's total
# for any subset is just the sum of its stations. Today is recomputed live on
# each request, so a record day still shows up as it happens. The cache refreshes
# in the background every _YTD_TTL_SEC (late replays into past days land within
# that) and synchronously when the AK date rolls over.
_YTD_TTL_SEC = 900
_ytd_cache: dict = {"today": None, "at": 0.0, "by_day": {}}
_ytd_lock = asyncio.Lock()
_ytd_task: asyncio.Task | None = None   # held so the background refresh isn't GC'd


def _ak_midnight_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=_AK).astimezone(timezone.utc)


async def _load_past_days(today: date) -> dict[date, dict[str, Decimal]]:
    """Per-station kWh by AK session-start date, 1-Jan up to (not incl.) today."""
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            WITH sessions AS (
                SELECT
                    station_id,
                    connector_id,
                    transaction_id,
                    MIN(ts)                                   AS start_ts,
                    MAX(energy_wh) - MIN(energy_wh)           AS energy_wh_delta
                FROM meter_values_parsed
                WHERE station_id = ANY($1::text[])
                  AND transaction_id IS NOT NULL
                  AND ts >= $2
                GROUP BY station_id, connector_id, transaction_id
            )
            SELECT
                station_id,
                (start_ts AT TIME ZONE 'America/Anchorage')::date AS ak_date,
                COALESCE(SUM(energy_wh_delta), 0) / 1000.0        AS energy_kwh
            FROM sessions
            WHERE start_ts < $3
            GROUP BY station_id, ak_date
            """,
            get_all_station_ids(),
            _ak_midnight_utc(date(today.year, 1, 1)),
            _ak_midnight_utc(today),
        )
    by_day: dict[date, dict[str, Decimal]] = {}
    for r in rows:
        by_day.setdefault(r["ak_date"], {})[r["station_id"]] = r["energy_kwh"]
    return by_day


async def _refresh_ytd(today: date) -> None:
    try:
        by_day = await _load_past_days(today)
        if _ytd_cache["today"] == today:   # the date hasn't rolled over meanwhile
            _ytd_cache.update(at=time.monotonic(), by_day=by_day)
    except Exception:
        log.exception("YTD peak cache refresh failed")


async def _past_days_cached(today: date) -> dict[date, dict[str, Decimal]]:
    global _ytd_task
    if _ytd_cache["today"] != today:
        async with _ytd_lock:
            if _ytd_cache["today"] != today:
                by_day = await _load_past_days(today)
                _ytd_cache.update(today=today, at=time.monotonic(), by_day=by_day)
    elif (time.monotonic() - _ytd_cache["at"] > _YTD_TTL_SEC
          and (_ytd_task is None or _ytd_task.done())):
        # Stale but same day: serve what we have, refresh behind the response.
        _ytd_task = asyncio.create_task(_refresh_ytd(today))
    return _ytd_cache["by_day"]


def _parse_dt_param(val: str, *, end: bool = False) -> datetime:
    """Accept YYYY-MM-DD (AK midnight boundary) **or** a full ISO-8601 datetime.

    Live-mode callers send a UTC ISO string (e.g. ``2026-03-08T08:50:00.000Z``).
    Static-mode callers send a bare date  (e.g. ``2026-03-08``).
    """
    if "T" in val or val.endswith("Z") or "+" in val[10:]:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    suffix = "T23:59:59" if end else "T00:00:00"
    return (
        datetime.fromisoformat(f"{val}{suffix}")
        .replace(tzinfo=_AK)
        .astimezone(timezone.utc)
    )


@router.get("", response_model=AnalyticsResponse)
async def get_analytics(
    user: CurrentUser,
    station_id: list[str] | None = Query(None),
    start_date: str | None = Query(None, description="YYYY-MM-DD AK local OR ISO-8601 datetime (live mode)"),
    end_date:   str | None = Query(None, description="YYYY-MM-DD AK local OR ISO-8601 datetime (live mode)"),
):
    # ── Resolve allowed EVSEs ─────────────────────────────────────────────────
    all_ids = get_all_station_ids()
    allowed = filter_evse_ids(all_ids, user.allowed_evse_ids)
    if station_id:
        allowed = [s for s in station_id if s in allowed]

    # ── Convert date/datetime params → UTC timestamps for the query ───────────
    start_utc: datetime | None = _parse_dt_param(start_date)           if start_date else None
    end_utc:   datetime | None = _parse_dt_param(end_date, end=True)   if end_date   else None

    # ── YTD peak: AK-local day with most energy since 1-Jan ──────────────────
    # Independent of the date-range filter; bounded only to the current AK
    # calendar year and the allowed EVSEs. Resets automatically each 1-Jan.
    ak_today  = datetime.now(_AK).date()
    past_days = await _past_days_cached(ak_today)

    async with acquire() as conn:
        # ── Daily totals: count + energy per AK-local start date ─────────────
        daily_rows = await conn.fetch(
            """
            WITH sessions AS (
                SELECT
                    station_id,
                    connector_id,
                    transaction_id,
                    MIN(ts)                                   AS start_ts,
                    MAX(energy_wh) - MIN(energy_wh)           AS energy_wh_delta
                FROM meter_values_parsed
                WHERE station_id = ANY($1::text[])
                  AND transaction_id IS NOT NULL
                  AND ($2::timestamptz IS NULL OR ts >= $2)
                  AND ($3::timestamptz IS NULL OR ts <= $3)
                GROUP BY station_id, connector_id, transaction_id
            )
            SELECT
                (start_ts AT TIME ZONE 'America/Anchorage')::date AS ak_date,
                COUNT(*)::int                                      AS session_count,
                COALESCE(SUM(energy_wh_delta), 0) / 1000.0        AS energy_kwh
            FROM sessions
            GROUP BY ak_date
            ORDER BY ak_date
            """,
            allowed,
            start_utc,
            end_utc,
        )

        # ── YTD peak, today's part: sessions that STARTED since AK midnight ──
        # Past days come from _past_days_cached. A session still running from
        # yesterday has meter rows today too, so it's excluded by checking for
        # earlier rows of the same transaction — it belongs to yesterday.
        today_rows = await conn.fetch(
            """
            WITH sessions AS (
                SELECT
                    station_id,
                    connector_id,
                    transaction_id,
                    MAX(energy_wh) - MIN(energy_wh)           AS energy_wh_delta
                FROM meter_values_parsed
                WHERE station_id = ANY($1::text[])
                  AND transaction_id IS NOT NULL
                  AND ts >= $3
                GROUP BY station_id, connector_id, transaction_id
            )
            SELECT station_id, COALESCE(SUM(energy_wh_delta), 0) / 1000.0 AS energy_kwh
            FROM sessions s
            WHERE NOT EXISTS (
                SELECT 1 FROM meter_values_parsed p
                WHERE p.transaction_id = s.transaction_id
                  AND p.station_id     = s.station_id
                  AND p.connector_id IS NOT DISTINCT FROM s.connector_id
                  AND p.ts >= $2
                  AND p.ts <  $3
            )
            GROUP BY station_id
            """,
            allowed,
            _ak_midnight_utc(date(ak_today.year, 1, 1)),
            _ak_midnight_utc(ak_today),
        )

        # ── Density: session-start counts by AK day-of-week + hour ───────────
        density_rows = await conn.fetch(
            """
            WITH sessions AS (
                SELECT
                    station_id,
                    connector_id,
                    transaction_id,
                    MIN(ts) AS start_ts
                FROM meter_values_parsed
                WHERE station_id = ANY($1::text[])
                  AND transaction_id IS NOT NULL
                  AND ($2::timestamptz IS NULL OR ts >= $2)
                  AND ($3::timestamptz IS NULL OR ts <= $3)
                GROUP BY station_id, connector_id, transaction_id
            )
            SELECT
                EXTRACT(DOW  FROM (start_ts AT TIME ZONE 'America/Anchorage'))::int AS dow,
                EXTRACT(HOUR FROM (start_ts AT TIME ZONE 'America/Anchorage'))::int AS hour,
                COUNT(*)::int AS count
            FROM sessions
            GROUP BY dow, hour
            ORDER BY dow, hour
            """,
            allowed,
            start_utc,
            end_utc,
        )

    daily_totals = [
        DailyTotal(
            date=str(r["ak_date"]),
            count=r["session_count"],
            energy_kwh=round(float(r["energy_kwh"]), 2),
        )
        for r in daily_rows
    ]

    density = [
        DensityPoint(
            dow=r["dow"],
            hour=r["hour"],
            count=r["count"],
        )
        for r in density_rows
    ]

    # A day counts only if one of the allowed EVSEs had a session start on it.
    allowed_set = set(allowed)
    daily_kwh: dict[date, Decimal] = {
        d: sum((v for sid, v in per_station.items() if sid in allowed_set), Decimal(0))
        for d, per_station in past_days.items()
        if allowed_set.intersection(per_station)
    }
    if today_rows:
        daily_kwh[ak_today] = sum((r["energy_kwh"] for r in today_rows), Decimal(0))

    max_daily_energy_kwh: float | None = None
    max_daily_energy_date: str | None = None
    if daily_kwh:
        # Most energy wins; a tie goes to the later day.
        peak_date, peak_kwh = max(daily_kwh.items(), key=lambda kv: (kv[1], kv[0]))
        max_daily_energy_kwh = round(float(peak_kwh), 2)
        max_daily_energy_date = str(peak_date)

    return AnalyticsResponse(
        daily_totals=daily_totals,
        density=density,
        max_daily_energy_kwh=max_daily_energy_kwh,
        max_daily_energy_date=max_daily_energy_date,
    )
