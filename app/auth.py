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

from fastapi import Cookie, Depends, HTTPException, status

from .config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, DEV_BYPASS_AUTH


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PortalUser:
    email: str
    user_id: str
    allowed_evse_ids: list[str] | None  # None = no restriction


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

def _supabase_get_user(access_token: str) -> dict | None:
    """Call Supabase /auth/v1/user to validate the token and get user info."""
    url = f"{SUPABASE_URL.rstrip('/')}/auth/v1/user"
    req = urllib.request.Request(url, method="GET")
    req.add_header("apikey", SUPABASE_SERVICE_ROLE_KEY)
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _fetch_allowed_evse(email: str) -> list[str] | None:
    """Look up portal_users.allowed_evse_ids for this email."""
    url = (
        f"{SUPABASE_URL.rstrip('/')}/rest/v1/portal_users"
        f"?select=allowed_evse_ids&email=eq.{urllib.parse.quote(email)}&limit=1"
    )
    req = urllib.request.Request(url, method="GET")
    req.add_header("apikey", SUPABASE_SERVICE_ROLE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_SERVICE_ROLE_KEY}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        if not data:
            return []
        val = data[0].get("allowed_evse_ids")
        if val is None:
            return None  # NULL = no restriction
        if isinstance(val, list):
            return [str(v) for v in val if v]
        return []
    except Exception:
        return None


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

async def get_current_user(cookie: Annotated[str | None, Cookie(alias="cookie")] = None) -> PortalUser:
    """FastAPI dependency — resolves the authenticated user or raises 401."""

    # DEV: bypass auth entirely for local review
    if DEV_BYPASS_AUTH:
        return PortalUser(email="kris.hall@rechargealaska.net", user_id="37553d35-318b-4587-ac86-2ee346b9c4ca", allowed_evse_ids=None)

    if not cookie:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    # Parse the Cookie header into a dict
    cookies: dict[str, str] = {}
    for part in cookie.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        cookies[k.strip()] = v.strip()

    raw_cookie = _find_supabase_cookie(cookies)
    if not raw_cookie:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No auth cookie")

    access_token = _extract_access_token_from_cookie(raw_cookie)
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed auth cookie")

    # Check in-memory cache
    ck = _cache_key(access_token)
    cached = _cache.get(ck)
    if cached:
        user, expires = cached
        if time.monotonic() < expires:
            return user

    # Validate with Supabase
    user_data = _supabase_get_user(access_token)
    if not user_data or not user_data.get("email"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")

    email   = user_data["email"]
    user_id = user_data.get("id", "")
    allowed = _fetch_allowed_evse(email)

    portal_user = PortalUser(email=email, user_id=user_id, allowed_evse_ids=allowed)
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
