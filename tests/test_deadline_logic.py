"""Pure date logic for Admin Deadlines — no database needed."""

from datetime import date, datetime, timezone

import pytest

from app.deadline_logic import days_remaining, next_due_date, today_ak, urgency


# ── Anchorage "today" around midnight ─────────────────────────────────────────

def test_today_is_anchorage_date_not_utc():
    # 2026-12-13 07:30 UTC is 2026-12-12 22:30 in Anchorage (AKST, UTC-9).
    assert today_ak(datetime(2026, 12, 13, 7, 30, tzinfo=timezone.utc)) == date(2026, 12, 12)
    # One minute past Anchorage midnight → the new day.
    assert today_ak(datetime(2026, 12, 13, 9, 1, tzinfo=timezone.utc)) == date(2026, 12, 13)
    # One minute before.
    assert today_ak(datetime(2026, 12, 13, 8, 59, tzinfo=timezone.utc)) == date(2026, 12, 12)


def test_today_respects_daylight_time():
    # Summer: AKDT is UTC-8, so midnight is 08:00 UTC.
    assert today_ak(datetime(2026, 7, 1, 7, 59, tzinfo=timezone.utc)) == date(2026, 6, 30)
    assert today_ak(datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)) == date(2026, 7, 1)


def test_due_today_late_evening_is_still_today():
    # 11:30 pm in Anchorage on the due date: 0 days left, not overdue —
    # even though UTC has already rolled over to the next day.
    now = datetime(2026, 12, 14, 8, 30, tzinfo=timezone.utc)  # 23:30 Dec 13 AKST
    d = days_remaining(date(2026, 12, 13), today_ak(now))
    assert d == 0 and urgency(d) == "critical"


# ── Urgency bands ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("days,expected", [
    (-30, "overdue"), (-1, "overdue"),
    (0, "critical"), (7, "critical"),
    (8, "soon"), (30, "soon"),
    (31, "later"), (400, "later"),
])
def test_urgency_bands(days, expected):
    assert urgency(days) == expected


# ── Recurrence + month-end clamping ───────────────────────────────────────────

def test_monthly_clamps_jan31_to_feb_end():
    assert next_due_date(date(2027, 1, 31), "monthly", 31) == date(2027, 2, 28)
    assert next_due_date(date(2028, 1, 31), "monthly", 31) == date(2028, 2, 29)  # leap year


def test_monthly_returns_to_anchor_after_short_month():
    # Without the anchor, Jan 31 → Feb 28 → Mar 28 would drift earlier forever.
    assert next_due_date(date(2027, 2, 28), "monthly", 31) == date(2027, 3, 31)
    assert next_due_date(date(2027, 3, 31), "monthly", 31) == date(2027, 4, 30)
    assert next_due_date(date(2027, 4, 30), "monthly", 31) == date(2027, 5, 31)
    assert next_due_date(date(2027, 2, 28), "monthly", 30) == date(2027, 3, 30)


def test_hand_moved_instance_becomes_new_anchor():
    # Series was set up on the 31st, but someone moved this one to the 15th.
    assert next_due_date(date(2027, 3, 15), "monthly", 31) == date(2027, 4, 15)


def test_month_end_without_anchor_keeps_own_day():
    assert next_due_date(date(2027, 1, 15), "monthly") == date(2027, 2, 15)


def test_quarterly_crosses_year():
    assert next_due_date(date(2026, 11, 30), "quarterly", 30) == date(2027, 2, 28)
    assert next_due_date(date(2027, 2, 28), "quarterly", 30) == date(2027, 5, 30)


def test_yearly_and_biennial_leap_day():
    assert next_due_date(date(2028, 2, 29), "yearly", 29) == date(2029, 2, 28)
    assert next_due_date(date(2031, 2, 28), "yearly", 29) == date(2032, 2, 29)
    assert next_due_date(date(2026, 1, 2), "biennial", 2) == date(2028, 1, 2)


def test_none_is_not_a_period():
    with pytest.raises(ValueError):
        next_due_date(date(2026, 1, 1), "none")


