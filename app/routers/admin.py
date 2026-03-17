"""Admin-only API — user management, EVSE registry, pricing.

Only accessible to kris.hall@rechargealaska.net (or dev bypass).
"""

from __future__ import annotations

import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..auth import CurrentUser, PortalUser
from ..config import DEV_BYPASS_AUTH
from ..constants import (
    get_evse_display,
    get_evse_location,
    get_platform_map,
    get_all_station_ids,
    get_archived_station_ids,
)
from ..db import acquire

router = APIRouter(prefix="/api/admin", tags=["admin"])

ADMIN_EMAIL = "kris.hall@rechargealaska.net"
_OVR_PATH   = Path(__file__).parent.parent / "runtime_overrides.json"
_AK         = ZoneInfo("America/Anchorage")


# ── Admin guard ───────────────────────────────────────────────────────────────

async def _require_admin(user: CurrentUser) -> PortalUser:
    if not DEV_BYPASS_AUTH and user.email != ADMIN_EMAIL:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access only")
    return user

AdminUser = Annotated[PortalUser, Depends(_require_admin)]


# ── Override file helpers ─────────────────────────────────────────────────────

def _read_overrides() -> dict:
    try:
        return json.loads(_OVR_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_overrides(obj: dict) -> None:
    _OVR_PATH.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        _OVR_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    except Exception:
        pass


# ── Pydantic models ───────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    email: str
    name: str = ""
    allowed_evse_ids: list[str] | None = None
    active: bool = True


class UserPatch(BaseModel):
    email: str | None = None
    name: str | None = None
    allowed_evse_ids: list[str] | None = None
    active: bool | None = None


class PricingCreate(BaseModel):
    station_id: str
    connection_fee: float | None = None
    price_per_kwh: float | None = None
    price_per_min: float | None = None
    idle_fee_per_min: float | None = None
    effective_start: str   # ISO8601 with tz (AK local from UI)
    effective_end: str | None = None


class PricingPatch(BaseModel):
    connection_fee: float | None = None
    price_per_kwh: float | None = None
    price_per_min: float | None = None
    idle_fee_per_min: float | None = None
    effective_start: str | None = None
    effective_end: str | None = None


class EvseUpsert(BaseModel):
    station_id: str
    display_name: str = ""
    location: str = ""
    platform: str = ""
    archived: bool = False


# ── Users ─────────────────────────────────────────────────────────────────────

@router.get("/users")
async def list_users(_: AdminUser):
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text, email, name, allowed_evse_ids, active, created_at "
            "FROM portal_users ORDER BY email"
        )
    return [dict(r) for r in rows]


@router.post("/users", status_code=201)
async def create_user(body: UserCreate, _: AdminUser):
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO portal_users (email, name, allowed_evse_ids, active)
            VALUES ($1, $2, $3, $4)
            RETURNING id::text, email, name, allowed_evse_ids, active
            """,
            body.email, body.name or None, body.allowed_evse_ids, body.active,
        )
    return dict(row)


@router.patch("/users/{user_id}")
async def update_user(user_id: str, body: UserPatch, _: AdminUser):
    fields: dict[str, Any] = {}
    if body.email            is not None: fields["email"]            = body.email
    if body.name             is not None: fields["name"]             = body.name
    if body.active           is not None: fields["active"]           = body.active
    if body.allowed_evse_ids is not None: fields["allowed_evse_ids"] = body.allowed_evse_ids

    if not fields:
        raise HTTPException(400, "No fields to update")

    set_clause = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields))
    values = [user_id] + list(fields.values())

    async with acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE portal_users SET {set_clause} WHERE id = $1::uuid "
            f"RETURNING id::text, email, name, allowed_evse_ids, active",
            *values,
        )
    if not row:
        raise HTTPException(404, "User not found")
    return dict(row)


# ── Pricing ───────────────────────────────────────────────────────────────────

@router.get("/pricing")
async def list_pricing(_: AdminUser):
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text, station_id,
                   connection_fee, price_per_kwh, price_per_min, idle_fee_per_min,
                   effective_start, effective_end
            FROM evse_pricing
            ORDER BY effective_start DESC NULLS LAST
            """
        )
    return [dict(r) for r in rows]


