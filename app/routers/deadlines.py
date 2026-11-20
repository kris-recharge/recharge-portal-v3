"""Admin → Deadlines API: ReCharge's bills, filings and renewals, with countdowns.

Visible to @rechargealaska.net accounts only (auth.is_staff), checked here on
the server for every route, never just hidden in the UI. External dashboard
users (AEA, site hosts) get 403. The tables themselves have RLS on with no
policies and no anon/authenticated grants, so the Supabase Data API returns
nothing either (migrations/20261120_admin_deadlines.sql).

Routes (all under /api/admin/deadlines):
  GET    ?status=open,scheduled&category=payable  list with days_remaining + urgency
  POST                                            create
  PATCH  /{id}                                    edit (status may only be set back to 'open')
  POST   /{id}/complete                           mark done; spawns the next instance if recurring
  POST   /{id}/schedule   {scheduled_on}          payment scheduled
  POST   /{id}/snooze     {until | null}          quiet alerts until a date (Phase 2)
  DELETE /{id}                                    soft delete → status 'cancelled'
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from ..auth import CurrentUser, PortalUser, is_staff
from ..config import DEV_BYPASS_AUTH
from ..db import acquire
from ..deadline_logic import (
    action_date, add_business_days, days_remaining, next_business_day, next_due_date, today_ak,
    urgency,
)

router = APIRouter(prefix="/api/admin/deadlines", tags=["deadlines"])


# ── Staff guard ───────────────────────────────────────────────────────────────

async def _require_staff(user: CurrentUser) -> PortalUser:
    if not DEV_BYPASS_AUTH and not is_staff(user.email):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="ReCharge staff only")
    return user

StaffUser = Annotated[PortalUser, Depends(_require_staff)]


# ── Models ────────────────────────────────────────────────────────────────────

Category = Literal["payable", "state_filing", "tax", "insurance", "permit", "grant", "maintenance", "other"]
RecurrenceT = Literal["none", "monthly", "quarterly", "yearly", "biennial"]
StatusT = Literal["open", "scheduled", "done", "cancelled"]


def _clean_text(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v or None


def _check_url(v: str | None) -> str | None:
    # The UI renders this as a clickable link: only http(s), so a stored
    # javascript: URL can never run in someone's session.
    v = _clean_text(v)
    if v is not None and not v.lower().startswith(("http://", "https://")):
        raise ValueError("link must start with http:// or https://")
    return v


def _check_offsets(v: list[int] | None) -> list[int] | None:
    if v is None:
        return None
    if any(o < 0 or o > 365 for o in v):
        raise ValueError("alert offsets must be between 0 and 365 days")
    return sorted(set(v), reverse=True)


class DeadlineBase(BaseModel):
    vendor: str | None = Field(None, max_length=120)
    amount: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    notes: str | None = Field(None, max_length=4000)
    link_url: str | None = Field(None, max_length=2000)
    document_url: str | None = Field(None, max_length=2000)  # invoice file (OneDrive share link)
    external_ref: str | None = Field(None, max_length=120)

    @field_validator("vendor", "notes", "external_ref")
    @classmethod
    def _v_text(cls, v: str | None) -> str | None:
        return _clean_text(v)

    @field_validator("link_url", "document_url")
    @classmethod
    def _v_link(cls, v: str | None) -> str | None:
        return _check_url(v)

    @field_validator("alert_offsets", check_fields=False)
    @classmethod
    def _v_offsets(cls, v: list[int] | None) -> list[int] | None:
        return _check_offsets(v)


class DeadlineCreate(DeadlineBase):
    title: str = Field(..., min_length=1, max_length=200)
    category: Category
    due_date: date
    recurrence: RecurrenceT = "none"
    alert_offsets: list[int] = Field(default_factory=lambda: [14, 7, 3, 1, 0], max_length=20)
    lead_business_days: int = Field(0, ge=0, le=30)  # 7 for a Wells Fargo mailed check
    # A weekend/holiday due date moves to the next business day (KPB sales tax).
    roll_to_business_day: bool = False

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title is required")
        return v


class DeadlinePatch(DeadlineBase):
    title: str | None = Field(None, min_length=1, max_length=200)
    category: Category | None = None
    due_date: date | None = None
    recurrence: RecurrenceT | None = None
    alert_offsets: list[int] | None = Field(None, max_length=20)
    lead_business_days: int | None = Field(None, ge=0, le=30)
    roll_to_business_day: bool | None = None
    # Only 'open' is accepted: it undoes "scheduled" (or reopens a done item).
    # Done / scheduled / cancelled go through their own routes.
    status: Literal["open"] | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("title cannot be blank")
        return v


class ScheduleBody(BaseModel):
    scheduled_on: date | None = None  # defaults to today (Anchorage)


class SnoozeBody(BaseModel):
    until: date | None = None  # null clears the snooze


# ── Helpers ───────────────────────────────────────────────────────────────────

_COLS = (
    "id::text, series_id::text, title, category, vendor, amount, due_date, notes, link_url, "
    "document_url, lead_business_days, anchor_day, roll_to_business_day, "
    "external_ref, status, scheduled_on, completed_at, snoozed_until, recurrence, alert_offsets, "
    "source, created_by, created_at, updated_at"
)


def _serialize(row: asyncpg.Record, today: date) -> dict[str, Any]:
    d = dict(row)
    lead = d["lead_business_days"]
    # Everything counts down to the date something must be DONE: the mail-by
    # date for a check, otherwise the due date.
    act = action_date(d["due_date"], lead)
    days = days_remaining(act, today)
    d["action_date"] = act.isoformat()
    d["days_to_due"] = days_remaining(d["due_date"], today)
    # A scheduled check: when it should land, and whether that beats the due date.
    arrival = add_business_days(d["scheduled_on"], lead) if d["scheduled_on"] and lead else None
    d["expected_arrival"] = arrival.isoformat() if arrival else None
    d["arrives_late"] = bool(arrival and arrival > d["due_date"])
    for k in ("due_date", "scheduled_on", "snoozed_until"):
        d[k] = d[k].isoformat() if d[k] else None
    for k in ("completed_at", "created_at", "updated_at"):
        d[k] = d[k].isoformat() if d[k] else None
    d["amount"] = float(d["amount"]) if d["amount"] is not None else None
    d["alert_offsets"] = list(d["alert_offsets"] or [])
    d["days_remaining"] = days
    d["urgency"] = urgency(days)
    return d


async def _fetch_one(conn: asyncpg.Connection, deadline_id: str, *, lock: bool = False) -> asyncpg.Record:
    try:
        row = await conn.fetchrow(
            f"SELECT {_COLS} FROM admin_deadlines WHERE id = $1::uuid" + (" FOR UPDATE" if lock else ""),
            deadline_id,
        )
    except asyncpg.DataError:
        row = None  # not a uuid
    if not row:
        raise HTTPException(404, "Deadline not found")
    return row


def _db_error(e: Exception) -> HTTPException:
    if isinstance(e, asyncpg.UniqueViolationError):
        if "vendor_ref" in (e.constraint_name or ""):
            return HTTPException(409, "That vendor already has a deadline with this reference number")
        return HTTPException(409, "This series already has an item due on that date")
    if isinstance(e, asyncpg.CheckViolationError):
        return HTTPException(400, f"Invalid value ({e.constraint_name})")
    raise e


# ── Routes ────────────────────────────────────────────────────────────────────

_ALL_STATUSES = {"open", "scheduled", "done", "cancelled"}


@router.get("")
async def list_deadlines(
    _: StaffUser,
    status_: Annotated[str, Query(alias="status")] = "open,scheduled",
    category: str | None = None,
):
    statuses = [s.strip() for s in status_.split(",") if s.strip()]
    if not statuses or any(s not in _ALL_STATUSES for s in statuses):
        raise HTTPException(400, "status must be a comma list of open, scheduled, done, cancelled")

    sql = f"SELECT {_COLS} FROM admin_deadlines WHERE status = ANY($1::text[])"
    args: list[Any] = [statuses]
    if category:
        sql += " AND category = $2"
        args.append(category)
    # Live items first, by due date (overdue at the top); history after, newest first.
    sql += (
        " ORDER BY (status IN ('done','cancelled')),"
        " CASE WHEN status IN ('open','scheduled') THEN due_date END ASC,"
        " CASE WHEN status IN ('done','cancelled') THEN due_date END DESC, title"
    )
    today = today_ak()
    async with acquire() as conn:
        rows = await conn.fetch(sql, *args)
    items = [_serialize(r, today) for r in rows]
    # Live items by the date action is needed (a check due Dec 13 has to be
    # mailed Dec 2, so it sorts ahead of something due Dec 5). History keeps
    # the SQL order: newest first, after the live ones.
    live = sorted(
        (i for i in items if i["status"] in ("open", "scheduled")),
        key=lambda i: (i["action_date"], i["due_date"], i["title"]),
    )
    history = [i for i in items if i["status"] not in ("open", "scheduled")]
    return {"today": today.isoformat(), "items": live + history}


@router.post("", status_code=201)
async def create_deadline(body: DeadlineCreate, user: StaffUser):
    # The date as entered is the day the series is meant to fall on (KPB: the
    # 1st). With rolling on, this instance lands on the next business day.
    due = next_business_day(body.due_date) if body.roll_to_business_day else body.due_date
    anchor = body.due_date.day if body.recurrence != "none" else None
    async with acquire() as conn:
        try:
            row = await conn.fetchrow(
                f"""
                INSERT INTO admin_deadlines
                  (title, category, vendor, amount, due_date, notes, link_url, external_ref,
                   recurrence, alert_offsets, source, created_by, document_url, lead_business_days,
                   anchor_day, roll_to_business_day)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'manual',$11,$12,$13,$14,$15)
                RETURNING {_COLS}
                """,
                body.title, body.category, body.vendor, body.amount, due, body.notes,
                body.link_url, body.external_ref, body.recurrence, body.alert_offsets, user.email,
                body.document_url, body.lead_business_days, anchor, body.roll_to_business_day,
            )
        except (asyncpg.UniqueViolationError, asyncpg.CheckViolationError) as e:
            raise _db_error(e)
    return _serialize(row, today_ak())


@router.patch("/{deadline_id}")
async def update_deadline(deadline_id: str, body: DeadlinePatch, _: StaffUser):
    sent = body.model_fields_set
    fields: dict[str, Any] = {}
    # Nullable fields: an explicit null clears them.
    for k in ("vendor", "amount", "notes", "link_url", "document_url", "external_ref"):
        if k in sent:
            fields[k] = getattr(body, k)
    # Required fields: null means "leave alone".
    for k in ("title", "category", "due_date", "recurrence", "alert_offsets", "lead_business_days",
              "roll_to_business_day"):
        if k in sent and getattr(body, k) is not None:
            fields[k] = getattr(body, k)
    if body.status == "open":
        fields.update(status="open", scheduled_on=None, completed_at=None)
    if not fields:
        raise HTTPException(400, "No fields to update")

    async with acquire() as conn:
        async with conn.transaction():
            current = await _fetch_one(conn, deadline_id, lock=True)
            if current["status"] == "cancelled":
                raise HTTPException(409, "This deadline was deleted")
            entered = fields.get("due_date", current["due_date"])
            # Newly recurring: remember the day the series falls on.
            if fields.get("recurrence", current["recurrence"]) != "none" and current["anchor_day"] is None:
                fields["anchor_day"] = entered.day
            # Rolling applies to a newly entered date, or when switched on.
            if fields.get("roll_to_business_day", current["roll_to_business_day"]) and (
                "due_date" in fields or fields.get("roll_to_business_day")
            ):
                fields["due_date"] = next_business_day(entered)
            set_clause = ", ".join(f"{k} = ${i + 2}" for i, k in enumerate(fields))
            try:
                row = await conn.fetchrow(
                    f"UPDATE admin_deadlines SET {set_clause}, updated_at = now() "
                    f"WHERE id = $1::uuid RETURNING {_COLS}",
                    deadline_id, *fields.values(),
                )
            except (asyncpg.UniqueViolationError, asyncpg.CheckViolationError) as e:
                raise _db_error(e)
    return _serialize(row, today_ak())


@router.post("/{deadline_id}/complete")
async def complete_deadline(deadline_id: str, _: StaffUser):
    """Mark done. If recurring, insert exactly one next instance in the same
    series, in the same transaction. The row lock plus the status check make a
    double click (or two tabs) a no-op the second time, and the partial unique
    index on (series_id, due_date) backs that up at the database."""
    today = today_ak()
    async with acquire() as conn:
        async with conn.transaction():
            current = await _fetch_one(conn, deadline_id, lock=True)
            if current["status"] == "cancelled":
                raise HTTPException(409, "This deadline was deleted")
            if current["status"] == "done":
                return {"item": _serialize(current, today), "next": None}

            done = await conn.fetchrow(
                f"UPDATE admin_deadlines SET status = 'done', completed_at = now(), updated_at = now() "
                f"WHERE id = $1::uuid RETURNING {_COLS}",
                deadline_id,
            )

            nxt = None
            if current["recurrence"] != "none":
                anchor = current["anchor_day"]
                if anchor is None:  # items made before anchor_day existed
                    first = await conn.fetchval(
                        "SELECT due_date FROM admin_deadlines WHERE series_id = $1::uuid "
                        "AND status <> 'cancelled' ORDER BY due_date LIMIT 1",
                        current["series_id"],
                    )
                    anchor = first.day
                next_due = next_due_date(
                    current["due_date"], current["recurrence"], anchor,
                    roll=current["roll_to_business_day"],
                )
                # external_ref and document_url are per-instance (this cycle's
                # invoice), so they are not carried forward; neither is anything about this cycle's status.
                nxt = await conn.fetchrow(
                    f"""
                    INSERT INTO admin_deadlines
                      (series_id, title, category, vendor, amount, due_date, notes, link_url,
                       recurrence, alert_offsets, lead_business_days, source, created_by,
                       anchor_day, roll_to_business_day)
                    SELECT series_id, title, category, vendor, amount, $2, notes, link_url,
                           recurrence, alert_offsets, lead_business_days, 'recurrence', created_by,
                           $3, roll_to_business_day
                    FROM admin_deadlines WHERE id = $1::uuid
                    ON CONFLICT (series_id, due_date) WHERE status <> 'cancelled' DO NOTHING
                    RETURNING {_COLS}
                    """,
                    deadline_id, next_due, anchor,
                )
    return {
        "item": _serialize(done, today),
        "next": _serialize(nxt, today) if nxt else None,
    }


@router.post("/{deadline_id}/schedule")
async def schedule_deadline(deadline_id: str, body: ScheduleBody, _: StaffUser):
    on = body.scheduled_on or today_ak()
    async with acquire() as conn:
        async with conn.transaction():
            current = await _fetch_one(conn, deadline_id, lock=True)
            if current["status"] in ("done", "cancelled"):
                raise HTTPException(409, f"Can't schedule an item that is {current['status']}")
            row = await conn.fetchrow(
                f"UPDATE admin_deadlines SET status = 'scheduled', scheduled_on = $2, updated_at = now() "
                f"WHERE id = $1::uuid RETURNING {_COLS}",
                deadline_id, on,
            )
    return _serialize(row, today_ak())


@router.post("/{deadline_id}/snooze")
async def snooze_deadline(deadline_id: str, body: SnoozeBody, _: StaffUser):
    async with acquire() as conn:
        async with conn.transaction():
            await _fetch_one(conn, deadline_id, lock=True)
            row = await conn.fetchrow(
                f"UPDATE admin_deadlines SET snoozed_until = $2, updated_at = now() "
                f"WHERE id = $1::uuid RETURNING {_COLS}",
                deadline_id, body.until,
            )
    return _serialize(row, today_ak())


@router.delete("/{deadline_id}")
async def cancel_deadline(deadline_id: str, _: StaffUser):
    async with acquire() as conn:
        async with conn.transaction():
            await _fetch_one(conn, deadline_id, lock=True)
            row = await conn.fetchrow(
                f"UPDATE admin_deadlines SET status = 'cancelled', updated_at = now() "
                f"WHERE id = $1::uuid RETURNING {_COLS}",
                deadline_id,
            )
    return _serialize(row, today_ak())
