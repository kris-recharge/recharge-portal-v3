"""Utility account & credentials management API + on-demand collection trigger.

All routes require admin access (same guard as admin.py).

Routes
------
GET    /api/utility/accounts              — list all accounts
POST   /api/utility/accounts              — add account
PATCH  /api/utility/accounts/{id}         — update account (enable/disable, rename, etc.)
DELETE /api/utility/accounts/{id}         — remove account

GET    /api/utility/credentials           — list utilities with credentials (no passwords)
PUT    /api/utility/credentials/{utility} — set/update credentials for a utility

GET    /api/utility/usage                 — query collected kWh data
POST   /api/utility/collect               — trigger an immediate collection run
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from ..auth import CurrentUser, PortalUser
from ..config import DEV_BYPASS_AUTH
from ..db import acquire

logger = logging.getLogger("rca.routers.utility")

router = APIRouter(prefix="/api/utility", tags=["utility"])

ADMIN_EMAIL = "kris.hall@rechargealaska.net"

VALID_UTILITIES = {"gvea", "cvea", "cea"}


# ── Admin guard (reuse same pattern as admin.py) ──────────────────────────────

async def _require_admin(user: CurrentUser) -> PortalUser:
    if not DEV_BYPASS_AUTH and user.email != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Admin access only")
    return user

AdminUser = Annotated[PortalUser, Depends(_require_admin)]


# ── Pydantic models ───────────────────────────────────────────────────────────

class AccountCreate(BaseModel):
    utility:                 str
    account_number:          str
    display_name:            str = ""
    service_location_number: str | None = None
    customer_number:         str | None = None
    system_of_record:        str = "UTILITY"
    meter_group_id:          str | None = None
    enabled:                 bool = True


class AccountPatch(BaseModel):
    display_name:            str | None = None
    service_location_number: str | None = None
    customer_number:         str | None = None
    meter_group_id:          str | None = None
    enabled:                 bool | None = None


class CredentialUpsert(BaseModel):
    username: str
    password: str


class CollectRequest(BaseModel):
    days_back: int = 2
    utility:   str | None = None   # if set, only collect this utility


# ── Accounts ──────────────────────────────────────────────────────────────────

@router.get("/accounts")
async def list_accounts(_: AdminUser):
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, utility, account_number, display_name,
                   service_location_number, customer_number,
                   system_of_record, meter_group_id,
                   enabled, last_collected, last_error, created_at
            FROM utility_accounts
            ORDER BY utility, account_number
            """
        )
    result = []
    for r in rows:
        d = dict(r)
        # Serialise timestamps to ISO strings for JSON
        for key in ("last_collected", "created_at"):
            if d[key] is not None:
                d[key] = d[key].isoformat()
        result.append(d)
    return result


@router.post("/accounts", status_code=201)
async def create_account(body: AccountCreate, _: AdminUser):
    if body.utility not in VALID_UTILITIES:
        raise HTTPException(
            400,
            f"Unknown utility '{body.utility}'. "
            f"Valid values: {sorted(VALID_UTILITIES)}"
        )
    async with acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO utility_accounts
                    (utility, account_number, display_name,
                     service_location_number, customer_number,
                     system_of_record, meter_group_id, enabled)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id, utility, account_number, display_name,
                          service_location_number, customer_number,
                          system_of_record, meter_group_id,
                          enabled, last_collected, last_error, created_at
                """,
                body.utility, body.account_number, body.display_name,
                body.service_location_number, body.customer_number,
                body.system_of_record, body.meter_group_id, body.enabled,
            )
        except Exception as exc:
            if "unique" in str(exc).lower():
                raise HTTPException(
                    409,
                    f"Account {body.utility}/{body.account_number} already exists",
                )
            raise
    d = dict(row)
    for key in ("last_collected", "created_at"):
        if d[key] is not None:
            d[key] = d[key].isoformat()
    return d


@router.patch("/accounts/{account_id}")
async def update_account(
    account_id: int, body: AccountPatch, _: AdminUser
):
    fields: dict[str, Any] = {}
    if body.display_name            is not None: fields["display_name"]            = body.display_name
    if body.service_location_number is not None: fields["service_location_number"] = body.service_location_number
    if body.customer_number         is not None: fields["customer_number"]         = body.customer_number
    if body.meter_group_id          is not None: fields["meter_group_id"]          = body.meter_group_id
    if body.enabled                 is not None: fields["enabled"]                 = body.enabled

    if not fields:
        raise HTTPException(400, "No fields to update")

    set_clause = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields))
    values     = [account_id] + list(fields.values())

    async with acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE utility_accounts SET {set_clause}
            WHERE id = $1
            RETURNING id, utility, account_number, display_name,
                      service_location_number, customer_number,
                      system_of_record, meter_group_id,
                      enabled, last_collected, last_error, created_at
            """,
            *values,
        )
    if not row:
        raise HTTPException(404, "Account not found")
    d = dict(row)
    for key in ("last_collected", "created_at"):
        if d[key] is not None:
            d[key] = d[key].isoformat()
    return d