# ── Business days / mailed checks ─────────────────────────────────────────────

from app.deadline_logic import (  # noqa: E402
    action_date, add_business_days, federal_holidays, is_business_day, plan_alert,
)


def test_federal_holidays_2026():
    h = federal_holidays(2026)
    assert date(2026, 1, 19) in h      # MLK
    assert date(2026, 5, 25) in h      # Memorial Day
    assert date(2026, 11, 26) in h     # Thanksgiving
    assert date(2026, 7, 3) in h       # July 4 is a Saturday → observed Friday
    assert date(2026, 11, 27) not in h  # day after Thanksgiving is a business day


def test_new_year_on_saturday_observed_previous_dec_31():
    assert date(2021, 12, 31) in federal_holidays(2021)  # Jan 1 2022 was a Saturday


def test_lynkwell_check_mail_by_date():
    # INV1539 due Sun Dec 13 2026. Last business day on/before = Fri Dec 11;
    # 7 business days earlier = Wed Dec 2. Mailed then, it lands Fri Dec 11.
    assert action_date(date(2026, 12, 13), 7) == date(2026, 12, 2)
    assert add_business_days(date(2026, 12, 2), 7) == date(2026, 12, 11)


def test_mail_by_skips_thanksgiving():
    # Due Fri Dec 4 2026: back 7 business days skips Thanksgiving (Nov 26)
    # and the weekend → Tue Nov 24.
    assert action_date(date(2026, 12, 4), 7) == date(2026, 11, 24)


def test_mail_by_skips_christmas_and_new_year():
    # Due Mon Jan 4 2027: Jan 1 (Fri) and Dec 25 (Fri) are holidays → Tue Dec 22.
    assert action_date(date(2027, 1, 4), 7) == date(2026, 12, 22)


def test_no_lead_time_means_due_date_even_on_weekend():
    assert action_date(date(2026, 12, 13), 0) == date(2026, 12, 13)
    assert not is_business_day(date(2026, 12, 13))


# ── Alert planning ────────────────────────────────────────────────────────────

def _plan(today, *, status="open", due=date(2026, 12, 13), lead=7,
          offsets=(14, 7, 3, 1, 0), snoozed=None):
    return plan_alert(status=status, due=due, lead_business_days=lead,
                      alert_offsets=list(offsets), snoozed_until=snoozed, today=today)


def test_countdown_runs_to_mail_by_date():
    # Mail-by is Dec 2.
    assert _plan(date(2026, 11, 17)) is None                                  # 15 days out
    assert _plan(date(2026, 11, 20)) == ("T-14@2026-12-02", "countdown")      # 12 days out
    assert _plan(date(2026, 11, 25)) == ("T-7@2026-12-02", "countdown")
    assert _plan(date(2026, 11, 29)) == ("T-3@2026-12-02", "countdown")
    assert _plan(date(2026, 12, 2)) == ("T-0@2026-12-02", "countdown")


def test_after_mail_by_date_alerts_daily():
    assert _plan(date(2026, 12, 3)) == ("OVERDUE-2026-12-03", "overdue")
    assert _plan(date(2026, 12, 4)) == ("OVERDUE-2026-12-04", "overdue")


def test_snooze_through_today_silences():
    assert _plan(date(2026, 12, 3), snoozed=date(2026, 12, 3)) is None
    assert _plan(date(2026, 12, 4), snoozed=date(2026, 12, 3)) is not None


def test_scheduled_only_confirms_on_due_date():
    assert _plan(date(2026, 12, 3), status="scheduled") is None
    assert _plan(date(2026, 12, 13), status="scheduled") == ("CONFIRM@2026-12-13", "confirm")


def test_done_and_cancelled_never_alert():
    assert _plan(date(2026, 12, 20), status="done") is None
    assert _plan(date(2026, 12, 20), status="cancelled") is None


def test_moving_the_due_date_rearms_the_countdown():
    a = _plan(date(2026, 11, 25))
    b = _plan(date(2026, 11, 25), due=date(2026, 12, 14))  # Mon → mail by Thu Dec 3
    assert a[0] != b[0]
