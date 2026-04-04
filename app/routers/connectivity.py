"""GET /api/connectivity — per-charger last-seen status.

Last-seen is the MAX of:
  1. Most recent ocpp_events.received_at  (heartbeats, status notifications, etc.)
  2. Most recent meter_values_parsed.received_at  (MeterValues during active sessions)

Tritium RTM chargers (ARG) do not send periodic Heartbeats — they are event-driven.
During an active session they stream MeterValues, so source #2 keeps them "online".
Between sessions, the last StatusNotification is the only signal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from ..auth import CurrentUser, filter_evse_ids
from ..constants import (
    display_name,
    get_all_station_ids,
    location_label,
)
from ..db import acquire
from ..models import ConnectivityRecord, ConnectivityResponse

router = APIRouter(prefix="/api/connectivity", tags=["connectivity"])

_AK = ZoneInfo("America/Anchorage")

# A charger is considered online if last activity < 20 min ago (matches alert threshold)
_OFFLINE_THRESHOLD_MIN = 20


def _fmt_ak(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_AK).strftime("%Y-%m-%d %H:%M:%S")


@router.get("", response_model=ConnectivityResponse)
async def get_connectivity(user: CurrentUser):
    all_ids = get_all_station_ids()
    allowed = filter_evse_ids(all_ids, user.allowed_evse_ids)

    now_utc = datetime.now(tz=timezone.utc)

    async with acquire() as conn:
        # Latest OCPP event per charger (for action label + connection_id)
        ocpp_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (asset_id)
                asset_id,
                received_at,
                action,
                connection_id
            FROM ocpp_events
            WHERE asset_id = ANY($1::text[])
            ORDER BY asset_id, received_at DESC
            """,
            allowed,
        )

        # Latest meter value timestamp per charger (active sessions)
        mv_rows = await conn.fetch(
            """
            SELECT station_id AS asset_id, MAX(received_at) AS last_mv
            FROM meter_values_parsed
            WHERE station_id = ANY($1::text[])
            GROUP BY station_id
            """,
            allowed,
        )

    # Index both by station_id
    ocpp_latest: dict[str, dict] = {r["asset_id"]: dict(r) for r in ocpp_rows}
    mv_latest:   dict[str, datetime] = {r["asset_id"]: r["last_mv"] for r in mv_rows}

    chargers: list[ConnectivityRecord] = []
    for sid in sorted(allowed):
        ocpp_rec      = ocpp_latest.get(sid)
        last_ocpp     = ocpp_rec["received_at"] if ocpp_rec else None
        last_action   = ocpp_rec["action"]       if ocpp_rec else None
        connection_id = ocpp_rec["connection_id"] if ocpp_rec else None
        last_mv       = mv_latest.get(sid)

        # Use the most recent signal from either source
        candidates = [t for t in (last_ocpp, last_mv) if t is not None]
        if candidates:
            last_seen = max(c if c.tzinfo else c.replace(tzinfo=timezone.utc) for c in candidates)
        else:
            last_seen = None

        # If last_seen came from meter_values (more recent than OCPP), note that
        if last_mv and last_ocpp:
            lmv = last_mv if last_mv.tzinfo else last_mv.replace(tzinfo=timezone.utc)
            lo  = last_ocpp if last_ocpp.tzinfo else last_ocpp.replace(tzinfo=timezone.utc)
            if lmv > lo:
                last_action = "MeterValues"  # override to show actual signal source

        if last_seen:
            mins_ago  = (now_utc - last_seen).total_seconds() / 60.0
            is_online = mins_ago < _OFFLINE_THRESHOLD_MIN
        else:
            mins_ago  = None
            is_online = False

        chargers.append(
            ConnectivityRecord(
                station_id                 = sid,
                evse_name                  = display_name(sid),
                location                   = location_label(sid),
                last_seen_utc              = last_seen,
                last_seen_ak               = _fmt_ak(last_seen),
                last_action                = last_action,
                connection_id              = connection_id,
                minutes_since_last_message = round(mins_ago, 1) if mins_ago is not None else None,
                is_online                  = is_online,
            )
        )

    return ConnectivityResponse(chargers=chargers, as_of_utc=now_utc)


@router.get("/history")
async def get_connectivity_history(
    user: CurrentUser,
    start_date: str = Query(..., description="YYYY-MM-DD Alaska local"),
    end_date:   str = Query(..., description="YYYY-MM-DD Alaska local"),
    station_id: list[str] | None = Query(None),
):
    """
    BootNotification events in the selected date range — each one means the charger
    rebooted or re-established its connection to the network.
    """
    all_ids = get_all_station_ids()
    allowed = filter_evse_ids(all_ids, user.allowed_evse_ids)
    if station_id:
        allowed = [s for s in station_id if s in allowed]

    start_utc = (
        datetime.fromisoformat(f"{start_date}T00:00:00")
        .replace(tzinfo=_AK)
        .astimezone(timezone.utc)
    )
    end_utc = (
        datetime.fromisoformat(f"{end_date}T23:59:59")
        .replace(tzinfo=_AK)
        .astimezone(timezone.utc)
    )

    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                asset_id,
                received_at,
                connector_id,
                connection_id,
                action
            FROM ocpp_events
            WHERE asset_id = ANY($1::text[])
              AND action = 'BootNotification'
              AND received_at >= $2
              AND received_at <= $3
            ORDER BY received_at DESC
            LIMIT 2000
            """,
            allowed, start_utc, end_utc,
        )

    events = []
    for r in rows:
        ts = r["received_at"]
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        events.append({
            "station_id":    r["asset_id"],
            "evse_name":     display_name(r["asset_id"]),
            "location":      location_label(r["asset_id"]),
            "connector_id":  r["connector_id"],
            "connection_id": r["connection_id"],
            "received_at_ak": _fmt_ak(ts),
            "event":         r["action"],
        })

    return {"events": events, "total": len(events)}
