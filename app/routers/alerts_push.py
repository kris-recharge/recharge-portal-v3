"""Web Push device registration.

POST   /api/alerts/push/subscribe    — register this browser/device
POST   /api/alerts/push/unsubscribe  — drop this browser/device
POST   /api/alerts/push/test         — send a test notification to the caller

A "subscription" here is a device, not an alert preference: which alert types
actually reach it is decided by alert_subscriptions.push_enabled (see
routers/alerts_config.py). Registering a device on its own delivers nothing.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from ..auth import CurrentUser
from ..db import acquire
from ..models import PushSubscribeRequest
from ..push import build_payload, push_available, send_to_subscriptions

router = APIRouter(prefix="/api/alerts/push", tags=["alerts"])

logger = logging.getLogger("rca.alerts.push")


@router.post("/subscribe")
async def subscribe(user: CurrentUser, body: PushSubscribeRequest, request: Request):
    """Store (or refresh) the push subscription for the calling device.

    The endpoint URL is unique per device+origin, so ON CONFLICT makes a repeat
    subscribe idempotent. It is also re-pointed at the current user: if someone
    signs out and a colleague signs in on the same iPad, the device must follow
    the new login rather than keep pushing the previous user's alerts.
    """
    if not push_available():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Web Push is not configured on this server (VAPID keys missing).",
        )

    user_agent = body.user_agent or request.headers.get("user-agent", "")

    async with acquire() as conn:
        await conn.execute(
            """
            INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent)
            VALUES ($1::uuid, $2, $3, $4, $5)
            ON CONFLICT (endpoint) DO UPDATE
                SET user_id      = EXCLUDED.user_id,
                    p256dh       = EXCLUDED.p256dh,
                    auth         = EXCLUDED.auth,
                    user_agent   = EXCLUDED.user_agent,
                    last_seen_at = NOW(),
                    last_error   = NULL
            """,
            user.user_id,
            body.endpoint,
            body.p256dh,
            body.auth,
            user_agent[:400],
        )

    logger.info("Push device registered for %s", user.email)
    return {"ok": True}


@router.post("/unsubscribe")
async def unsubscribe(user: CurrentUser, body: PushSubscribeRequest):
    """Remove this device's subscription.

    Scoped to the calling user so one account cannot delete another's device by
    guessing an endpoint.
    """
    async with acquire() as conn:
        await conn.execute(
            "DELETE FROM push_subscriptions WHERE user_id = $1::uuid AND endpoint = $2",
            user.user_id,
            body.endpoint,
        )
    return {"ok": True}


@router.post("/test")
async def send_test(user: CurrentUser):
    """Fire a test notification at every device registered to the caller.

    Uses the same delivery path as a real alert, so a success here means the
    VAPID keys, the service worker, and the OS permission grant all line up.
    """
    if not push_available():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Web Push is not configured on this server (VAPID keys missing).",
        )

    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT user_id::text, endpoint, p256dh, auth
            FROM push_subscriptions
            WHERE user_id = $1::uuid
            """,
            user.user_id,
        )

    if not rows:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No push devices registered. Enable notifications on this device first.",
        )

    subs = [
        {"user_id": r["user_id"], "endpoint": r["endpoint"],
         "p256dh": r["p256dh"], "auth": r["auth"]}
        for r in rows
    ]
    payload = build_payload(
        alert_type="test",
        title="✅ ReCharge Alaska — Test Notification",
        evse_name="Test",
        message="Push notifications are working. Alerts will arrive here.",
        timestamp_ak="",
    )

    # send_to_subscriptions needs the alert thread's *sync* connection to prune
    # dead rows, so borrow one for the duration of this request.
    from ..db import get_conn_sync  # noqa: PLC0415

    def _deliver() -> int:
        conn_sync = get_conn_sync()
        try:
            return send_to_subscriptions(conn_sync, subs, payload)
        finally:
            conn_sync.close()

    # webpush() is blocking network I/O — off the event loop it goes.
    delivered = await run_in_threadpool(_deliver)

    return {"ok": delivered > 0, "delivered": delivered, "devices": len(subs)}
