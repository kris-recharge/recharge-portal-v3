"""GET /api/analytics — daily session totals and start-time density heatmap data."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from ..auth import CurrentUser, filter_evse_ids
from ..constants import get_all_station_ids
from ..db import acquire
from ..models import AnalyticsResponse, DailyTotal, DensityPoint

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

_AK = ZoneInfo("America/Anchorage")


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

        # ── YTD peak: AK-local day with most energy since 1-Jan ──────────────
        # Independent of the date-range filter; bounded only to the current AK
        # calendar year and the allowed EVSEs. Resets automatically each 1-Jan.
        ytd_row = await conn.fetchrow(
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
                  AND (ts AT TIME ZONE 'America/Anchorage')
                        >= date_trunc('year', (now() AT TIME ZONE 'America/Anchorage'))
                GROUP BY station_id, connector_id, transaction_id
            ),
            daily AS (
                SELECT
                    (start_ts AT TIME ZONE 'America/Anchorage')::date AS ak_date,
                    COALESCE(SUM(energy_wh_delta), 0) / 1000.0         AS energy_kwh
                FROM sessions
                GROUP BY ak_date
            )
            SELECT ak_date, energy_kwh
            FROM daily
            ORDER BY energy_kwh DESC, ak_date DESC
            LIMIT 1
            """,
            allowed,
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

    max_daily_energy_kwh: float | None = None
    max_daily_energy_date: str | None = None
    if ytd_row is not None and ytd_row["energy_kwh"] is not None:
        max_daily_energy_kwh = round(float(ytd_row["energy_kwh"]), 2)
        max_daily_energy_date = str(ytd_row["ak_date"])

    return AnalyticsResponse(
        daily_totals=daily_totals,
        density=density,
        max_daily_energy_kwh=max_daily_energy_kwh,
        max_daily_energy_date=max_daily_energy_date,
    )
