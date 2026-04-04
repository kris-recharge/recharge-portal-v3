"""Maintenance Tracker API — v3.1

Routes:
  GET  /api/maintenance/overview              — fleet overview (all users, filtered by EVSE access)
  GET  /api/maintenance/units/{charger_id}    — unit detail + history
  GET  /api/maintenance/templates/{charger_id}/{pm_type}  — PM form tasks
  POST /api/maintenance/records               — submit maintenance record (admin only)
  PATCH /api/maintenance/records/{record_id}/hyperdoc     — mark Hyperdoc submitted (admin)

  GET  /api/admin/fleet/unit-types            — list unit types (admin)
  POST /api/admin/fleet/unit-types            — create unit type (admin)
  PATCH /api/admin/fleet/unit-types/{type_id} — update unit type (admin)
  POST /api/admin/fleet/onboard               — onboard new unit (admin)
  POST /api/admin/fleet/units/{charger_id}/move   — move unit to site (admin)
  POST /api/admin/fleet/units/{charger_id}/retire — retire unit (admin)
  PATCH /api/admin/fleet/units/{charger_id}   — update operational flags (admin)
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from ..auth import CurrentUser, PortalUser
from ..config import DEV_BYPASS_AUTH
from ..db import acquire

router = APIRouter(tags=["maintenance"])

ADMIN_EMAIL = "kris.hall@rechargealaska.net"


# ── Admin guard ───────────────────────────────────────────────────────────────

async def _require_admin(user: CurrentUser) -> PortalUser:
    if not DEV_BYPASS_AUTH and user.email != ADMIN_EMAIL:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access only")
    return user

AdminUser = Annotated[PortalUser, Depends(_require_admin)]


# ── Warranty status helper ────────────────────────────────────────────────────

def _warranty_status(warranty_end: date | None, owner_name: str | None) -> dict:
    if warranty_end is None:
        if owner_name:
            return {"status": "warranty_unknown", "label": "Warranty Unknown",
                    "color": "gray", "asterisk": True}
        return {"status": "no_warranty", "label": "No Warranty", "color": "gray", "asterisk": False}

    today = date.today()
    days_remaining = (warranty_end - today).days

    if today >= warranty_end:
        return {"status": "out_of_warranty", "label": "Out of Warranty",
                "color": "gray", "days_remaining": 0, "asterisk": False}
    if days_remaining <= 90:
        return {"status": "expiring_soon",
                "label": f"Expiring {warranty_end.strftime('%b %d, %Y')}",
                "color": "amber", "days_remaining": days_remaining, "asterisk": False}
    return {"status": "in_warranty", "label": "In Warranty",
            "color": "green", "days_remaining": days_remaining, "asterisk": False}


# ── Charger visibility filter ─────────────────────────────────────────────────

def _allowed_external_ids(user: PortalUser) -> list[str] | None:
    """None = no restriction (admin). [] = no access."""
    if user.email == ADMIN_EMAIL or DEV_BYPASS_AUTH:
        return None  # admin sees everything
    if user.allowed_evse_ids is None:
        return None
    return user.allowed_evse_ids


# ── Overview endpoint ─────────────────────────────────────────────────────────

@router.get("/api/maintenance/overview")
async def maintenance_overview(
    user: CurrentUser,
    show_retired: bool = Query(False),
):
    allowed = _allowed_external_ids(user)

    async with acquire() as conn:
        # Build the WHERE clause for visibility
        if allowed is None:
            # Admin: sees all (including non-LynkWell units with NULL external_id)
            status_filter = "AND c.status = 'active'" if not show_retired else ""
            charger_rows = await conn.fetch(
                f"""
                SELECT
                    c.id::text, c.external_id, c.name,
                    c.serial_number, c.status,
                    c.warranty_start, c.warranty_end, c.warranty_notes,
                    c.owner_name, c.maintenance_responsibility,
                    c.network_platform, c.network_platform_notes,
                    c.port_count, c.commission_date,
                    c.retired_at, c.retired_reason,
                    c.parts_on_order,
                    s.name  AS site_name,
                    s.id::text AS site_id,
                    ut.id::text   AS unit_type_id,
                    ut.type_name  AS unit_type_name,
                    ut.manufacturer,
                    ut.hyperdoc_required,
                    ut.interval_quarterly_months,
                    ut.interval_semiannual_months,
                    ut.interval_annual_months,
                    ut.mirror_type_id::text AS mirror_type_id,
                    -- Template coverage: check if a template exists for this type (or its mirror)
                    EXISTS (
                        SELECT 1 FROM pm_templates pt
                        WHERE pt.is_active = true
                          AND (pt.unit_type_id = ut.id
                               OR (ut.mirror_type_id IS NOT NULL AND pt.unit_type_id = ut.mirror_type_id))
                    ) AS has_pm_template
                FROM chargers c
                LEFT JOIN sites s     ON s.id = c.site_id
                LEFT JOIN unit_types ut ON ut.id = c.unit_type_id
                WHERE 1=1 {status_filter}
                ORDER BY s.name NULLS LAST, c.name
                """
            )
        else:
            # Non-admin: only sees their allowed EVSEs (by external_id)
            if not allowed:
                return {"chargers": []}
            charger_rows = await conn.fetch(
                """
                SELECT
                    c.id::text, c.external_id, c.name,
                    c.serial_number, c.status,
                    c.warranty_start, c.warranty_end, c.warranty_notes,
                    c.owner_name, c.maintenance_responsibility,
                    c.network_platform, c.network_platform_notes,
                    c.port_count, c.commission_date,
                    c.retired_at, c.retired_reason,
                    c.parts_on_order,
                    s.name  AS site_name,
                    s.id::text AS site_id,
                    ut.id::text   AS unit_type_id,
                    ut.type_name  AS unit_type_name,
                    ut.manufacturer,
                    ut.hyperdoc_required,
                    ut.interval_quarterly_months,
                    ut.interval_semiannual_months,
                    ut.interval_annual_months,
                    ut.mirror_type_id::text AS mirror_type_id,
                    EXISTS (
                        SELECT 1 FROM pm_templates pt
                        WHERE pt.is_active = true
                          AND (pt.unit_type_id = ut.id
                               OR (ut.mirror_type_id IS NOT NULL AND pt.unit_type_id = ut.mirror_type_id))
                    ) AS has_pm_template
                FROM chargers c
                LEFT JOIN sites s     ON s.id = c.site_id
                LEFT JOIN unit_types ut ON ut.id = c.unit_type_id
                WHERE c.status = 'active'
                  AND c.external_id = ANY($1::text[])
                ORDER BY s.name NULLS LAST, c.name
                """,
                allowed,
            )

        if not charger_rows:
            return {"chargers": []}

        charger_ids = [r["id"] for r in charger_rows]

        # Last PM per interval per charger
        pm_rows = await conn.fetch(
            """
            SELECT
                charger_id::text,
                record_type,
                MAX(record_timestamp) AS last_pm
            FROM maintenance_records
            WHERE charger_id = ANY($1::uuid[])
              AND record_type IN ('pm_quarterly','pm_semi_annual','pm_annual','pm_general')
            GROUP BY charger_id, record_type
            """,
            charger_ids,
        )
        pm_by_charger: dict[str, dict] = {}
        for row in pm_rows:
            cid = row["charger_id"]
            if cid not in pm_by_charger:
                pm_by_charger[cid] = {}
            pm_by_charger[cid][row["record_type"]] = row["last_pm"]

        # Open follow-up count per charger (latest record with additional_work_needed = true)
        followup_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (charger_id)
                charger_id::text,
                additional_work_needed
            FROM maintenance_records
            WHERE charger_id = ANY($1::uuid[])
            ORDER BY charger_id, record_timestamp DESC
            """,
            charger_ids,
        )
        service_needed: dict[str, bool] = {
            r["charger_id"]: r["additional_work_needed"]
            for r in followup_rows
        }

        # Pending Hyperdoc per charger
        hyperdoc_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (charger_id)
                charger_id::text,
                hyperdoc_required,
                hyperdoc_submitted
            FROM maintenance_records
            WHERE charger_id = ANY($1::uuid[])
              AND hyperdoc_required = true
            ORDER BY charger_id, record_timestamp DESC
            """,
            charger_ids,
        )
        hyperdoc_pending: dict[str, bool] = {
            r["charger_id"]: (r["hyperdoc_required"] and not r["hyperdoc_submitted"])
            for r in hyperdoc_rows
        }

    today = date.today()
    result = []
    for c in charger_rows:
        cid = c["id"]
        pm_data = pm_by_charger.get(cid, {})

        # Compute next PM due dates
        q_months  = c["interval_quarterly_months"]
        sa_months = c["interval_semiannual_months"]
        a_months  = c["interval_annual_months"] or 12

        def _next_due(last_ts, months):
            if not months:
                return None
            if not last_ts:
                return None  # Never had a PM — no calculated due date
            last_date = last_ts.date() if hasattr(last_ts, "date") else last_ts
            # Approximate months as 30 days each
            due = date.fromordinal(last_date.toordinal() + months * 30)
            return due.isoformat()

        last_q  = pm_data.get("pm_quarterly")
        last_sa = pm_data.get("pm_semi_annual")
        last_a  = pm_data.get("pm_annual") or pm_data.get("pm_general")

        next_q  = _next_due(last_q,  q_months)
        next_sa = _next_due(last_sa, sa_months)
        next_a  = _next_due(last_a,  a_months)

        # Soonest upcoming PM
        upcoming = [(d, t) for d, t in [
            (next_q, "quarterly"), (next_sa, "semi_annual"), (next_a, "annual")
        ] if d]
        upcoming.sort(key=lambda x: x[0])
        next_due_date = upcoming[0][0] if upcoming else None
        next_due_type = upcoming[0][1] if upcoming else None
        next_overdue  = (next_due_date is not None and next_due_date < today.isoformat())

        # Format last PM dates
        def _fmt_ts(ts):
            if ts is None:
                return None
            return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)

        result.append({
            "id": cid,
            "external_id": c["external_id"],
            "name": c["name"],
            "serial_number": c["serial_number"],
            "status": c["status"],
            "site_name": c["site_name"],
            "site_id": c["site_id"],
            "unit_type_id": c["unit_type_id"],
            "unit_type_name": c["unit_type_name"],
            "manufacturer": c["manufacturer"],
            "hyperdoc_required": c["hyperdoc_required"] or False,
            "maintenance_responsibility": c["maintenance_responsibility"],
            "network_platform": c["network_platform"],
            "owner_name": c["owner_name"],
            "warranty_end": c["warranty_end"].isoformat() if c["warranty_end"] else None,
            "warranty_start": c["warranty_start"].isoformat() if c["warranty_start"] else None,
            "warranty_notes": c["warranty_notes"],
            "warranty": _warranty_status(c["warranty_end"], c["owner_name"]),
            "commission_date": c["commission_date"].isoformat() if c["commission_date"] else None,
            "retired_at": _fmt_ts(c["retired_at"]),
            "retired_reason": c["retired_reason"],
            # PM status
            "last_pm_quarterly":   _fmt_ts(last_q),
            "last_pm_semi_annual": _fmt_ts(last_sa),
            "last_pm_annual":      _fmt_ts(last_a),
            "next_pm_quarterly_due":   next_q,
            "next_pm_semi_annual_due": next_sa,
            "next_pm_annual_due":      next_a,
            "next_pm_due_date": next_due_date,
            "next_pm_due_type": next_due_type,
            "pm_overdue": next_overdue,
            # Alert badges
            "service_needed":       service_needed.get(cid, False),
            "parts_on_order":       c["parts_on_order"] or False,
            "hyperdoc_pending":     hyperdoc_pending.get(cid, False),
            "pm_template_pending":  not c["has_pm_template"],
        })

    return {"chargers": result}


# ── Unit detail endpoint ──────────────────────────────────────────────────────

@router.get("/api/maintenance/units/{charger_id}")
async def maintenance_unit_detail(charger_id: str, user: CurrentUser):
    allowed = _allowed_external_ids(user)

    async with acquire() as conn:
        charger = await conn.fetchrow(
            """
            SELECT
                c.id::text, c.external_id, c.name,
                c.serial_number, c.status,
                c.warranty_start, c.warranty_end, c.warranty_notes,
                c.owner_name, c.maintenance_responsibility,
                c.network_platform, c.network_platform_notes,
                c.port_count, c.commission_date,
                c.retired_at, c.retired_reason,
                c.parts_on_order,
                s.name  AS site_name,
                s.id::text AS site_id,
                ut.id::text   AS unit_type_id,
                ut.type_name  AS unit_type_name,
                ut.manufacturer,
                ut.hyperdoc_required,
                ut.interval_quarterly_months,
                ut.interval_semiannual_months,
                ut.interval_annual_months
            FROM chargers c
            LEFT JOIN sites s       ON s.id = c.site_id
            LEFT JOIN unit_types ut ON ut.id = c.unit_type_id
            WHERE c.id = $1::uuid
            """,
            charger_id,
        )

        if not charger:
            raise HTTPException(404, "Unit not found")

        # Non-admin visibility check
        if allowed is not None and charger["external_id"] not in (allowed or []):
            raise HTTPException(403, "Access denied")

        # Maintenance records (most recent first, up to 100)
        records = await conn.fetch(
            """
            SELECT
                mr.id::text,
                mr.record_timestamp,
                mr.record_type,
                mr.overall_result,
                mr.firmware_version,
                mr.technician_name,
                mr.work_description,
                mr.onsite_hours,
                mr.mobilized_hours,
                mr.additional_work_needed,
                mr.planned_future_work,
                mr.hyperdoc_required,
                mr.hyperdoc_submitted,
                mr.hyperdoc_submitted_at,
                mr.pm_template_version,
                s.name AS site_name
            FROM maintenance_records mr
            LEFT JOIN sites s ON s.id = mr.site_id
            WHERE mr.charger_id = $1::uuid
            ORDER BY mr.record_timestamp DESC
            LIMIT 100
            """,
            charger_id,
        )

        # Parts replaced per record
        if records:
            record_ids = [r["id"] for r in records]
            parts_rows = await conn.fetch(
                """
                SELECT record_id::text, part_name, part_number, action_taken, notes
                FROM maintenance_parts_replaced
                WHERE record_id = ANY($1::uuid[])
                ORDER BY record_id, id
                """,
                record_ids,
            )
            parts_by_record: dict[str, list] = {}
            for p in parts_rows:
                parts_by_record.setdefault(p["record_id"], []).append({
                    "part_name": p["part_name"],
                    "part_number": p["part_number"],
                    "action_taken": p["action_taken"],
                    "notes": p["notes"],
                })
        else:
            parts_by_record = {}

        # Location history
        loc_history = await conn.fetch(
            """
            SELECT
                ulh.id::text,
                ulh.assigned_at,
                ulh.notes,
                s.name AS site_name
            FROM unit_location_history ulh
            LEFT JOIN sites s ON s.id = ulh.site_id
            WHERE ulh.charger_id = $1::uuid
            ORDER BY ulh.assigned_at DESC
            """,
            charger_id,
        )

    def _fmt(v):
        if v is None:
            return None
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return v

    records_out = []
    for r in records:
        records_out.append({
            "id": r["id"],
            "record_timestamp": _fmt(r["record_timestamp"]),
            "record_type": r["record_type"],
            "overall_result": r["overall_result"],
            "firmware_version": r["firmware_version"],
            "technician_name": r["technician_name"],
            "work_description": r["work_description"],
            "onsite_hours": float(r["onsite_hours"]) if r["onsite_hours"] is not None else None,
            "mobilized_hours": float(r["mobilized_hours"]) if r["mobilized_hours"] is not None else None,
            "additional_work_needed": r["additional_work_needed"],
            "planned_future_work": r["planned_future_work"],
            "hyperdoc_required": r["hyperdoc_required"],
            "hyperdoc_submitted": r["hyperdoc_submitted"],
            "hyperdoc_submitted_at": _fmt(r["hyperdoc_submitted_at"]),
            "pm_template_version": r["pm_template_version"],
            "site_name": r["site_name"],
            "parts": parts_by_record.get(r["id"], []),
        })

    c = charger
    return {
        "charger": {
            "id": c["id"],
            "external_id": c["external_id"],
            "name": c["name"],
            "serial_number": c["serial_number"],
            "status": c["status"],
            "site_name": c["site_name"],
            "site_id": c["site_id"],
            "unit_type_id": c["unit_type_id"],
            "unit_type_name": c["unit_type_name"],
            "manufacturer": c["manufacturer"],
            "hyperdoc_required": c["hyperdoc_required"] or False,
            "maintenance_responsibility": c["maintenance_responsibility"],
            "network_platform": c["network_platform"],
            "network_platform_notes": c["network_platform_notes"],
            "owner_name": c["owner_name"],
            "warranty_start": _fmt(c["warranty_start"]),
            "warranty_end": _fmt(c["warranty_end"]),
            "warranty_notes": c["warranty_notes"],
            "warranty": _warranty_status(c["warranty_end"], c["owner_name"]),
            "commission_date": _fmt(c["commission_date"]),
            "retired_at": _fmt(c["retired_at"]),
            "retired_reason": c["retired_reason"],
            "parts_on_order": c["parts_on_order"] or False,
            "port_count": c["port_count"],
            "interval_quarterly_months": c["interval_quarterly_months"],
            "interval_semiannual_months": c["interval_semiannual_months"],
            "interval_annual_months": c["interval_annual_months"],
        },
        "maintenance_records": records_out,
        "location_history": [
            {
                "id": lh["id"],
                "assigned_at": _fmt(lh["assigned_at"]),
                "site_name": lh["site_name"],
                "notes": lh["notes"],
            }
            for lh in loc_history
        ],
    }


# ── PM template (form tasks) ─────────────────────────────────────────────────

@router.get("/api/maintenance/templates/{charger_id}/{pm_type}")
async def get_pm_template(charger_id: str, pm_type: str, user: CurrentUser):
    valid_types = ("quarterly", "semi_annual", "annual")
    if pm_type not in valid_types:
        raise HTTPException(400, f"pm_type must be one of {valid_types}")

    async with acquire() as conn:
        # Get unit type for this charger
        charger = await conn.fetchrow(
            "SELECT unit_type_id FROM chargers WHERE id = $1::uuid",
            charger_id,
        )
        if not charger:
            raise HTTPException(404, "Unit not found")

        unit_type_id = charger["unit_type_id"]

        if not unit_type_id:
            # No unit type assigned — general inspection
            return {"template": None, "tasks": [], "fallback": "general_inspection"}

        # Look for a template for this unit type
        template = await conn.fetchrow(
            """
            SELECT id::text, template_name, pm_interval, source_document, template_version
            FROM pm_templates
            WHERE unit_type_id = $1 AND pm_interval = $2 AND is_active = true
            LIMIT 1
            """,
            unit_type_id, pm_type,
        )

        if not template:
            # Check mirror_type_id
            ut = await conn.fetchrow(
                "SELECT mirror_type_id FROM unit_types WHERE id = $1",
                unit_type_id,
            )
            if ut and ut["mirror_type_id"]:
                template = await conn.fetchrow(
                    """
                    SELECT id::text, template_name, pm_interval, source_document, template_version
                    FROM pm_templates
                    WHERE unit_type_id = $1 AND pm_interval = $2 AND is_active = true
                    LIMIT 1
                    """,
                    ut["mirror_type_id"], pm_type,
                )

        if not template:
            return {"template": None, "tasks": [], "fallback": "general_inspection"}

        # Get tasks for this template
        tasks = await conn.fetch(
            """
            SELECT
                id::text, task_code, task_order, task_category,
                task_name, task_description, input_type, unit_of_measure,
                is_required, is_conditional, conditional_label,
                critical_fail, fail_guidance
            FROM pm_template_tasks
            WHERE template_id = $1::uuid
            ORDER BY task_order
            """,
            template["id"],
        )

    return {
        "template": dict(template),
        "tasks": [dict(t) for t in tasks],
        "fallback": None,
    }


# ── Submit maintenance record ─────────────────────────────────────────────────

class PartReplaced(BaseModel):
    part_name: str
    part_number: str | None = None
    action_taken: str  # replaced|repaired|cleaned|adjusted
    notes: str | None = None


class TaskResult(BaseModel):
    task_id: str
    result_pass_fail: str | None = None   # pass|fail|na
    result_completed: bool | None = None
    result_measured_value: str | None = None
    result_text: str | None = None
    task_notes: str | None = None


class SubmitRecordBody(BaseModel):
    charger_id: str
    record_type: str
    pm_template_id: str | None = None
    pm_template_version: str | None = None
    overall_result: str | None = None  # pass|conditional|fail
    firmware_version: str | None = None
    technician_name: str
    work_description: str | None = None
    onsite_hours: float | None = None
    mobilized_hours: float | None = None
    additional_work_needed: bool = False
    planned_future_work: str | None = None
    task_results: list[TaskResult] = []
    parts: list[PartReplaced] = []


@router.post("/api/maintenance/records", status_code=201)
async def submit_record(body: SubmitRecordBody, user: AdminUser):
    if body.additional_work_needed and not body.planned_future_work:
        raise HTTPException(400, "planned_future_work is required when additional_work_needed is true")

    async with acquire() as conn:
        # Verify charger exists and get site_id
        charger = await conn.fetchrow(
            "SELECT id, site_id, unit_type_id FROM chargers WHERE id = $1::uuid",
            body.charger_id,
        )
        if not charger:
            raise HTTPException(404, "Unit not found")

        # Derive hyperdoc_required from unit type
        hyperdoc_req = False
        if charger["unit_type_id"]:
            ut = await conn.fetchrow(
                "SELECT hyperdoc_required FROM unit_types WHERE id = $1",
                charger["unit_type_id"],
            )
            if ut:
                hyperdoc_req = ut["hyperdoc_required"]

        # Insert maintenance record
        record = await conn.fetchrow(
            """
            INSERT INTO maintenance_records
                (charger_id, site_id, record_type,
                 pm_template_id, pm_template_version,
                 overall_result, firmware_version,
                 technician_name, technician_user_id,
                 work_description, onsite_hours, mobilized_hours,
                 additional_work_needed, planned_future_work,
                 hyperdoc_required, created_by)
            VALUES ($1::uuid, $2::uuid, $3,
                    $4::uuid, $5,
                    $6, $7,
                    $8, $9::uuid,
                    $10, $11, $12,
                    $13, $14,
                    $15, $16::uuid)
            RETURNING id::text, record_timestamp
            """,
            body.charger_id,
            str(charger["site_id"]) if charger["site_id"] else None,
            body.record_type,
            body.pm_template_id,
            body.pm_template_version,
            body.overall_result,
            body.firmware_version,
            body.technician_name,
            user.user_id,
            body.work_description,
            body.onsite_hours,
            body.mobilized_hours,
            body.additional_work_needed,
            body.planned_future_work,
            hyperdoc_req,
            user.user_id,
        )

        record_id = record["id"]

        # Insert task results
        for tr in body.task_results:
            await conn.execute(
                """
                INSERT INTO pm_task_results
                    (record_id, task_id, result_pass_fail, result_completed,
                     result_measured_value, result_text, task_notes)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7)
                """,
                record_id, tr.task_id, tr.result_pass_fail,
                tr.result_completed, tr.result_measured_value,
                tr.result_text, tr.task_notes,
            )

        # Insert parts replaced
        for p in body.parts:
            await conn.execute(
                """
                INSERT INTO maintenance_parts_replaced
                    (record_id, part_name, part_number, action_taken, notes)
                VALUES ($1::uuid, $2, $3, $4, $5)
                """,
                record_id, p.part_name, p.part_number, p.action_taken, p.notes,
            )

    return {"id": record_id, "record_timestamp": record["record_timestamp"].isoformat()}


# ── Update Hyperdoc submission ────────────────────────────────────────────────

class HyperdocPatch(BaseModel):
    submitted_at: str  # ISO date string YYYY-MM-DD


@router.patch("/api/maintenance/records/{record_id}/hyperdoc")
async def update_hyperdoc(record_id: str, body: HyperdocPatch, _: AdminUser):
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE maintenance_records
            SET hyperdoc_submitted = true, hyperdoc_submitted_at = $2::date
            WHERE id = $1::uuid
            RETURNING id::text
            """,
            record_id, body.submitted_at,
        )
    if not row:
        raise HTTPException(404, "Record not found")
    return {"ok": True, "id": row["id"]}


