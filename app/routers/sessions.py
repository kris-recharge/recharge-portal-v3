"""GET /api/sessions — charging session list with pagination."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from ..auth import CurrentUser, filter_evse_ids
from ..constants import (
    connector_type_for,
    display_name,
    get_all_station_ids,
    location_label,
)
from ..db import acquire
from ..models import ChargingSession, MeterValuePoint, SessionDetailResponse, SessionsResponse

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

_AK = ZoneInfo("America/Anchorage")


def _fmt_ak(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_AK).strftime("%Y-%m-%d %H:%M")


def _resolve_soc(
    raw_start:         float | None,
    raw_first_nonzero: float | None,
    raw_end:           float | None,
) -> tuple[float | None, float | None]:
    """Return (soc_start_pct, soc_end_pct) with two corrections applied.

    1. Scale normalisation — some chargers report SoC as a 0-1 fraction instead
       of 0-100.  If the session-end value is ≤ 1.0 we multiply by 100.

    2. Bogus-zero filter — chargers often emit one or more soc=0 readings at
       session start before the BMS has responded.  Rule: if the first reading
       is 0% AND the first *non-zero* reading is NOT ≈ 1% (i.e. the car was
       not actually near-depleted), discard all leading zeros and use the first
       non-zero reading as the true starting SoC.  If soc_first_nonzero ≤ 1.5%
       we assume the car genuinely started near-empty and keep 0%.
    """
    # Pick scale from the most-reliable reference: soc_end, then soc_start
    ref = raw_end if raw_end is not None else raw_start
    scale = 100.0 if (ref is not None and float(ref) <= 1.0) else 1.0

    soc_end_pct = round(float(raw_end) * scale, 1) if raw_end is not None else None

    if raw_start is None:
        return None, soc_end_pct

    start_val = float(raw_start) * scale

    # Leading-zero artifact: skip any number of 0% readings until we find a
    # meaningful SoC, but only if that value is clearly above 1.5%
    # (preserves genuine near-depleted starts like 0 → 1 → 2%).
    if start_val == 0.0 and raw_first_nonzero is not None:
        nonzero_val = float(raw_first_nonzero) * scale
        if nonzero_val > 1.5:
            start_val = nonzero_val

    return round(start_val, 1), soc_end_pct


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
    # Date-only — apply AK midnight / end-of-day boundary
    suffix = "T23:59:59" if end else "T00:00:00"
    return (
        datetime.fromisoformat(f"{val}{suffix}")
        .replace(tzinfo=_AK)
        .astimezone(timezone.utc)
    )


@router.get("", response_model=SessionsResponse)
async def get_sessions(
    user: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    station_id: list[str] | None = Query(None),
    start_date: str | None = Query(None, description="YYYY-MM-DD AK local OR ISO-8601 datetime (live mode)"),
    end_date:   str | None = Query(None, description="YYYY-MM-DD AK local OR ISO-8601 datetime (live mode)"),
):
    # Resolve EVSE filter
    all_ids = get_all_station_ids()
    allowed = filter_evse_ids(all_ids, user.allowed_evse_ids)
    if station_id:
        allowed = [s for s in station_id if s in allowed]

    # Convert date/datetime params → UTC timestamps for the query
    start_utc: datetime | None = _parse_dt_param(start_date)           if start_date else None
    end_utc:   datetime | None = _parse_dt_param(end_date, end=True)   if end_date   else None

    offset = (page - 1) * page_size

    async with acquire() as conn:
        # Build session aggregates from meter_values_parsed
        # One row per (station_id, connector_id, transaction_id)
        rows = await conn.fetch(
            """
            WITH sessions AS (
                SELECT
                    m.station_id,
                    m.connector_id,
                    m.transaction_id,
                    MIN(m.ts)                                         AS start_utc,
                    MAX(m.ts)                                         AS end_utc,
                    MAX(m.power_w)                                    AS max_power_w,
                    MAX(m.energy_wh) - MIN(m.energy_wh)               AS energy_wh_delta,
                    MIN(m.energy_wh)                                  AS energy_wh_min,
                    MAX(m.energy_wh)                                  AS energy_wh_max,
                    (SELECT mv2.soc FROM meter_values_parsed mv2
                     WHERE mv2.station_id = m.station_id
                       AND mv2.connector_id = m.connector_id
                       AND mv2.transaction_id = m.transaction_id
                       AND mv2.soc IS NOT NULL
                     ORDER BY mv2.ts ASC LIMIT 1)                     AS soc_start,
                    (SELECT mv2.soc FROM meter_values_parsed mv2
                     WHERE mv2.station_id = m.station_id
                       AND mv2.connector_id = m.connector_id
                       AND mv2.transaction_id = m.transaction_id
                       AND mv2.soc IS NOT NULL
                       AND mv2.soc > 0
                     ORDER BY mv2.ts ASC LIMIT 1)                     AS soc_first_nonzero,
                    (SELECT mv2.soc FROM meter_values_parsed mv2
                     WHERE mv2.station_id = m.station_id
                       AND mv2.connector_id = m.connector_id
                       AND mv2.transaction_id = m.transaction_id
                       AND mv2.soc IS NOT NULL
                     ORDER BY mv2.ts DESC LIMIT 1)                    AS soc_end
                FROM meter_values_parsed m
                WHERE m.station_id = ANY($1::text[])
                  AND m.transaction_id IS NOT NULL
                  AND ($2::timestamptz IS NULL OR m.ts >= $2)
                  AND ($3::timestamptz IS NULL OR m.ts <= $3)
                GROUP BY m.station_id, m.connector_id, m.transaction_id
            ),
            with_auth AS (
                SELECT
                    s.*,
                    (SELECT o.action_payload->>'idTag' FROM ocpp_events o
                     WHERE o.asset_id = s.station_id
                       AND o.action = 'Authorize'
                       AND o.action_payload->>'idTag' LIKE 'VID:%'
                       AND o.received_at BETWEEN s.start_utc - INTERVAL '60 minutes'
                                             AND s.start_utc + INTERVAL '5 minutes'
                     ORDER BY ABS(EXTRACT(EPOCH FROM (o.received_at - s.start_utc))) ASC
                     LIMIT 1) AS id_tag,
                    (SELECT ep.price_per_kwh FROM evse_pricing ep
                     WHERE ep.station_id = s.station_id
                       AND ep.effective_start <= s.start_utc
                       AND (ep.effective_end IS NULL OR ep.effective_end > s.start_utc)
                     ORDER BY ep.effective_start DESC LIMIT 1) AS price_per_kwh,
                    (SELECT ep.connection_fee FROM evse_pricing ep
                     WHERE ep.station_id = s.station_id
                       AND ep.effective_start <= s.start_utc
                       AND (ep.effective_end IS NULL OR ep.effective_end > s.start_utc)
                     ORDER BY ep.effective_start DESC LIMIT 1) AS connection_fee
                FROM sessions s
            )
            SELECT *,
                   COUNT(*) OVER()                                        AS total_count,
                   SUM(energy_wh_delta) OVER()                            AS agg_energy_wh,
                   SUM(
                       CASE WHEN price_per_kwh IS NOT NULL OR connection_fee IS NOT NULL
                            THEN COALESCE(connection_fee, 0)
                                 + (energy_wh_delta / 1000.0) * COALESCE(price_per_kwh, 0)
                            ELSE 0 END
                   ) OVER()                                                AS agg_revenue,
                   AVG(EXTRACT(EPOCH FROM (end_utc - start_utc)) / 60.0) OVER()
                                                                           AS agg_avg_duration_min
            FROM with_auth
            ORDER BY end_utc DESC NULLS LAST
            LIMIT $4 OFFSET $5
            """,
            allowed,
            start_utc,
            end_utc,
            page_size,
            offset,
        )

    total         = int(rows[0]["total_count"])                       if rows else 0
    total_energy  = float(rows[0]["agg_energy_wh"] or 0) / 1000.0    if rows else 0.0
    total_revenue = float(rows[0]["agg_revenue"]   or 0)              if rows else 0.0
    avg_dur_raw   = rows[0]["agg_avg_duration_min"]                    if rows else None
    avg_duration  = float(avg_dur_raw) if avg_dur_raw is not None else None

    sessions: list[ChargingSession] = []
    for r in rows:
        sid        = r["station_id"]
        conn_id    = r["connector_id"]
        tx_id      = str(r["transaction_id"])
        start_dt   = r["start_utc"]
        end_dt     = r["end_utc"]
        dur_min    = (end_dt - start_dt).total_seconds() / 60.0 if start_dt and end_dt else None
        energy_wh  = r["energy_wh_delta"]
        energy_kwh = round(float(energy_wh) / 1000.0, 3) if energy_wh else None
        max_kw     = round(float(r["max_power_w"]) / 1000.0, 2) if r["max_power_w"] else None

        p_kwh   = float(r["price_per_kwh"] or 0)
        c_fee   = float(r["connection_fee"] or 0)
        est_rev = round(c_fee + (energy_kwh or 0) * p_kwh, 2) if (p_kwh or c_fee) else None

        soc_start_pct, soc_end_pct = _resolve_soc(
            r["soc_start"], r["soc_first_nonzero"], r["soc_end"]
        )

        sessions.append(
            ChargingSession(
                transaction_id  = tx_id,
                station_id      = sid,
                evse_name       = display_name(sid),
                location        = location_label(sid),
                connector_id    = conn_id,
                connector_type  = connector_type_for(sid, conn_id or 0, start_dt),
                start_dt        = _fmt_ak(start_dt),
                end_dt          = _fmt_ak(end_dt),
                duration_min    = round(dur_min, 1) if dur_min is not None else None,
                max_power_kw    = max_kw,
                energy_kwh      = energy_kwh,
                soc_start       = soc_start_pct,
                soc_end         = soc_end_pct,
                id_tag          = r["id_tag"],
                est_revenue_usd = est_rev,
            )
        )

    return SessionsResponse(
        sessions=sessions,
        total=total,
        page=page,
        page_size=page_size,
        total_energy_kwh=round(total_energy, 3),
        total_revenue_usd=round(total_revenue, 2),
        avg_duration_min=round(avg_duration, 1) if avg_duration is not None else None,
    )


# ── Session Detail — time-series meter values for a single transaction ─────────

@router.get("/detail", response_model=SessionDetailResponse)
async def get_session_detail(
    user: CurrentUser,
    station_id:     str = Query(...),
    transaction_id: str = Query(...),
    connector_id:   int | None = Query(None),
):
    # Auth: verify this station is allowed for the user
    all_ids  = get_all_station_ids()
    allowed  = filter_evse_ids(all_ids, user.allowed_evse_ids)
    if station_id not in allowed:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="EVSE not permitted")

    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                ts,
                power_w,
                power_offered_w,
                current_offered_a,
                energy_wh,
                soc,
                voltage_v
            FROM meter_values_parsed
            WHERE station_id = $1
              AND transaction_id::text = $2
              AND ($3::int IS NULL OR connector_id = $3)
            ORDER BY ts ASC
            """,
            station_id,
            transaction_id,
            connector_id,
        )

    if not rows:
        return SessionDetailResponse(
            station_id=station_id,
            evse_name=display_name(station_id),
            transaction_id=transaction_id,
            start_dt="—",
            end_dt=None,
            points=[],
        )

    # Baseline energy to session start
    first_energy_wh = next(
        (float(r["energy_wh"]) for r in rows if r["energy_wh"] is not None), None
    )

    # SoC normalisation: some chargers send 0–1 fraction instead of 0–100.
    # Use the max value across ALL rows to distinguish: if max ≤ 1.0 it's fractional.
    soc_max = max((float(r["soc"]) for r in rows if r["soc"] is not None), default=None)
    soc_scale = 100.0 if (soc_max is not None and soc_max <= 1.0) else 1.0

    points: list[MeterValuePoint] = []
    for r in rows:
        ts = r["ts"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_ak_str = ts.astimezone(_AK).strftime("%Y-%m-%d %H:%M")

        raw_soc = r["soc"]
        soc_pct = round(float(raw_soc) * soc_scale, 1) if raw_soc is not None else None

        ew = r["energy_wh"]
        e_delta = (
            round((float(ew) - first_energy_wh) / 1000.0, 3)
            if ew is not None and first_energy_wh is not None
            else None
        )

        pw = r["power_w"]
        poffered = r["power_offered_w"]

        points.append(
            MeterValuePoint(
                ts_ak=ts_ak_str,
                power_kw=round(float(pw) / 1000.0, 2) if pw is not None else None,
                power_offered_kw=round(float(poffered) / 1000.0, 2) if poffered is not None else None,
                current_offered_a=round(float(r["current_offered_a"]), 1) if r["current_offered_a"] is not None else None,
                soc=soc_pct,
                energy_kwh_delta=e_delta,
                voltage_v=round(float(r["voltage_v"]), 0) if r["voltage_v"] is not None else None,
            )
        )

    start_dt = _fmt_ak(rows[0]["ts"])
    end_dt   = _fmt_ak(rows[-1]["ts"])

    return SessionDetailResponse(
        station_id=station_id,
        evse_name=display_name(station_id),
        transaction_id=transaction_id,
        start_dt=start_dt,
        end_dt=end_dt,
        points=points,
    )
