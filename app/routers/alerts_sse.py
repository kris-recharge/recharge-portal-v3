"""GET /api/alerts/stream — Server-Sent Events for in-browser alert banners.

SCOPING (v3.4). This stream used to be a flat fan-out: every connected client
received every alert, so a tenant scoped to a single charger saw banner toasts
naming other tenants' sites. Two filters now run before an event is queued, and
neither is client-side — an unscoped event never reaches the browser at all:

  1. EVSE scope — always enforced. A client only receives alerts for asset_ids
     in its own allowed_evse_ids (None = unrestricted, i.e. staff).
  2. Alert-type scope — the set of types the user is subscribed to, computed by
     the alert thread and passed in as `subscriber_ids`. Users who set
     portal_users.banner_all_alert_types receive every type instead, still
     limited by rule 1.

Both the user identity and the allowed EVSE list are captured from the
authenticated session at connect time, so a client cannot widen its own scope.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..auth import CurrentUser
from ..db import acquire

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

logger = logging.getLogger("rca.alerts.sse")


@dataclass
class _Client:
    """One connected browser, with the scope it is allowed to see."""
    queue: asyncio.Queue
    user_id: str
    # None = no EVSE restriction (staff). Otherwise the asset_ids this user owns.
    allowed_evse_ids: set[str] | None
    # True = show banners for every alert type, not just subscribed ones.
    all_alert_types: bool = False
    tags: set[str] = field(default_factory=set)


_clients: list[_Client] = []


def broadcast_alert(event: dict, subscriber_ids: set[str] | None = None) -> None:
    """Push an event to the connected clients entitled to see it.

    Called from the alert thread (a non-async context), which is why this uses
    put_nowait against each client's queue rather than awaiting anything.

    `event` must carry "asset_id" so EVSE scope can be applied. `subscriber_ids`
    is the set of user_ids subscribed to this alert type; clients outside it are
    skipped unless they have opted into all-types banners. Passing None means
    "type scope unknown" — the conservative reading is to deliver to nobody
    except all-types clients, so a caller that forgets the argument cannot
    accidentally reopen the unscoped fan-out this module used to have.
    """
    asset_id = event.get("asset_id")
    subscriber_ids = subscriber_ids or set()

    # asset_id is stripped before the event goes out: the banner renders
    # evse_name, and the raw OCPP station id is not something a tenant needs.
    payload = json.dumps({k: v for k, v in event.items() if k != "asset_id"})

    for client in list(_clients):
        # ── 1. EVSE scope (never optional) ────────────────────────────────────
        if client.allowed_evse_ids is not None:
            if asset_id is None or asset_id not in client.allowed_evse_ids:
                continue

        # ── 2. Alert-type scope ───────────────────────────────────────────────
        if not client.all_alert_types and client.user_id not in subscriber_ids:
            continue

        try:
            client.queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("SSE queue full for user %s — dropping alert", client.user_id)


async def _event_stream(client: _Client) -> AsyncGenerator[str, None]:
    try:
        while True:
            try:
                data = await asyncio.wait_for(client.queue.get(), timeout=30.0)
                yield f"data: {data}\n\n"
            except asyncio.TimeoutError:
                # Keepalive ping so the connection doesn't drop
                yield ": ping\n\n"
    finally:
        try:
            _clients.remove(client)
        except ValueError:
            pass


@router.get("/stream")
async def alert_stream(user: CurrentUser):
    # portal_users is keyed by portal_user_id; user_id is the Supabase auth uid.
    row = None
    if user.portal_user_id:
        async with acquire() as conn:
            row = await conn.fetchrow(
                "SELECT banner_all_alert_types FROM portal_users WHERE id = $1::uuid",
                user.portal_user_id,
            )
    all_types = bool(row["banner_all_alert_types"]) if row else False

    client = _Client(
        queue=asyncio.Queue(maxsize=50),
        user_id=user.user_id,
        allowed_evse_ids=(
            None if user.allowed_evse_ids is None else set(user.allowed_evse_ids)
        ),
        all_alert_types=all_types,
    )
    _clients.append(client)

    return StreamingResponse(
        _event_stream(client),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable Nginx/Caddy buffering
        },
    )