# ═══════════════════════════════════════════════════════════════════════════════
# Admin Fleet Management routes
# ═══════════════════════════════════════════════════════════════════════════════

# ── Unit Types ───────────────────────────────────────────────────────────────

@router.get("/api/admin/fleet/unit-types")
async def list_unit_types(_: AdminUser):
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                ut.id::text, ut.type_name, ut.manufacturer,
                ut.mirror_type_id::text AS mirror_type_id,
                ut.default_pm_template_id::text AS default_pm_template_id,
                ut.interval_quarterly_months,
                ut.interval_semiannual_months,
                ut.interval_annual_months,
                ut.hyperdoc_required, ut.notes, ut.is_active,
                -- template coverage
                (SELECT template_name FROM pm_templates pt
                 WHERE pt.unit_type_id = ut.id AND pt.pm_interval = 'annual'
                   AND pt.is_active = true LIMIT 1)     AS annual_template_name,
                (SELECT template_name FROM pm_templates pt
                 WHERE (pt.unit_type_id = ut.id OR pt.unit_type_id = ut.mirror_type_id)
                   AND pt.pm_interval = 'annual' AND pt.is_active = true LIMIT 1) AS effective_template_name,
                -- unit count
                (SELECT COUNT(*) FROM chargers c
                 WHERE c.unit_type_id = ut.id AND c.status = 'active') AS unit_count
            FROM unit_types ut
            ORDER BY ut.is_active DESC, ut.type_name
            """
        )
    return [dict(r) for r in rows]


class UnitTypeCreate(BaseModel):
    type_name: str
    manufacturer: str
    mirror_type_id: str | None = None
    interval_quarterly_months: int | None = 3
    interval_semiannual_months: int | None = 6
    interval_annual_months: int = 12
    hyperdoc_required: bool = False
    notes: str | None = None
    is_active: bool = True


@router.post("/api/admin/fleet/unit-types", status_code=201)
async def create_unit_type(body: UnitTypeCreate, _: AdminUser):
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO unit_types
                (type_name, manufacturer, mirror_type_id,
                 interval_quarterly_months, interval_semiannual_months, interval_annual_months,
                 hyperdoc_required, notes, is_active)
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9)
            RETURNING id::text, type_name, manufacturer, is_active
            """,
            body.type_name, body.manufacturer,
            body.mirror_type_id,
            body.interval_quarterly_months, body.interval_semiannual_months,
            body.interval_annual_months,
            body.hyperdoc_required, body.notes, body.is_active,
        )
    return dict(row)


