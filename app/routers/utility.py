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


# v3.2: Daily metered-vs-dispensed per meter.
# A meter maps to a site (whole-site meter, e.g. ARG) and optionally narrows to a
# single charger (e.g. each Delta unit has its own meter at one shared site).
# Params: $1 unrestricted (bool), $2 allowed station ids (text[]),
#         $3 start date, $4 end date.
UTILITY_EFFICIENCY_SQL = """
    WITH meters AS (
        SELECT ua.account_number, ua.utility, ua.display_name,
               ua.site_id, ua.charger_id,
               s.name  AS site_name,
               ch.name AS unit_name
        FROM utility_accounts ua
        JOIN sites s          ON s.id  = ua.site_id
        LEFT JOIN chargers ch ON ch.id = ua.charger_id
        WHERE ua.site_id IS NOT NULL
          AND ua.enabled = true
          AND (
                $1::boolean
                OR EXISTS (
                    SELECT 1 FROM chargers c
                    WHERE c.external_id = ANY($2::text[])
                      AND ( (ua.charger_id IS NOT NULL AND c.id = ua.charger_id)
                            OR (ua.charger_id IS NULL AND c.site_id = ua.site_id) )
                )
          )
    ),
    meter_stations AS (
        SELECT m.account_number, c.external_id AS station_id
        FROM meters m
        JOIN chargers c ON (
             (m.charger_id IS NOT NULL AND c.id = m.charger_id)
          OR (m.charger_id IS NULL AND c.site_id = m.site_id)
        )
    ),
    sess AS (
        SELECT ms.account_number,
               MIN(mv.ts)                            AS start_utc,
               MAX(mv.energy_wh) - MIN(mv.energy_wh) AS energy_wh
        FROM meter_stations ms
        JOIN meter_values_parsed mv ON mv.station_id = ms.station_id
        WHERE mv.transaction_id IS NOT NULL
          AND mv.ts >= $3::date
          AND mv.ts <  ($4::date + INTERVAL '2 days')
        GROUP BY ms.account_number, mv.station_id, mv.connector_id, mv.transaction_id
    )
    SELECT m.account_number, m.utility, m.display_name, m.site_name, m.unit_name,
           u.interval_start, u.interval_end,
           u.kwh AS metered_kwh,
           COALESCE(SUM(sess.energy_wh) FILTER (
               WHERE sess.start_utc >= u.interval_start
                 AND sess.start_utc <  u.interval_end
           ), 0) / 1000.0 AS dispensed_kwh
    FROM utility_usage u
    JOIN meters m   ON m.account_number = u.account_number
    LEFT JOIN sess  ON sess.account_number = m.account_number
    WHERE u.interval_start >= $3::date
      AND u.interval_start <  ($4::date + INTERVAL '1 day')
      -- v3.2: CEA (mymeterQ) emits overlapping rolling-24h reads stamped hourly;
      -- keep only the canonical midnight-UTC daily read. GVEA/CVEA are already
      -- midnight-stamped daily, so this is a no-op for them.
      AND (u.interval_start AT TIME ZONE 'UTC')::time = TIME '00:00:00'
    GROUP BY m.account_number, m.utility, m.display_name, m.site_name, m.unit_name,
             u.interval_start, u.interval_end, u.kwh
    ORDER BY m.site_name, m.account_number, u.interval_start
"""


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
    site_id:                 str | None = None   # v3.2: links meter to a site
    charger_id:              str | None = None   # v3.2: optional single-unit narrowing
    enabled:                 bool = True


