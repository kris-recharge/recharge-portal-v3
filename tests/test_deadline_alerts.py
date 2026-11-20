"""Deadline alerts: who receives them, and that nothing is ever sent twice."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest

from tests.conftest import TEST_DATABASE_URL, make_client

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

AK = ZoneInfo("America/Anchorage")
KRIS = "00000000-0000-0000-0000-000000000001"
STAFF2 = "00000000-0000-0000-0000-000000000002"
GONE = "00000000-0000-0000-0000-000000000003"   # deactivated staff
AEA = "00000000-0000-0000-0000-000000000004"    # external user


@pytest.fixture(autouse=True)
def people(db):
    db.execute("DELETE FROM admin_deadline_alerts; DELETE FROM admin_deadlines; "
               "DELETE FROM alert_subscriptions; DELETE FROM portal_users; DELETE FROM auth.users")
    for uid, email, active, e, p in [
        (KRIS, "Kris.Hall@rechargealaska.net", True, True, True),
        (STAFF2, "ops@rechargealaska.net", True, False, True),
        (GONE, "former@rechargealaska.net", False, True, True),
        (AEA, "someone@aidea.org", True, True, True),
    ]:
        db.execute("INSERT INTO auth.users (id, email) VALUES (%s, %s)", (uid, email))
        db.execute("INSERT INTO portal_users (email, active) VALUES (%s, %s)", (email, active))
        # Even if an external user's row somehow says "deadlines", they get nothing.
        db.execute(
            "INSERT INTO alert_subscriptions (user_id, alert_type, enabled, email_enabled, push_enabled) "
            "VALUES (%s, 'deadlines', true, %s, %s)", (uid, e, p))


def _deadline(db, **kw):
    row = {"title": "LynkWell INV1539", "category": "payable", "vendor": "LynkWell",
           "amount": 1105.29, "due_date": date(2026, 12, 13), "lead_business_days": 7,
           "status": "open", "scheduled_on": None, **kw}
    cols = ", ".join(row)
    ph = ", ".join(["%s"] * len(row))
    return db.execute(f"INSERT INTO admin_deadlines ({cols}) VALUES ({ph}) RETURNING id::text",
                      list(row.values())).fetchone()[0]


def _run(at: datetime):
    from app.deadline_alerts import check_deadline_alerts
    emails, pushes = [], []
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        n = check_deadline_alerts(
            conn, now=at,
            send_email=lambda to, subj, html: emails.append((to, subj, html)),
            send_push=lambda _c, ids, payload: pushes.append((sorted(ids), payload)),
        )
    return n, emails, pushes


def test_sends_to_subscribed_active_staff_only(db):
    did = _deadline(db)
    n, emails, pushes = _run(datetime(2026, 11, 25, 9, 0, tzinfo=AK))   # 7 days before mail-by
    assert n == 1
    assert [e[0] for e in emails] == ["Kris.Hall@rechargealaska.net"]   # ops@ is push-only
    assert emails[0][1] == "Mail check in 7 days: LynkWell INV1539"
    assert "Mail by Wed Dec 2" in emails[0][2] and "$1,105.29" in emails[0][2]
    assert f"#deadline-{did}" in emails[0][2]
    assert pushes[0][0] == sorted([KRIS, STAFF2])
    assert pushes[0][1]["url"] == f"/app/#deadline-{did}"


def test_rerun_same_day_sends_nothing(db):
    _deadline(db)
    at = datetime(2026, 11, 25, 9, 0, tzinfo=AK)
    assert _run(at)[0] == 1
    assert _run(at)[0] == 0
    assert _run(datetime(2026, 11, 25, 17, 0, tzinfo=AK))[0] == 0


def test_nothing_before_8am_anchorage(db):
    _deadline(db)
    assert _run(datetime(2026, 11, 25, 7, 59, tzinfo=AK))[0] == 0
    assert _run(datetime(2026, 11, 25, 8, 0, tzinfo=AK))[0] == 1


def test_overdue_repeats_daily_with_late_to_mail_wording(db):
    _deadline(db)
    n1, e1, _ = _run(datetime(2026, 12, 3, 9, 0, tzinfo=AK))
    n2, e2, _ = _run(datetime(2026, 12, 4, 9, 0, tzinfo=AK))
    assert n1 == n2 == 1
    assert e1[0][1] == "⚠ Late to mail: LynkWell INV1539"
    n3, e3, _ = _run(datetime(2026, 12, 14, 9, 0, tzinfo=AK))
    assert e3[0][1] == "⚠ OVERDUE: LynkWell INV1539"


def test_scheduled_check_only_gets_confirm_on_due_date(db):
    _deadline(db, status="scheduled", scheduled_on=date(2026, 12, 1))
    assert _run(datetime(2026, 12, 3, 9, 0, tzinfo=AK))[0] == 0
    n, emails, _ = _run(datetime(2026, 12, 13, 9, 0, tzinfo=AK))
    assert n == 1 and emails[0][1] == "Confirm: LynkWell INV1539"


def test_quiet_when_nothing_is_due(db):
    _deadline(db, due_date=date(2027, 6, 1))
    assert _run(datetime(2026, 11, 25, 9, 0, tzinfo=AK)) == (0, [], [])


def test_html_is_escaped(db):
    _deadline(db, title="<script>x</script>", due_date=date(2026, 11, 25), lead_business_days=0)
    _, emails, _ = _run(datetime(2026, 11, 25, 9, 0, tzinfo=AK))
    assert "<script>" not in emails[0][2]


# ── Alerts tab: the type is staff-only ────────────────────────────────────────

def test_external_user_cannot_see_or_subscribe_to_deadlines(db):
    with make_client("someone@aidea.org", user_id=AEA) as c:
        db.execute("DELETE FROM alert_subscriptions WHERE user_id = %s", (AEA,))
        types = [s["alert_type"] for s in c.get("/api/alerts/subscriptions").json()["subscriptions"]]
        assert "deadlines" not in types and "fault" in types
        c.post("/api/alerts/subscriptions", json=[
            {"alert_type": "deadlines", "enabled": True, "email_enabled": True, "push_enabled": True}])
    assert db.execute("SELECT count(*) FROM alert_subscriptions WHERE user_id = %s",
                      (AEA,)).fetchone()[0] == 0


def test_staff_can_subscribe_to_deadlines(db):
    with make_client("ops@rechargealaska.net", user_id=STAFF2) as c:
        r = c.post("/api/alerts/subscriptions", json=[
            {"alert_type": "deadlines", "enabled": True, "email_enabled": True, "push_enabled": False}])
        subs = {s["alert_type"]: s for s in r.json()["subscriptions"]}
    assert subs["deadlines"]["enabled"] and subs["deadlines"]["email_enabled"]
