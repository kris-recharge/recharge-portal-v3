"""Pure date logic for Admin Deadlines: no DB, no config, so it is easy to test.

Every due date is a calendar date in America/Anchorage. "Today" is the
Anchorage date, never the server's (the VPS runs on UTC, where it is already
tomorrow from 3 or 4 pm Alaska time).
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

AK_TZ = ZoneInfo("America/Anchorage")

Recurrence = Literal["none", "monthly", "quarterly", "yearly", "biennial"]
Urgency = Literal["overdue", "critical", "soon", "later"]

PERIOD_MONTHS: dict[str, int] = {
    "monthly": 1,
    "quarterly": 3,
    "yearly": 12,
    "biennial": 24,
}


def today_ak(now: datetime | None = None) -> date:
    """Today's date in Anchorage. `now` must be tz-aware when given (for tests)."""
    now = now or datetime.now(AK_TZ)
    return now.astimezone(AK_TZ).date()


def days_remaining(due: date, today: date) -> int:
    return (due - today).days


def urgency(days: int) -> Urgency:
    if days < 0:
        return "overdue"
    if days <= 7:
        return "critical"
    if days <= 30:
        return "soon"
    return "later"


def _month_len(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


# ── Business days (for mailed checks) ─────────────────────────────────────────
# A Wells Fargo Bill Pay check takes ~7 business days to reach the payee. Mail
# and banks don't move on weekends or federal holidays, so the "mail by" date
# is counted back in business days from the due date.

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th (1-based) given weekday (Mon=0) of a month; n=-1 means the last."""
    if n > 0:
        first = date(year, month, 1)
        return date(year, month, 1 + (weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year, month, _month_len(year, month))
    return date(year, month, last.day - (last.weekday() - weekday) % 7)


def federal_holidays(year: int) -> set[date]:
    """US federal holidays, with BOTH observance rules applied.

    Banks (Federal Reserve) don't move a Saturday holiday to Friday; the
    Postal Service does. Treating both as non-business days errs a day early,
    which is the safe direction for a payment.
    """
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4),
             date(year, 11, 11), date(year, 12, 25)]
    floating = [
        _nth_weekday(year, 1, 0, 3),   # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),   # Presidents' Day
        _nth_weekday(year, 5, 0, -1),  # Memorial Day
        _nth_weekday(year, 9, 0, 1),   # Labor Day
        _nth_weekday(year, 10, 0, 2),  # Columbus Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
    ]
    out = set(fixed) | set(floating)
    for d in fixed:
        if d.weekday() == 5:
            out.add(d - timedelta(days=1))   # Saturday → observed Friday
        elif d.weekday() == 6:
            out.add(d + timedelta(days=1))   # Sunday → observed Monday
    # Jan 1 on a Saturday is observed on Dec 31 of the year before.
    nxt = date(year + 1, 1, 1)
    if nxt.weekday() == 5:
        out.add(nxt - timedelta(days=1))
    return out


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in federal_holidays(d.year)


def add_business_days(start: date, n: int) -> date:
    """The date n business days after `start` (start itself not counted)."""
    d = start
    while n > 0:
        d += timedelta(days=1)
        if is_business_day(d):
            n -= 1
    return d


def subtract_business_days(start: date, n: int) -> date:
    d = start
    while n > 0:
        d -= timedelta(days=1)
        if is_business_day(d):
            n -= 1
    return d


def action_date(due: date, lead_business_days: int) -> date:
    """The date something has to be DONE by for it to land by `due`.

    With no lead time that is the due date itself. With a lead time (a mailed
    check), it is the last business day on or before the due date, minus the
    lead: mail it then and it arrives by the due date even if the due date is a
    weekend. E.g. due Sun Dec 13 2026, 7 days → mail by Wed Dec 2.
    """
    if lead_business_days <= 0:
        return due
    last = due
    while not is_business_day(last):
        last -= timedelta(days=1)
    return subtract_business_days(last, lead_business_days)


def next_business_day(d: date) -> date:
    """`d` itself if it is a business day, else the next one. Government
    offices (KPB sales tax, for one) push a weekend or holiday due date to the
    next business day: the 1st of May 2027 is a Saturday, so it's due Mon May 3.
    Uses federal holidays; the 1st-of-month KPB dates never hit an
    Alaska-only holiday (Seward's Day, Alaska Day)."""
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def _on_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, _month_len(year, month)))


