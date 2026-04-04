"""GET/PUT /api/alerts/subscriptions  — per-user alert opt-in preferences.
   GET     /api/alerts/history       — fired alerts for the current user.
"""

from __future__ import annotations

from datetime import timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from ..auth import CurrentUser
from ..db import acquire
from ..models import ALERT_TYPES, AlertHistoryResponse, AlertSubscription, AlertSubscriptionsResponse, FiredAlert

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

_AK = ZoneInfo("America/Anchorage")


def _fmt_ak(dt) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_AK).strftime("%Y-%m-%d %H:%M AKT")


# ── GET subscriptions ─────────────────────────────────────────────────────────

@router.get("/subscriptions", response_model=AlertSubscriptionsResponse)
async def get_subscriptions(user: CurrentUser):
    """Return the current user's alert subscription state for all 4 alert types."""
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT alert_type, enabled
            FROM alert_subscriptions
            WHERE user_id = $1::uuid
            """,
            user.user_id,
        )

    enabled_map = {r["alert_type"]: r["enabled"] for r in rows}

    subscriptions = [
        AlertSubscription(
            alert_type=at,
            enabled=enabled_map.get(at, False),   # default: opt-in (off)
        )
        for at in ALERT_TYPES
    ]

    return AlertSubscriptionsResponse(
        email=user.email,
        subscriptions=subscriptions,
    )


# ── PUT subscriptions ─────────────────────────────────────────────────────────

@router.post("/subscriptions", response_model=AlertSubscriptionsResponse)
async def update_subscriptions(
    user: CurrentUser,
    body: list[AlertSubscription],
):
    """Upsert all 4 alert type preferences for the current user."""
    async with acquire() as conn:
        for sub in body:
            if sub.alert_type not in ALERT_TYPES:
                continue
            await conn.execute(
                """
                INSERT INTO alert_subscriptions (user_id, alert_type, enabled, updated_at)
                VALUES ($1::uuid, $2, $3, NOW())
                ON CONFLICT (user_id, alert_type) DO UPDATE
                    SET enabled    = EXCLUDED.enabled,
                        updated_at = NOW()
                """,
                user.user_id,
                sub.alert_type,
                sub.enabled,
            )

    # Return the updated state
    return await get_subscriptions(user)


# ── GET history ───────────────────────────────────────────────────────────────

@router.get("/history", response_model=AlertHistoryResponse)
async def get_alert_history(user: CurrentUser):
    """
    Return fired alerts from the last 15 days that:
    - Are for an EVSE the user is allowed to see
    - Match an alert type the user is currently subscribed to
    """
    allowed = user.allowed_evse_ids  # None = all EVSEs

    async with acquire() as conn:
        if allowed is None:
            # User has access to all EVSEs — just filter by subscriptions
            rows = await conn.fetch(
                """
                SELECT fa.id::text, fa.fired_at, fa.alert_type, fa.evse_name, fa.message
                FROM fired_alerts fa
                WHERE fa.fired_at >= NOW() - INTERVAL '15 days'
                  AND EXISTS (
                      SELECT 1 FROM alert_subscriptions asub
                      WHERE asub.user_id   = $1::uuid
                        AND asub.alert_type = fa.alert_type
                        AND asub.enabled    = true
                  )
                ORDER BY fa.fired_at DESC
                LIMIT 500
                """,
                user.user_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT fa.id::text, fa.fired_at, fa.alert_type, fa.evse_name, fa.message
                FROM fired_alerts fa
                WHERE fa.fired_at >= NOW() - INTERVAL '15 days'
                  AND fa.asset_id = ANY($2::text[])
                  AND EXISTS (
                      SELECT 1 FROM alert_subscriptions asub
                      WHERE asub.user_id   = $1::uuid
                        AND asub.alert_type = fa.alert_type
                        AND asub.enabled    = true
                  )
                ORDER BY fa.fired_at DESC
                LIMIT 500
                """,
                user.user_id,
                allowed,
            )

    alerts = [
        FiredAlert(
            id=r["id"],
            fired_at_ak=_fmt_ak(r["fired_at"]),
            alert_type=r["alert_type"],
            evse_name=r["evse_name"],
            message=r["message"],
        )
        for r in rows
    ]

    return AlertHistoryResponse(alerts=alerts)