class UnitTypePatch(BaseModel):
    type_name: str | None = None
    manufacturer: str | None = None
    mirror_type_id: str | None = None
    interval_quarterly_months: int | None = None
    interval_semiannual_months: int | None = None
    interval_annual_months: int | None = None
    hyperdoc_required: bool | None = None
    notes: str | None = None
    is_active: bool | None = None


@router.patch("/api/admin/fleet/unit-types/{type_id}")
async def update_unit_type(type_id: str, body: UnitTypePatch, _: AdminUser):
    fields: dict[str, Any] = {}
    for f in ["type_name", "manufacturer", "mirror_type_id",
              "interval_quarterly_months", "interval_semiannual_months",
              "interval_annual_months", "hyperdoc_required", "notes", "is_active"]:
        v = getattr(body, f)
        if v is not None:
            fields[f] = v
    if not fields:
        raise HTTPException(400, "No fields to update")

    set_clause = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields))
    async with acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE unit_types SET {set_clause} WHERE id = $1::uuid RETURNING id::text, type_name, is_active",
            type_id, *fields.values(),
        )
    if not row:
        raise HTTPException(404, "Unit type not found")
    return dict(row)


# ── New Unit Onboarding ───────────────────────────────────────────────────────

class OnboardUnitBody(BaseModel):
    serial_number: str
    name: str
    unit_type_id: str
    site_id: str
    commission_date: str | None = None
    warranty_start: str | None = None
    warranty_end: str | None = None
    warranty_notes: str | None = None
    owner_name: str | None = None
    maintenance_responsibility: str = "RCA"
    network_platform: str | None = None
    network_platform_notes: str | None = None
    port_count: int | None = None
    notes: str | None = None


