"""Admin Deadlines alerts: a quiet daily reminder until the item is handled.

Runs inside the existing alert thread (alerts.py), so there is no separate
scheduler to deploy. From 08:00 Anchorage time onward it checks every open or
scheduled deadline once per pass; each alert has a key that is claimed in
admin_deadline_alerts BEFORE it is sent, so hourly passes, restarts and a
second container can never send the same alert twice.

Who gets it: people who switched on "Deadlines" in the Alerts tab, using the
same Email / Push channel choices as every other alert, and ONLY if their
login email is @rechargealaska.net. External users (AEA, site hosts) can't
subscribe (alerts_config.py hides and rejects the type) and are filtered out
here again regardless. Deadline alerts are deliberately not written to
fired_alerts, whose History view is not limited to staff.

What gets sent (deadline_logic.plan_alert): countdown alerts at the item's
alert offsets before its ACTION date (the mail-by date for a mailed check),
then one alert a day once that date has passed, and a single "confirm it
arrived" on the due date for anything marked scheduled. Nothing on days
with nothing due: no digest.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from html import escape
from typing import Callable

import psycopg

from .auth import is_staff
from .config import PORTAL_URL
from .deadline_logic import AK_TZ, action_date, plan_alert
from .push import push_available, send_to_subscriptions

logger = logging.getLogger("rca.deadline_alerts")

ALERT_TYPE = "deadlines"
SEND_FROM_HOUR_AK = 8       # nothing goes out before 08:00 Anchorage
CHANNEL = "subscribers"     # one claim per alert, whoever it went to

EmailSender = Callable[[str, str, str], None]           # (to, subject, html)
PushSender = Callable[[object, list[str], dict], None]  # (conn, user_ids, payload)

_warned_missing_table = False


# ── Formatting ────────────────────────────────────────────────────────────────

def _day(d: date) -> str:
    return f"{d:%a} {d:%b} {d.day}"          # "Wed Dec 2"


def _usd(v) -> str:
    return f"${float(v):,.2f}" if v is not None else ""


def compose(item: dict, kind: str, today: date) -> tuple[str, str]:
    """(headline, detail line) for one alert. Plain text; escaped for email later."""
    title = item["title"]
    due: date = item["due_date"]
    lead = item["lead_business_days"]
    act = action_date(due, lead)
    amount = _usd(item["amount"])
    money = f" · {amount}" if amount else ""
    is_check = lead > 0

    if kind == "confirm":
        sched = item.get("scheduled_on")
        how = f"Check sent {_day(sched)}" if is_check and sched else "Payment scheduled"
        return (f"Confirm: {title}",
                f"Due today ({_day(due)}). {how}. Confirm it arrived, then mark it done.{money}")

    if kind == "overdue":
        if is_check and due >= today:
            return (f"⚠ Late to mail: {title}",
                    f"Check should have been mailed by {_day(act)} to arrive by {_day(due)}. "
                    f"Mail it today or pay another way.{money}")
        late = (today - due).days
        return (f"⚠ OVERDUE: {title}",
                f"Was due {_day(due)} ({late} day{'s' if late != 1 else ''} ago).{money}")

    days = (act - today).days
    when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
    if is_check:
        return (f"Mail check {when}: {title}",
                f"Mail by {_day(act)} ({lead} business days in the mail) · due {_day(due)}{money}")
    return (f"Due {when}: {title}", f"Due {_day(due)}{money}")


def _email_html(item: dict, headline: str, detail: str) -> str:
    link = f"{PORTAL_URL}/#deadline-{item['id']}"
    buttons = [f'<a href="{escape(link)}">Open in dashboard</a>']
    if item.get("document_url"):
        buttons.append(f'<a href="{escape(item["document_url"])}">Invoice</a>')
    if item.get("link_url"):
        buttons.append(f'<a href="{escape(item["link_url"])}">Payment / filing link</a>')
    vendor = f"<p style='color:#6b7280;margin:0'>{escape(item['vendor'])}</p>" if item.get("vendor") else ""
    return (
        "<div style='font-family:Arial,sans-serif;font-size:14px;color:#111827'>"
        f"<h3 style='margin:0 0 4px'>{escape(headline)}</h3>{vendor}"
        f"<p>{escape(detail)}</p>"
        f"<p>{' &nbsp;·&nbsp; '.join(buttons)}</p>"
        "<p style='color:#9ca3af;font-size:12px'>ReCharge Alaska deadline reminder. "
        "It repeats until the item is marked done or scheduled. "
        "Change delivery in the dashboard's Alerts tab.</p></div>"
    )


# ── Delivery (defaults; tests pass fakes) ─────────────────────────────────────

def _default_email(to: str, subject: str, html: str) -> None:
    from .alerts import _send_email_to  # noqa: PLC0415 — alerts imports this module
    _send_email_to(to, subject, html)


def _default_push(conn, user_ids: list[str], payload: dict) -> None:
    if not push_available() or not user_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id::text, endpoint, p256dh, auth FROM push_subscriptions "
            "WHERE user_id = ANY(%s::uuid[])",
            (user_ids,),
        )
        subs = [{"user_id": r[0], "endpoint": r[1], "p256dh": r[2], "auth": r[3]}
                for r in cur.fetchall()]
    if subs:
        send_to_subscriptions(conn, subs, json.dumps(payload))


# ── Recipients ────────────────────────────────────────────────────────────────

def _recipients(conn) -> list[tuple[str, str, bool, bool]]:
    """(auth uid, email, email_on, push_on) for staff subscribed to deadlines."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asub.user_id::text, au.email, asub.email_enabled, asub.push_enabled
            FROM alert_subscriptions asub
            JOIN auth.users au ON au.id = asub.user_id   -- user_id is the AUTH uid
            LEFT JOIN portal_users pu ON lower(pu.email) = lower(au.email)
            WHERE asub.alert_type = %s
              AND asub.enabled = true
              AND COALESCE(pu.active, true)               -- a deactivated user gets nothing
            """,
            (ALERT_TYPE,),
        )
        rows = cur.fetchall()
    return [(uid, email, bool(e), bool(p)) for uid, email, e, p in rows if is_staff(email)]


# ── Main entry point (called from the alert thread) ───────────────────────────

def check_deadline_alerts(
    conn,
    *,
    now: datetime | None = None,
    send_email: EmailSender = _default_email,
    send_push: PushSender = _default_push,
) -> int:
    """Send whatever deadline alerts are due. Returns how many alerts went out."""
    global _warned_missing_table
    now_ak = (now or datetime.now(AK_TZ)).astimezone(AK_TZ)
    if now_ak.hour < SEND_FROM_HOUR_AK:
        return 0
    today = now_ak.date()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id::text, title, vendor, amount, due_date, status, scheduled_on,
                       snoozed_until, lead_business_days, alert_offsets, link_url, document_url
                FROM admin_deadlines
                WHERE status IN ('open', 'scheduled')
                ORDER BY due_date
                """
            )
            cols = [c.name for c in cur.description]
            items = [dict(zip(cols, r)) for r in cur.fetchall()]
    except psycopg.errors.UndefinedTable:
        # Code deployed before migrations/20261120_admin_deadlines.sql was applied.
        if not _warned_missing_table:
            logger.warning("admin_deadlines table missing — deadline alerts off until the migration is applied")
            _warned_missing_table = True
        conn.rollback()
        return 0

    planned = []
    for it in items:
        p = plan_alert(
            status=it["status"], due=it["due_date"], lead_business_days=it["lead_business_days"],
            alert_offsets=list(it["alert_offsets"] or []), snoozed_until=it["snoozed_until"],
            today=today,
        )
        if p:
            planned.append((it, *p))
    if not planned:
        return 0

    recipients = _recipients(conn)
    sent = 0
    for item, key, kind in planned:
        # Claim first: only the run that inserts the row sends the alert.
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO admin_deadline_alerts (deadline_id, alert_key, channel) "
                "VALUES (%s::uuid, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                (item["id"], key, CHANNEL),
            )
            claimed = cur.fetchone() is not None
        conn.commit()
        if not claimed:
            continue

        headline, detail = compose(item, kind, today)
        html = _email_html(item, headline, detail)
        for _uid, email, email_on, _push_on in recipients:
            if email_on:
                send_email(email, headline, html)
        push_ids = [uid for uid, _e, _eo, push_on in recipients if push_on]
        if push_ids:
            try:
                send_push(conn, push_ids, {
                    "alert_type": ALERT_TYPE,
                    "title": headline,
                    "evse_name": item.get("vendor") or "",
                    "message": detail,
                    "timestamp_ak": now_ak.strftime("%Y-%m-%d %H:%M AKT"),
                    # One notification per deadline on the lock screen, replaced by the next.
                    "tag": f"deadline:{item['id']}",
                    "url": f"/app/#deadline-{item['id']}",
                })
            except Exception as exc:  # push must never block email or the loop
                logger.error("Deadline push failed for %s: %s", item["id"], exc)
        logger.info("Deadline alert %s (%s) → %d recipient(s)", key, item["title"], len(recipients))
        sent += 1
    return sent
