"""GET/PUT /api/alerts/subscriptions  — per-user alert opt-in preferences.
   GET     /api/alerts/history       — fired alerts for the current user.
"""

from __future__ import annotations

from datetime import timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from fastapi import Request

from ..auth import CurrentUser
from ..config import VAPID_PUBLIC_KEY
from ..db import acquire
from ..models import (
    ALERT_TYPES,
    AlertHistoryResponse,
    AlertSubscription,
    AlertSubscriptionsResponse,
    BannerScopeRequest,
    FiredAlert,
    PushDevice,
)
from ..push import push_available

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
async def get_subscriptions(user: CurrentUser, request: Request = None):  # noqa: B008
    """Return the current user's alert subscriptions, channels, and push devices."""
    current_ua = request.headers.get("user-agent", "") if request is not None else ""

    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT alert_type, enabled, email_enabled, push_enabled
            FROM alert_subscriptions
            WHERE user_id = $1::uuid
            """,
            user.user_id,
        )
        devices = await conn.fetch(
            """
            SELECT id::text, user_agent, created_at, last_seen_at
            FROM push_subscriptions
            WHERE user_id = $1::uuid
            ORDER BY created_at
            """,
            user.user_id,
        )
        # portal_users is keyed by portal_user_id, NOT the auth uid.
        prefs = await conn.fetchrow(
            "SELECT banner_all_alert_types FROM portal_users WHERE id = $1::uuid",
            user.portal_user_id,
        ) if user.portal_user_id else None

    by_type = {r["alert_type"]: r for r in rows}

    subscriptions = [
        AlertSubscription(
            alert_type=at,
            # default: opt-in (off). email_enabled defaults true so that turning
            # a brand-new subscription on behaves the way it always has.
            enabled=bool(by_type[at]["enabled"]) if at in by_type else False,
            email_enabled=bool(by_type[at]["email_enabled"]) if at in by_type else True,
            push_enabled=bool(by_type[at]["push_enabled"]) if at in by_type else False,
        )
        for at in ALERT_TYPES
    ]

    return AlertSubscriptionsResponse(
        email=user.email,
        subscriptions=subscriptions,
        push_supported=push_available(),
        vapid_public_key=VAPID_PUBLIC_KEY,
        push_devices=[
            PushDevice(
                id=d["id"],
                user_agent=d["user_agent"],
                created_at_ak=_fmt_ak(d["created_at"]),
                last_seen_at_ak=_fmt_ak(d["last_seen_at"]),
                is_current=bool(current_ua) and d["user_agent"] == current_ua,
            )
            for d in devices
        ],
        banner_all_alert_types=bool(prefs["banner_all_alert_types"]) if prefs else True,
    )


# ── PUT subscriptions ─────────────────────────────────────────────────────────

@router.post("/subscriptions", response_model=AlertSubscriptionsResponse)
async def update_subscriptions(
    user: CurrentUser,
    body: list[AlertSubscription],
    request: Request = None,  # noqa: B008
):
    """Upsert all 4 alert type preferences for the current user."""
    async with acquire() as conn:
        for sub in body:
            if sub.alert_type not in ALERT_TYPES:
                continue
            await conn.execute(
                """
                INSERT INTO alert_subscriptions
                    (user_id, alert_type, enabled, email_enabled, push_enabled, updated_at)
                VALUES ($1::uuid, $2, $3, $4, $5, NOW())
                ON CONFLICT (user_id, alert_type) DO UPDATE
                    SET enabled       = EXCLUDED.enabled,
                        email_enabled = EXCLUDED.email_enabled,
                        push_enabled  = EXCLUDED.push_enabled,
                        updated_at    = NOW()
                """,
                user.user_id,
                sub.alert_type,
                sub.enabled,
                sub.email_enabled,
                sub.push_enabled,
            )

    # Return the updated state
    return await get_subscriptions(user, request)


# ── Banner scope ──────────────────────────────────────────────────────────────

@router.post("/banner-scope", response_model=AlertSubscriptionsResponse)
async def set_banner_scope(
    user: CurrentUser,
    body: BannerScopeRequest,
    request: Request = None,  # noqa: B008
):
    """Widen or narrow which alert *types* raise an in-app banner.

    EVSE scope is never affected by this — a user always and only sees banners
    for chargers in their own allowed_evse_ids, enforced in the SSE router.
    """
    async with acquire() as conn:
        await conn.execute(
            "UPDATE portal_users SET banner_all_alert_types = $2 WHERE id = $1::uuid",
            user.portal_user_id,
            body.banner_all_alert_types,
        )
    return await get_subscriptions(user, request)


# ── GET history ───────────────────────────────────────────────────────────────

@router.get("/history", response_model=AlertHistoryResponse)
async def get_alert_history(user: CurrentUser):
    """
    Return fired alerts from the last 15 days that:
    - Are for an EVSE the user is allowed to see (always enforced)
    - Match an alert type the user should see in-app

    The type filter follows the same rule as the banner: by default a logged-in
    user sees EVERY alert type on their own chargers, because a toast you can't
    find again in History is a dead end. Only a user who turned
    banner_all_alert_types off is narrowed to their subscribed types.
    """
    allowed = user.allowed_evse_ids  # None = all EVSEs

    async with acquire() as conn:
        prefs = await conn.fetchrow(
            "SELECT banner_all_alert_types FROM portal_users WHERE id = $1::uuid",
            user.portal_user_id,
        ) if user.portal_user_id else None
        all_types = bool(prefs["banner_all_alert_types"]) if prefs else True

        # Build the WHERE clause incrementally so the $n placeholders stay
        # contiguous — Postgres rejects a query that uses $2 without $1, which
        # is exactly what happens if an optional clause is simply omitted.
        where = ["fa.fired_at >= NOW() - INTERVAL '15 days'"]
        args: list = []

        if allowed is not None:
            args.append(allowed)
            where.append(f"fa.asset_id = ANY(${len(args)}::text[])")

        if not all_types:
            args.append(user.user_id)
            where.append(
                f"""EXISTS (
                      SELECT 1 FROM alert_subscriptions asub
                      WHERE asub.user_id    = ${len(args)}::uuid
                        AND asub.alert_type = fa.alert_type
                        AND asub.enabled    = true
                  )"""
            )

        rows = await conn.fetch(
            f"""
            SELECT fa.id::text, fa.fired_at, fa.alert_type, fa.evse_name, fa.message
            FROM fired_alerts fa
            WHERE {' AND '.join(where)}
            ORDER BY fa.fired_at DESC
            LIMIT 500
            """,
            *args,
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
