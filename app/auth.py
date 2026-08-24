"""FastAPI authentication helpers.

Strategy (same access model as v2, no new roles):
- Browser sends the Supabase sb-*-auth-token cookie.
- FastAPI decodes the JWT locally (using the service role key as the JWKS source
  is overkill — Supabase JWTs are HS256 signed with the JWT secret, which we can
  verify with the service role key's embedded iat/exp, but the simplest and most
  reliable approach is to validate via the Supabase REST /auth/v1/user endpoint).
- On success, we extract email + look up portal_users.allowed_evse_ids.
- allowed_evse_ids NULL → no restriction (full access).
- allowed_evse_ids []   → deny.

For the initial scaffold we validate the Supabase JWT by calling the Supabase
auth endpoint, then cache the result in a short-lived in-memory dict so
repeated requests on the same tab don't each make a round trip.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Header, status

from .config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, DEV_BYPASS_AUTH


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PortalUser:
    email: str
    # Supabase auth uid. This is what alert_subscriptions.user_id and
    # push_subscriptions.user_id are keyed by.
    user_id: str
    allowed_evse_ids: list[str] | None  # None = no restriction
    name: str = ""
    can_submit_pm: bool = False  # may log PMs/repairs on their own units
    # portal_users.id — a DIFFERENT key from user_id above. The two were
    # conflated before v3.4, which silently broke every alert_subscriptions →
    # portal_users join: rows are written with the auth uid but were being
    # looked up by portal_users.id, so opt-ins delivered nothing. Anything
    # touching the portal_users table must use this field, never user_id.
    portal_user_id: str = ""


# ── Token cache (avoids a Supabase round-trip on every API call) ──────────────

_cache: dict[str, tuple[PortalUser, float]] = {}
_CACHE_TTL = 300.0  # 5 min


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


# ── JWT decode (local, no signature verification — Supabase validates upstream) ──

def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        return json.loads(_b64url_decode(parts[1]).decode("utf-8", errors="replace"))
    except Exception:
        return {}


# ── Supabase REST helpers ─────────────────────────────────────────────────────

class _SupabaseUnreachable(Exception):
    """Raised when token validation failed for infrastructure reasons
    (timeout, DNS, 5xx) rather than because the token is invalid."""


def _supabase_get_user(access_token: str) -> dict | None:
    """Call Supabase /auth/v1/user to validate the token and get user info.

    Returns None only when Supabase definitively rejects the token (4xx).
    Raises _SupabaseUnreachable on network errors / 5xx so callers don't
    treat a transient outage as an expired session.
    """
    url = f"{SUPABASE_URL.rstrip('/')}/auth/v1/user"
    req = urllib.request.Request(url, method="GET")
    req.add_header("apikey", SUPABASE_SERVICE_ROLE_KEY)
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            return None  # token genuinely invalid/expired
        raise _SupabaseUnreachable(f"Supabase auth returned {e.code}")
    except Exception as e:
        raise _SupabaseUnreachable(str(e))


def _fetch_portal_user(email: str) -> dict:
    """Look up the portal_users row for this email.

    Uses ilike (case-insensitive) so mixed-case email in portal_users still
    matches the lowercase email returned by Supabase auth (e.g. Mark vs mark).

    Returns a dict: {"allowed_evse_ids", "can_submit_pm", "name"} where
      allowed_evse_ids: None → NULL row value (no restriction / admin)
                        [...] → explicit allowlist
                        []    → authenticated but no matching row → DENY all

    Raises _SupabaseUnreachable on network / 5xx errors. This is deliberate:
    the lookup must FAIL CLOSED. A transient lookup failure must never silently
    grant a restricted user access to every EVSE. Callers serve a stale cache
    entry or return 503 instead of granting unrestricted access.
    """
    url = (
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/portal_users"
        f"?select=id,allowed_evse_ids,can_submit_pm,name"
        f"&email=ilike.{urllib.parse.quote(email.lower())}&limit=1"
    )
    req = urllib.request.Request(url, method="GET")
    req.add_header("apikey", SUPABASE_SERVICE_ROLE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_SERVICE_ROLE_KEY}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            # PostgREST rejected the request — treat as "no provisioning" → deny.
            return {"allowed_evse_ids": [], "can_submit_pm": False, "name": "", "id": ""}
        raise _SupabaseUnreachable(f"portal_users lookup returned {e.code}")
    except Exception as e:
        raise _SupabaseUnreachable(str(e))

    if not data:
        return {"allowed_evse_ids": [], "can_submit_pm": False, "name": "", "id": ""}

    row = data[0]
    val = row.get("allowed_evse_ids")
    if val is None:
        allowed: list[str] | None = None  # NULL = no restriction
    elif isinstance(val, list):
        allowed = [str(v) for v in val if v]
    else:
        allowed = []
    return {
        "allowed_evse_ids": allowed,
        "can_submit_pm": bool(row.get("can_submit_pm")),
        "name": row.get("name") or "",
        "id": str(row.get("id") or ""),
    }


# ── Cookie extraction ─────────────────────────────────────────────────────────

def _extract_access_token_from_cookie(raw: str) -> str | None:
    """Parse sb-*-auth-token cookie value to find the access_token JWT."""
    val = urllib.parse.unquote(raw)

    # Format 1: plain JWT (three dot-separated parts)
    if val.count(".") >= 2:
        return val

    # Format 2: base64- prefix
    if val.startswith("base64-"):
        val = val[len("base64-"):]

    # Format 3: base64-encoded JSON {"access_token": "..."}
    for candidate in (val, val.replace("-", "+").replace("_", "/")):
        try:
            pad = "=" * (-len(candidate) % 4)
            decoded = base64.b64decode(candidate + pad)
            data = json.loads(decoded.decode("utf-8", errors="replace"))
            at = data.get("access_token") or data.get("accessToken")
            if at and isinstance(at, str):
                return at
        except Exception:
            continue

    return None


def _find_supabase_cookie(cookies: dict[str, str]) -> str | None:
    """Return the first sb-*-auth-token or supabase-auth-token value found."""
    import re
    for name, value in cookies.items():
        if re.match(r"^sb-[A-Za-z0-9_-]+-auth-token$", name):
            return value
    return cookies.get("supabase-auth-token")


# ── FastAPI dependency ────────────────────────────────────────────────────────

async def get_current_user(
    authorization: Annotated[str | None, Header(alias="authorization")] = None,
    cookie: Annotated[str | None, Cookie(alias="cookie")] = None,
) -> PortalUser:
    """FastAPI dependency — resolves the authenticated user or raises 401.

    Accepts auth via (in priority order):
      1. Authorization: Bearer <jwt>  — preferred (sent by React frontend)
      2. sb-*-auth-token cookie       — fallback for browsers that set it
    """

    # DEV: bypass auth entirely for local review
    if DEV_BYPASS_AUTH:
        return PortalUser(
            email="kris.hall@rechargealaska.net",
            user_id="a3377629-be5f-4180-b0ba-d96c4c4bad15",          # auth uid
            allowed_evse_ids=None,
            name="Kris Hall",
            can_submit_pm=True,
            portal_user_id="37553d35-318b-4587-ac86-2ee346b9c4ca",   # portal_users.id
        )

    access_token: str | None = None

    # 1. Try Authorization: Bearer header (set by api.ts via Supabase localStorage session)
    if authorization and authorization.startswith("Bearer "):
        access_token = authorization[7:].strip() or None

    # 2. Fall back to Supabase cookie (sb-*-auth-token)
    if not access_token and cookie:
        cookies: dict[str, str] = {}
        for part in cookie.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
        raw_cookie = _find_supabase_cookie(cookies)
        if raw_cookie:
            access_token = _extract_access_token_from_cookie(raw_cookie)

    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    # Check in-memory cache
    ck = _cache_key(access_token)
    cached = _cache.get(ck)
    if cached:
        user, expires = cached
        if time.monotonic() < expires:
            return user

    # Validate with Supabase
    try:
        user_data = _supabase_get_user(access_token)
    except _SupabaseUnreachable:
        # Supabase is down/slow — not the user's fault. Serve a stale cache
        # entry if we have one; otherwise surface 503 so the frontend does
        # NOT treat this as an expired session and log the user out.
        if cached:
            return cached[0]
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth service temporarily unavailable",
        )
    if not user_data or not user_data.get("email"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")

    email   = user_data["email"]
    user_id = user_data.get("id", "")
    try:
        perms = _fetch_portal_user(email)
    except _SupabaseUnreachable:
        # Allowlist lookup failed for infrastructure reasons. Do NOT fail open —
        # serve the last known-good cached user if we have one, otherwise 503 so
        # the request is rejected rather than granted unrestricted EVSE access.
        if cached:
            return cached[0]
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authorization service temporarily unavailable",
        )

    portal_user = PortalUser(
        email=email,
        user_id=user_id,
        allowed_evse_ids=perms["allowed_evse_ids"],
        name=perms["name"],
        can_submit_pm=perms["can_submit_pm"],
        portal_user_id=perms.get("id", ""),
    )
    _cache[ck] = (portal_user, time.monotonic() + _CACHE_TTL)
    return portal_user


def filter_evse_ids(all_ids: list[str], allowed: list[str] | None) -> list[str]:
    """Apply EVSE allowlist. None = no restriction. [] = deny all."""
    if allowed is None:
        return all_ids
    if not allowed:
        return []
    if "__ALL__" in allowed:
        return all_ids
    allowed_set = set(allowed)
    return [x for x in all_ids if x in allowed_set]


# Convenience type alias for route injection
CurrentUser = Annotated[PortalUser, Depends(get_current_user)]