def _add_months(d: date, months: int, day: int) -> date:
    idx = d.month - 1 + months
    return _on_day(d.year + idx // 12, idx % 12 + 1, day)


def next_due_date(
    current: date, recurrence: str, anchor_day: int | None = None, roll: bool = False,
) -> date:
    """Move `current` forward one period, clamping to the end of short months.

    `anchor_day` is the day-of-month the series is meant to fall on (the day of
    its first instance). Without it, Jan 31 → Feb 28 → Mar 28 would drift
    earlier every month. With it, Jan 31 → Feb 28 → Mar 31.

    If `current` is not on the anchor day (or its clamped form), someone moved
    this instance by hand, so its own day becomes the anchor from here on.

    With `roll`, a due date that lands on a weekend or holiday moves to the
    next business day, and the series keeps counting from the date it was
    meant to fall on: KPB's quarterly return is due the 1st, so Feb 1 → May 1
    (Sat) → Mon May 3 → Aug 1 (Sun) → Mon Aug 2 → Nov 1, never drifting to the 3rd.
    """
    months = PERIOD_MONTHS.get(recurrence)
    if months is None:
        raise ValueError(f"not a recurring period: {recurrence!r}")

    if roll and anchor_day is not None:
        # Which anchor date did `current` roll forward from? Usually the same
        # month; for an anchor near month end it can be the month before.
        prev = date(current.year, current.month, 1) - timedelta(days=1)
        for y, m in ((current.year, current.month), (prev.year, prev.month)):
            meant = _on_day(y, m, anchor_day)
            if meant <= current and next_business_day(meant) == current:
                return next_business_day(_add_months(meant, months, anchor_day))
        # Moved by hand: carry on from its own day.
        return next_business_day(_add_months(current, months, current.day))

    day = current.day
    if anchor_day is not None:
        clamped_anchor = min(anchor_day, _month_len(current.year, current.month))
        if current.day == clamped_anchor:
            day = anchor_day
    return _add_months(current, months, day)


# ── Which alert (if any) is due today ─────────────────────────────────────────

AlertKind = Literal["countdown", "overdue", "confirm"]


def plan_alert(
    *,
    status: str,
    due: date,
    lead_business_days: int,
    alert_offsets: list[int],
    snoozed_until: date | None,
    today: date,
) -> tuple[str, AlertKind] | None:
    """Return (alert_key, kind) for the alert this item should send today, or None.

    Stateless: it names the alert, and the caller claims the key in
    admin_deadline_alerts before sending, so re-running on the same day (or
    every hour) never sends twice.

    - open: counts down to the ACTION date (the mail-by date for a check).
      Sends the tightest offset already reached, so a missed day or an item
      added 5 days out still gets one alert, not a burst of stale ones. The key
      carries the action date, so moving the due date re-arms the countdown.
    - open and past the action date: one high-priority alert every day.
    - scheduled: no countdown; one "confirm it arrived" alert on the due date.
    - snoozed through today, done, cancelled: nothing.
    """
    if status not in ("open", "scheduled"):
        return None
    if snoozed_until is not None and snoozed_until >= today:
        return None

    if status == "scheduled":
        if (due - today).days <= 0:
            return f"CONFIRM@{due.isoformat()}", "confirm"
        return None

    act = action_date(due, lead_business_days)
    days = (act - today).days
    if days < 0:
        return f"OVERDUE-{today.isoformat()}", "overdue"
    reached = [o for o in alert_offsets if o >= days]
    if not reached:
        return None
    return f"T-{min(reached)}@{act.isoformat()}", "countdown"
