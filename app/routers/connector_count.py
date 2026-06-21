"""Connector Count — lifetime plug-in odometer per cord, with cord-change reset.

GET  /api/connector-count                                 — one card per charger
POST /api/connector-count/{station_id}/{connector_id}/reset — log a cord swap

Reads the rolling sums maintained by ``app.connector_counts`` (the alerts poll
loop). Displayed count for the *current* cord is
``baseline_attempts + (attempts − cord_start_attempts)`` so a replacement just
moves ``cord_start_attempts`` to the live odometer reading — the monotonic
``attempts`` counter is never zeroed, preserving how long the retired cord lasted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Path, status

from ..auth import CurrentUser, PortalUser, filter_evse_ids
from ..config import DEV_BYPASS_AUTH
from ..constants import (
    connector_type_for,
    display_name,
    get_all_station_ids,
    location_label,
    manufacturer_for,
)
from ..db import acquire
from ..models import (
    ConnectorCount,
    ConnectorCountCard,
    ConnectorCountResponse,
    CordResetRequest,
)

router = APIRouter(prefix="/api/connector-count", tags=["connector-count"])

_AK = ZoneInfo("America/Anchorage")
ADMIN_EMAIL = "kris.hall@rechargealaska.net"

# Every charger in the fleet has these two physical cords.
_CONNECTORS = (1, 2)


def _fmt_ak(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_AK).strftime("%Y-%m-%d %H:%M")


def _can_reset(user: PortalUser) -> bool:
    """Admin or any user granted can_submit_pm may log a cord change."""
    return DEV_BYPASS_AUTH or user.email == ADMIN_EMAIL or user.can_submit_pm


@router.get("", response_model=ConnectorCountResponse)
async def get_connector_count(user: CurrentUser):
    all_ids = get_all_station_ids()
    allowed = filter_evse_ids(all_ids, user.allowed_evse_ids)

    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT station_id,
                   connector_id,
                   baseline_attempts + (attempts - cord_start_attempts) AS total
            FROM connector_counts
            WHERE station_id = ANY($1::text[])
            """,
            allowed,
        )
        # Most-recent cord change per (station, connector) for the "previous cord" line.
        change_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (station_id, connector_id)
                   station_id, connector_id, prev_cord_attempts, changed_at
            FROM connector_cord_changes
            WHERE station_id = ANY($1::text[])
            ORDER BY station_id, connector_id, changed_at DESC
            """,
            allowed,
        )

    totals = {(r["station_id"], r["connector_id"]): int(r["total"]) for r in rows}
    last_change = {
        (r["station_id"], r["connector_id"]): r for r in change_rows
    }

    chargers: list[ConnectorCountCard] = []
    for sid in sorted(allowed, key=display_name):
        connectors = []
        for cid in _CONNECTORS:
            ch = last_change.get((sid, cid))
            connectors.append(
                ConnectorCount(
                    connector_id=cid,
                    connector_type=connector_type_for(sid, cid),
                    attempts=totals.get((sid, cid), 0),
                    previous_cord_attempts=int(ch["prev_cord_attempts"]) if ch else None,
                    last_reset_ak=_fmt_ak(ch["changed_at"]) if ch else None,
                )
            )
        chargers.append(
            ConnectorCountCard(
                station_id=sid,
                evse_name=display_name(sid),
                manufacturer=manufacturer_for(sid),
                location=location_label(sid),
                connectors=connectors,
            )
        )

    return ConnectorCountResponse(chargers=chargers, as_of_utc=datetime.now(timezone.utc))


@router.post("/{station_id}/{connector_id}/reset", response_model=ConnectorCount)
async def reset_connector_cord(
    user: CurrentUser,
    body: CordResetRequest,
    station_id: str = Path(...),
    connector_id: int = Path(..., ge=1, le=2),
):
    """Record a cord replacement: log the retired cord's lifetime and restart at 0.

    Non-destructive — moves ``cord_start_attempts`` to the live odometer and
    clears the manufacturer baseline. Open to admin + PM submitters, scoped to
    the caller's allowed EVSEs.
    """
    if not _can_reset(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "You do not have permission to reset connector counts")

    note = (body.note or "").strip()
    if not note:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A note is required to log a cord change")

    allowed = filter_evse_ids(get_all_station_ids(), user.allowed_evse_ids)
    if station_id not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Charger not found or not permitted")

    async with acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT attempts, baseline_attempts, cord_start_attempts
                FROM connector_counts
                WHERE station_id = $1 AND connector_id = $2
                FOR UPDATE
                """,
                station_id, connector_id,
            )
            attempts            = int(row["attempts"]) if row else 0
            baseline            = int(row["baseline_attempts"]) if row else 0
            cord_start          = int(row["cord_start_attempts"]) if row else 0
            prev_cord_attempts  = baseline + (attempts - cord_start)

            await conn.execute(
                """
                INSERT INTO connector_cord_changes
                    (station_id, connector_id, odometer_attempts, prev_cord_attempts,
                     changed_by, source, note)
                VALUES ($1, $2, $3, $4, $5, 'manual', $6)
                """,
                station_id, connector_id, attempts, prev_cord_attempts,
                user.email, note,
            )
            # Upsert so a connector with no counted events yet can still be reset.
            await conn.execute(
                """
                INSERT INTO connector_counts
                    (station_id, connector_id, attempts, baseline_attempts,
                     cord_start_attempts, updated_at)
                VALUES ($1, $2, $3, 0, $3, now())
                ON CONFLICT (station_id, connector_id) DO UPDATE
                    SET cord_start_attempts = connector_counts.attempts,
                        baseline_attempts   = 0,
                        updated_at          = now()
                """,
                station_id, connector_id, attempts,
            )

    return ConnectorCount(
        connector_id=connector_id,
        connector_type=connector_type_for(station_id, connector_id),
        attempts=0,
        previous_cord_attempts=prev_cord_attempts,
        last_reset_ak=_fmt_ak(datetime.now(timezone.utc)),
    )