@router.post("/api/admin/fleet/onboard", status_code=201)
async def onboard_unit(body: OnboardUnitBody, _: AdminUser):
    async with acquire() as conn:
        # Check serial_number not already used
        existing = await conn.fetchrow(
            "SELECT id FROM chargers WHERE serial_number = $1 LIMIT 1",
            body.serial_number,
        )
        if existing:
            raise HTTPException(409, f"Serial number '{body.serial_number}' is already registered")

        row = await conn.fetchrow(
            """
            INSERT INTO chargers
                (serial_number, name, unit_type_id, site_id,
                 commission_date, warranty_start, warranty_end, warranty_notes,
                 owner_name, maintenance_responsibility,
                 network_platform, network_platform_notes,
                 port_count, status, connector_types)
            VALUES ($1, $2, $3::uuid, $4::uuid,
                    $5::date, $6::date, $7::date, $8,
                    $9, $10, $11, $12, $13, 'active', '[]'::jsonb)
            RETURNING id::text, name, serial_number
            """,
            body.serial_number, body.name, body.unit_type_id, body.site_id,
            body.commission_date, body.warranty_start, body.warranty_end, body.warranty_notes,
            body.owner_name, body.maintenance_responsibility,
            body.network_platform, body.network_platform_notes,
            body.port_count,
        )

        charger_id = row["id"]

        # Create initial location history entry
        await conn.execute(
            """
            INSERT INTO unit_location_history (charger_id, site_id, notes)
            VALUES ($1::uuid, $2::uuid, 'Initial onboarding')
            """,
            charger_id, body.site_id,
        )

    return {"id": charger_id, "name": row["name"], "serial_number": row["serial_number"]}