@router.post("/pricing", status_code=201)
async def create_pricing(body: PricingCreate, _: AdminUser):
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO evse_pricing
              (station_id, connection_fee, price_per_kwh, price_per_min,
               idle_fee_per_min, effective_start, effective_end)
            VALUES ($1,$2,$3,$4,$5,$6::timestamptz,$7::timestamptz)
            RETURNING id::text, station_id, connection_fee, price_per_kwh,
                      price_per_min, idle_fee_per_min, effective_start, effective_end
            """,
            body.station_id, body.connection_fee, body.price_per_kwh,
            body.price_per_min, body.idle_fee_per_min,
            body.effective_start, body.effective_end,
        )
    return dict(row)


@router.patch("/pricing/{pricing_id}")
async def update_pricing(pricing_id: str, body: PricingPatch, _: AdminUser):
    fields: dict[str, Any] = {}
    if body.connection_fee   is not None: fields["connection_fee"]   = body.connection_fee
    if body.price_per_kwh    is not None: fields["price_per_kwh"]    = body.price_per_kwh
    if body.price_per_min    is not None: fields["price_per_min"]    = body.price_per_min
    if body.idle_fee_per_min is not None: fields["idle_fee_per_min"] = body.idle_fee_per_min
    if body.effective_start  is not None: fields["effective_start"]  = body.effective_start
    if body.effective_end    is not None: fields["effective_end"]    = body.effective_end

    if not fields:
        raise HTTPException(400, "No fields to update")

    set_clause = ", ".join(
        f"{k} = ${i+2}{'::timestamptz' if 'effective' in k else ''}"
        for i, k in enumerate(fields)
    )
    values = [pricing_id] + list(fields.values())

    async with acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE evse_pricing SET {set_clause} WHERE id = $1::uuid "
            f"RETURNING id::text, station_id, connection_fee, price_per_kwh, "
            f"price_per_min, idle_fee_per_min, effective_start, effective_end",
            *values,
        )
    if not row:
        raise HTTPException(404, "Pricing rule not found")
    return dict(row)


# ── EVSEs ─────────────────────────────────────────────────────────────────────

@router.get("/evse")
async def list_evse(_: AdminUser):
    display  = get_evse_display()
    location = get_evse_location()
    platform = get_platform_map()
    archived = set(get_archived_station_ids())
    all_ids  = sorted(set(display) | set(location) | set(platform))
    return [
        {
            "station_id":   sid,
            "display_name": display.get(sid, ""),
            "location":     location.get(sid, ""),
            "platform":     platform.get(sid, ""),
            "archived":     sid in archived,
        }
        for sid in all_ids
    ]


@router.get("/evse/unidentified")
async def list_unidentified_evse(_: AdminUser):
    known = set(get_all_station_ids())
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                COALESCE(asset_id, action_payload->'data'->'asset'->>'id') AS station_id,
                MAX(received_at) AS last_seen
            FROM ocpp_events
            WHERE COALESCE(asset_id, action_payload->'data'->'asset'->>'id') IS NOT NULL
            GROUP BY station_id
            ORDER BY last_seen DESC
            """
        )
    result = []
    for r in rows:
        sid = r["station_id"]
        if sid not in known:
            last = r["last_seen"]
            if last and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            result.append({
                "station_id":  sid,
                "last_seen_ak": last.astimezone(_AK).strftime("%m/%d/%y %H:%M") if last else "",
            })
    return result


@router.put("/evse")
async def upsert_evse(body: EvseUpsert, _: AdminUser):
    """Write EVSE metadata to runtime_overrides.json AND public.chargers / public.sites."""
    sid = body.station_id.strip()
    if not sid:
        raise HTTPException(400, "station_id is required")

    # ── 1. Update runtime_overrides.json (immediate in-process effect) ─────────
    ov     = _read_overrides()
    ev_map = ov.get("evse_display",           {})
    lo_map = ov.get("evse_location",          {})
    pf_map = ov.get("platform_map",           {})
    ar_set = set(ov.get("archived_station_ids", []))

    if body.display_name: ev_map[sid] = body.display_name
    if body.location:     lo_map[sid] = body.location
    if body.platform:     pf_map[sid] = body.platform

    if body.archived:
        ar_set.add(sid)
    else:
        ar_set.discard(sid)

    ov["evse_display"]         = ev_map
    ov["evse_location"]        = lo_map
    ov["platform_map"]         = pf_map
    ov["archived_station_ids"] = sorted(ar_set)
    _write_overrides(ov)

    # ── 2. Write to public.chargers + public.sites in Supabase ────────────────
    async with acquire() as conn:
        # Find or create site by location name
        site_id: str | None = None
        if body.location:
            row = await conn.fetchrow(
                "SELECT id::text FROM sites WHERE LOWER(name) = LOWER($1) LIMIT 1",
                body.location,
            )
            if row:
                site_id = row["id"]
            else:
                row = await conn.fetchrow(
                    "INSERT INTO sites (name) VALUES ($1) RETURNING id::text",
                    body.location,
                )
                site_id = row["id"] if row else None

        # Check if charger already exists (match on external_id)
        existing = await conn.fetchrow(
            "SELECT id::text FROM chargers WHERE external_id = $1 LIMIT 1",
            sid,
        )

        if existing:
            # Build dynamic UPDATE — only set non-empty fields
            sets: list[str] = ["updated_at = NOW()"]
            vals: list      = []
            idx = 1

            if body.display_name:
                sets.append(f"name = ${idx}"); vals.append(body.display_name); idx += 1
            if site_id:
                sets.append(f"site_id = ${idx}::uuid"); vals.append(site_id); idx += 1
            if body.platform:
                sets.append(f"make = ${idx}"); vals.append(body.platform); idx += 1

            if len(sets) > 1:  # something beyond just updated_at
                await conn.execute(
                    f"UPDATE chargers SET {', '.join(sets)} WHERE external_id = ${idx}",
                    *vals, sid,
                )
        else:
            # INSERT new charger
            await conn.execute(
                """
                INSERT INTO chargers (external_id, name, site_id, make, connector_types)
                VALUES ($1, $2, $3::uuid, $4, '{}'::jsonb)
                """,
                sid,
                body.display_name or sid,
                site_id,
                body.platform or None,
            )

    return {"ok": True, "station_id": sid}