class AccountPatch(BaseModel):
    display_name:            str | None = None
    service_location_number: str | None = None
    customer_number:         str | None = None
    meter_group_id:          str | None = None
    site_id:                 str | None = None   # v3.2: links meter to a site
    charger_id:              str | None = None   # v3.2: optional single-unit narrowing
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
            SELECT ua.id, ua.utility, ua.account_number, ua.display_name,
                   ua.service_location_number, ua.customer_number,
                   ua.system_of_record, ua.meter_group_id,
                   ua.site_id::text AS site_id, s.name AS site_name,
                   ua.charger_id::text AS charger_id, ch.name AS unit_name,
                   ua.enabled, ua.last_collected, ua.last_error, ua.created_at
            FROM utility_accounts ua
            LEFT JOIN sites s     ON s.id  = ua.site_id
            LEFT JOIN chargers ch ON ch.id = ua.charger_id
            ORDER BY ua.utility, ua.account_number
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
                WITH ins AS (
                    INSERT INTO utility_accounts
                        (utility, account_number, display_name,
                         service_location_number, customer_number,
                         system_of_record, meter_group_id, site_id, charger_id, enabled)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::uuid, $9::uuid, $10)
                    RETURNING *
                )
                SELECT ins.id, ins.utility, ins.account_number, ins.display_name,
                       ins.service_location_number, ins.customer_number,
                       ins.system_of_record, ins.meter_group_id,
                       ins.site_id::text AS site_id, s.name AS site_name,
                       ins.charger_id::text AS charger_id, ch.name AS unit_name,
                       ins.enabled, ins.last_collected, ins.last_error, ins.created_at
                FROM ins
                LEFT JOIN sites s     ON s.id  = ins.site_id
                LEFT JOIN chargers ch ON ch.id = ins.charger_id
                """,
                body.utility, body.account_number, body.display_name,
                body.service_location_number, body.customer_number,
                body.system_of_record, body.meter_group_id,
                body.site_id, body.charger_id, body.enabled,
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
    # v3.2: site_id / charger_id — empty string clears the assignment (NULL).
    if body.site_id                 is not None: fields["site_id"]                 = body.site_id or None
    if body.charger_id              is not None: fields["charger_id"]              = body.charger_id or None

    if not fields:
        raise HTTPException(400, "No fields to update")

    # v3.2: uuid columns need an explicit ::uuid cast in the placeholder.
    set_clause = ", ".join(
        f"{k} = ${i+2}::uuid" if k in ("site_id", "charger_id") else f"{k} = ${i+2}"
        for i, k in enumerate(fields)
    )
    values     = [account_id] + list(fields.values())

    async with acquire() as conn:
        row = await conn.fetchrow(
            f"""
            WITH upd AS (
                UPDATE utility_accounts SET {set_clause}
                WHERE id = $1
                RETURNING *
            )
            SELECT upd.id, upd.utility, upd.account_number, upd.display_name,
                   upd.service_location_number, upd.customer_number,
                   upd.system_of_record, upd.meter_group_id,
                   upd.site_id::text AS site_id, s.name AS site_name,
                   upd.charger_id::text AS charger_id, ch.name AS unit_name,
                   upd.enabled, upd.last_collected, upd.last_error, upd.created_at
            FROM upd
            LEFT JOIN sites s     ON s.id  = upd.site_id
            LEFT JOIN chargers ch ON ch.id = upd.charger_id
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


# ── Site efficiency (v3.2) ────────────────────────────────────────────────────

@router.get("/efficiency")
async def utility_efficiency(
    user:       CurrentUser,
    start_date: str | None = Query(None),   # YYYY-MM-DD (AK)
    end_date:   str | None = Query(None),   # YYYY-MM-DD (AK)
):
    """v3.2: Daily site efficiency per meter.

    efficiency_pct = (session kWh dispensed at the meter's site during the
    utility interval) / (utility metered kWh) * 100.

    Access scope follows unit access: a user who can see any unit at a site
    sees that site's meter(s). Admins (allowed_evse_ids = None) see all meters
    that have been assigned a site.
    """
    # ── Resolve access scope ──────────────────────────────────────────────────
    allowed = None if (user.email == ADMIN_EMAIL or DEV_BYPASS_AUTH) else user.allowed_evse_ids
    unrestricted = allowed is None or "__ALL__" in (allowed or [])
    if not unrestricted and not allowed:
        return {"meters": []}          # [] = no access
    allowed_ids = list(allowed) if (allowed and not unrestricted) else []

    # ── Date window (default: last 30 days) ───────────────────────────────────
    today = datetime.now(timezone.utc).date()
    start = date.fromisoformat(start_date) if start_date else date.fromordinal(today.toordinal() - 29)
    end   = date.fromisoformat(end_date)   if end_date   else today

    async with acquire() as conn:
        rows = await conn.fetch(
            UTILITY_EFFICIENCY_SQL,
            unrestricted, allowed_ids, start, end,
        )

    # ── Group daily rows by meter ─────────────────────────────────────────────
    meters: dict[str, dict] = {}
    for r in rows:
        key = r["account_number"]
        if key not in meters:
            meters[key] = {
                "account_number": r["account_number"],
                "utility":        r["utility"],
                "display_name":   r["display_name"],
                "site_name":      r["site_name"],
                "unit_name":      r["unit_name"],
                "days":           [],
            }
        metered   = float(r["metered_kwh"])   if r["metered_kwh"]   is not None else 0.0
        dispensed = float(r["dispensed_kwh"]) if r["dispensed_kwh"] is not None else 0.0
        eff = round(dispensed / metered * 100.0, 1) if metered > 0 else None
        meters[key]["days"].append({
            "date":           r["interval_start"].date().isoformat(),
            "metered_kwh":    round(metered, 3),
            "dispensed_kwh":  round(dispensed, 3),
            "efficiency_pct": eff,
        })

    return {"meters": list(meters.values())}


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