# ── Move Unit ────────────────────────────────────────────────────────────────

class MoveUnitBody(BaseModel):
    site_id: str
    notes: str | None = None


@router.post("/api/admin/fleet/units/{charger_id}/move")
async def move_unit(charger_id: str, body: MoveUnitBody, user: AdminUser):
    async with acquire() as conn:
        # Verify charger exists
        charger = await conn.fetchrow(
            "SELECT id, name FROM chargers WHERE id = $1::uuid AND status = 'active'",
            charger_id,
        )
        if not charger:
            raise HTTPException(404, "Active unit not found")

        # Update current site
        await conn.execute(
            "UPDATE chargers SET site_id = $1::uuid, updated_at = NOW() WHERE id = $2::uuid",
            body.site_id, charger_id,
        )

        # Record location history
        await conn.execute(
            """
            INSERT INTO unit_location_history (charger_id, site_id, assigned_by, notes)
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4)
            """,
            charger_id, body.site_id, user.user_id, body.notes,
        )

    return {"ok": True}


# ── Retire Unit ──────────────────────────────────────────────────────────────

class RetireUnitBody(BaseModel):
    retired_reason: str


@router.post("/api/admin/fleet/units/{charger_id}/retire")
async def retire_unit(charger_id: str, body: RetireUnitBody, user: AdminUser):
    if not body.retired_reason.strip():
        raise HTTPException(400, "retired_reason is required")

    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE chargers
            SET status = 'retired',
                retired_at = NOW(),
                retired_reason = $2,
                retired_by = $3::uuid,
                updated_at = NOW()
            WHERE id = $1::uuid AND status = 'active'
            RETURNING id::text, name
            """,
            charger_id, body.retired_reason, user.user_id,
        )
    if not row:
        raise HTTPException(404, "Active unit not found")
    return {"ok": True, "id": row["id"], "name": row["name"]}


# ── Update operational flags (parts_on_order, etc.) ──────────────────────────

class FleetUnitPatch(BaseModel):
    parts_on_order: bool | None = None
    warranty_notes: str | None = None
    network_platform_notes: str | None = None
    notes: str | None = None


@router.patch("/api/admin/fleet/units/{charger_id}")
async def patch_fleet_unit(charger_id: str, body: FleetUnitPatch, _: AdminUser):
    fields: dict[str, Any] = {}
    if body.parts_on_order is not None:       fields["parts_on_order"]          = body.parts_on_order
    if body.warranty_notes is not None:       fields["warranty_notes"]           = body.warranty_notes
    if body.network_platform_notes is not None: fields["network_platform_notes"] = body.network_platform_notes
    if not fields:
        raise HTTPException(400, "No fields to update")
    fields["updated_at"] = datetime.now(timezone.utc)
    set_clause = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(fields))
    async with acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE chargers SET {set_clause} WHERE id = $1::uuid RETURNING id::text",
            charger_id, *fields.values(),
        )
    if not row:
        raise HTTPException(404, "Unit not found")
    return {"ok": True}


# ── Sites list (for dropdowns) ────────────────────────────────────────────────

@router.get("/api/maintenance/sites")
async def list_sites(_: CurrentUser):
    async with acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text, name FROM sites ORDER BY name"
        )
    return [dict(r) for r in rows]