@router.delete("/accounts/{account_id}", status_code=204)
async def delete_account(account_id: int, _: AdminUser):
    async with acquire() as conn:
        result = await conn.execute(
            "DELETE FROM utility_accounts WHERE id = $1", account_id
        )
    if result == "DELETE 0":
        raise HTTPException(404, "Account not found")


# ── Credentials ───────────────────────────────────────────────────────────────

@router.get("/credentials")
async def list_credentials(_: AdminUser):
    """List which utilities have credentials stored. Passwords are never returned."""
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT utility, username, updated_at FROM utility_credentials ORDER BY utility"
        )
    result = []
    for r in rows:
        d = dict(r)
        if d["updated_at"] is not None:
            d["updated_at"] = d["updated_at"].isoformat()
        result.append(d)
    return result


@router.put("/credentials/{utility}")
async def upsert_credentials(
    utility: str, body: CredentialUpsert, _: AdminUser
):
    if utility not in VALID_UTILITIES:
        raise HTTPException(
            400,
            f"Unknown utility '{utility}'. Valid values: {sorted(VALID_UTILITIES)}"
        )
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO utility_credentials (utility, username, password)
            VALUES ($1, $2, $3)
            ON CONFLICT (utility) DO UPDATE
                SET username   = EXCLUDED.username,
                    password   = EXCLUDED.password,
                    updated_at = NOW()
            RETURNING utility, username, updated_at
            """,
            utility, body.username, body.password,
        )
    d = dict(row)
    if d["updated_at"] is not None:
        d["updated_at"] = d["updated_at"].isoformat()
    return d


# ── Usage data ────────────────────────────────────────────────────────────────

@router.get("/usage")
async def get_usage(
    _:             AdminUser,
    utility:       str | None  = Query(None),
    account_number: str | None = Query(None),
    start_date:    str | None  = Query(None),   # YYYY-MM-DD
    end_date:      str | None  = Query(None),   # YYYY-MM-DD
    limit:         int         = Query(1000, ge=1, le=10000),
):
    """Return collected utility usage rows for charting / export."""
    conditions = []
    params: list[Any] = []
    idx = 1

    if utility:
        conditions.append(f"utility = ${idx}"); params.append(utility); idx += 1
    if account_number:
        conditions.append(f"account_number = ${idx}"); params.append(account_number); idx += 1
    if start_date:
        conditions.append(f"interval_start >= ${idx}::date"); params.append(start_date); idx += 1
    if end_date:
        conditions.append(f"interval_start <  (${idx}::date + interval '1 day')"); params.append(end_date); idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    async with acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT utility, account_number, meter_id,
                   interval_start, interval_end,
                   kwh, is_estimated, granularity_min, collected_at
            FROM utility_usage
            {where}
            ORDER BY utility, account_number, interval_start DESC
            LIMIT ${idx}
            """,
            *params, limit,
        )

    result = []
    for r in rows:
        d = dict(r)
        for key in ("interval_start", "interval_end", "collected_at"):
            if d[key] is not None:
                d[key] = d[key].isoformat()
        result.append(d)
    return {"usage": result, "count": len(result)}


# ── Manual collection trigger ─────────────────────────────────────────────────

@router.post("/collect")
async def trigger_collect(body: CollectRequest, _: AdminUser):
    """Kick off an immediate collection run in the background.

    Returns immediately — check account last_collected / last_error for results.
    """
    from ..collectors.scheduler import run_all_collectors

    async def _run():
        try:
            await run_all_collectors(days_back=body.days_back)
        except Exception as exc:
            logger.error("Manual collect run failed: %s", exc, exc_info=True)

    asyncio.create_task(_run())
    return {"ok": True, "message": f"Collection started (days_back={body.days_back})"}
