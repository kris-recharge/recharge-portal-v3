"""GET /api/alerts/stream — Server-Sent Events for in-browser alert notifications."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ..auth import CurrentUser

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

# Simple broadcast queue — alerts.py puts events here, SSE clients consume them
_subscribers: list[asyncio.Queue] = []


def broadcast_alert(event: dict) -> None:
    """Called by the alert thread to push an event to all SSE subscribers."""
    payload = json.dumps(event)
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


async def _event_stream(queue: asyncio.Queue) -> AsyncGenerator[str, None]:
    try:
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield f"data: {data}\n\n"
            except asyncio.TimeoutError:
                # Keepalive ping so the connection doesn't drop
                yield ": ping\n\n"
    finally:
        _subscribers.remove(queue)


@router.get("/stream")
async def alert_stream(user: CurrentUser):
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    _subscribers.append(queue)
    return StreamingResponse(
        _event_stream(queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable Nginx/Caddy buffering
        },
    )
