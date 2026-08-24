"""Web Push delivery (VAPID) for the alert service.

Alerts are delivered on three channels: email (SMTP, app/alerts.py), the in-app
SSE banner (app/routers/alerts_sse.py), and Web Push — this module. Push is the
only channel that reaches a phone whose PWA is closed, which is the whole point:
it replaces the alert email without costing a mailbox message in both the Sent
and Inbox folders.

Two hard requirements on iOS (16.4+), both handled on the frontend:
  - the site must be installed to the Home Screen (a Safari tab cannot subscribe)
  - the permission prompt must be triggered by a real user gesture

Dead subscriptions: push services return 404/410 when a subscription is gone for
good (user deleted the Home Screen icon, reinstalled, revoked permission). Those
rows are deleted immediately — retrying them forever is how a push table rots.
"""

from __future__ import annotations

import json
import logging
from typing import Iterable

from .config import (
    PUSH_ENABLED,
    VAPID_PRIVATE_KEY,
    VAPID_PUBLIC_KEY,
    VAPID_SUBJECT,
)

logger = logging.getLogger("rca.push")

# pywebpush is an optional import so the app still boots (with push disabled) on
# an image built before the dependency was added to requirements.txt.
try:
    from pywebpush import WebPushException, webpush  # type: ignore
    _HAVE_PYWEBPUSH = True
except ImportError:  # pragma: no cover
    webpush = None            # type: ignore
    WebPushException = Exception  # type: ignore
    _HAVE_PYWEBPUSH = False
    logger.warning("pywebpush not installed — Web Push delivery is unavailable.")


def push_available() -> bool:
    """True when both the VAPID keys and the pywebpush library are present."""
    return PUSH_ENABLED and _HAVE_PYWEBPUSH


# ── Send to one subscription ──────────────────────────────────────────────────

def _send_one(sub: dict, payload: str) -> tuple[bool, str | None, bool]:
    """Deliver to a single subscription.

    Returns (delivered, error_message, is_gone). `is_gone` is True only for the
    404/410 responses that mean the subscription will never work again.
    """
    subscription_info = {
        "endpoint": sub["endpoint"],
        "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
    }
    try:
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_SUBJECT},
            timeout=10,
            # Apple drops a push whose TTL has expired rather than queueing it
            # forever. 4 hours: long enough to survive a phone that is off or
            # out of service in the Valley, short enough that nobody gets a
            # "charger offline" buzz for something resolved a day ago.
            ttl=14400,
        )
        return True, None, False
    except WebPushException as exc:                     # type: ignore[misc]
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            return False, f"gone ({status})", True
        return False, f"{status or 'error'}: {exc}", False
    except Exception as exc:                            # noqa: BLE001
        return False, str(exc), False


# ── Build the notification payload ────────────────────────────────────────────

def build_payload(
    alert_type: str,
    title: str,
    evse_name: str,
    message: str,
    timestamp_ak: str,
) -> str:
    """JSON body the service worker reads in its `push` handler."""
    return json.dumps({
        "alert_type":   alert_type,
        "title":        title,
        "evse_name":    evse_name,
        "message":      message,
        "timestamp_ak": timestamp_ak,
        # Collapse repeats of the same alert type on the same charger into one
        # notification instead of stacking a wall of them on the lock screen.
        "tag":          f"{alert_type}:{evse_name}",
        "url":          "/app/",
    })


# ── Fan out to a set of subscriptions ─────────────────────────────────────────

def send_to_subscriptions(conn, subs: Iterable[dict], payload: str) -> int:
    """Send `payload` to every subscription, pruning the ones that are gone.

    `conn` is the synchronous psycopg connection owned by the alert thread.
    Returns the number of successful deliveries.
    """
    if not push_available():
        return 0

    # Materialise once: the endpoint list is walked again below for bookkeeping,
    # and a generator would be exhausted by then.
    subs = list(subs)
    delivered = 0
    gone: list[str] = []
    errors: list[tuple[str, str]] = []

    for sub in subs:
        ok, err, is_gone = _send_one(sub, payload)
        if ok:
            delivered += 1
        elif is_gone:
            gone.append(sub["endpoint"])
            logger.info("Pruning dead push subscription for user %s (%s)",
                        sub.get("user_id"), err)
        else:
            errors.append((sub["endpoint"], err or "unknown"))
            logger.error("Push failed for user %s: %s", sub.get("user_id"), err)

    # Prune dead endpoints and record transient errors for troubleshooting.
    try:
        with conn.cursor() as cur:
            if gone:
                cur.execute(
                    "DELETE FROM push_subscriptions WHERE endpoint = ANY(%s)",
                    (gone,),
                )
            for endpoint, err in errors:
                cur.execute(
                    "UPDATE push_subscriptions SET last_error = %s WHERE endpoint = %s",
                    (err[:500], endpoint),
                )
            if delivered:
                cur.execute(
                    """
                    UPDATE push_subscriptions
                       SET last_seen_at = NOW(), last_error = NULL
                     WHERE endpoint = ANY(%s)
                    """,
                    ([s["endpoint"] for s in subs
                      if s["endpoint"] not in gone
                      and s["endpoint"] not in {e for e, _ in errors}],),
                )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error("push_subscriptions bookkeeping failed: %s", exc)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass

    return delivered


# ── Key generation helper ─────────────────────────────────────────────────────

def generate_keys() -> tuple[str, str]:
    """Return (public_key, private_key) as base64url strings for .env.

    Run with:  python -m app.push --generate-keys
    """
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())

    public_bytes = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    private_bytes = key.private_numbers().private_value.to_bytes(32, "big")

    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")  # noqa: E731
    return b64(public_bytes), b64(private_bytes)


if __name__ == "__main__":  # pragma: no cover
    import sys

    if "--generate-keys" in sys.argv:
        pub, priv = generate_keys()
        print("# Add these to .env (and to the VPS .env), then restart the API:")
        print(f"VAPID_PUBLIC_KEY={pub}")
        print(f"VAPID_PRIVATE_KEY={priv}")
        print("VAPID_SUBJECT=mailto:info@rechargealaska.net")
    else:
        print("usage: python -m app.push --generate-keys")
