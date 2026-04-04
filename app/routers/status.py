"""GET /api/status — StatusNotification history."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from ..auth import CurrentUser, filter_evse_ids
from ..constants import display_name, get_all_station_ids
from ..db import acquire
from ..models import StatusEvent, StatusHistoryResponse

router = APIRouter(prefix="/api/status", tags=["status"])

_AK = ZoneInfo("America/Anchorage")


def _fmt_ak(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_AK).strftime("%Y-%m-%d %H:%M:%S")


@router.get("", response_model=StatusHistoryResponse)
async def get_status_history(
    user: CurrentUser,
    limit: int = Query(500, ge=1, le=2000),
    station_id: list[str] | None = Query(None),
    include_no_error: bool = Query(False, description="Include NoError rows (default: faults only)"),
):
    all_ids = get_all_station_ids()
    allowed = filter_evse_ids(all_ids, user.allowed_evse_ids)
    if station_id:
        allowed = [s for s in station_id if s in allowed]

    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                e.id,
                e.asset_id                                          AS station_id,
                e.received_at,
                e.connector_id,
                (e.action_payload->>'status')                       AS status,
                (e.action_payload->>'errorCode')                    AS error_code,
                (e.action_payload->>'vendorErrorCode')              AS vendor_error_code,
                CASE
                    WHEN (e.action_payload->>'vendorErrorCode') ~ '^\d+$'
                    THEN (SELECT t.description FROM tritium_error_codes t
                          WHERE t.code = (e.action_payload->>'vendorErrorCode')::integer
                          LIMIT 1)
                    ELSE NULL
                END                                                 AS vendor_error_description,
                COUNT(*) OVER()                                     AS total_count
            FROM ocpp_events e
            WHERE e.action = 'StatusNotification'
              AND e.asset_id = ANY($1::text[])
              AND ($2 OR (e.action_payload->>'errorCode') != 'NoError')
            ORDER BY e.received_at DESC
            LIMIT $3
            """,
            allowed,
            include_no_error,
            limit,
        )

    total = int(rows[0]["total_count"]) if rows else 0

    events = [
        StatusEvent(
            id                        = r["id"],
            station_id                = r["station_id"],
            evse_name                 = display_name(r["station_id"]),
            connector_id              = r["connector_id"],
            status                    = r["status"] or "",
            error_code                = r["error_code"],
            vendor_error_code         = r["vendor_error_code"],
            vendor_error_description  = r["vendor_error_description"],
            received_at               = r["received_at"],
            received_at_ak            = _fmt_ak(r["received_at"]),
        )
        for r in rows
    ]

    return StatusHistoryResponse(events=events, total=total)
