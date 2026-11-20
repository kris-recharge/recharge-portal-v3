"""Admin Deadlines API against a throwaway Postgres (see conftest.py)."""

import psycopg
import pytest

from tests.conftest import TEST_DATABASE_URL, make_client

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


def _client(email):
    return make_client(email)


@pytest.fixture(scope="module")
def staff(db):
    with _client("Kris.Hall@ReChargeAlaska.net") as c:
        yield c


@pytest.fixture(autouse=True)
def _clean(db):
    db.execute("DELETE FROM admin_deadlines")


def _create(client, **kw):
    body = {"title": "Test", "category": "payable", "due_date": "2026-12-13", **kw}
    r = client.post("/api/admin/deadlines", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ── Access control ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("email", [
    "someone@aidea.org",                 # AEA-style external user
    "kris.hall@rechargealaska.net.evil.com",
    "x@notrechargealaska.net",
    "x@sub.rechargealaska.net",
])
def test_non_staff_gets_403_on_every_route(db, email):
    with _client(email) as c:
        assert c.get("/api/admin/deadlines").status_code == 403
        assert c.post("/api/admin/deadlines", json={}).status_code == 403
        some_id = "00000000-0000-0000-0000-000000000000"
        assert c.patch(f"/api/admin/deadlines/{some_id}", json={"title": "x"}).status_code == 403
        assert c.post(f"/api/admin/deadlines/{some_id}/complete").status_code == 403
        assert c.post(f"/api/admin/deadlines/{some_id}/schedule", json={}).status_code == 403
        assert c.post(f"/api/admin/deadlines/{some_id}/snooze", json={}).status_code == 403
        assert c.delete(f"/api/admin/deadlines/{some_id}").status_code == 403
        assert c.get("/api/me").json()["is_staff"] is False


def test_unauthenticated_gets_401(db):
    with _client(None) as c:
        assert c.get("/api/admin/deadlines").status_code == 401


def test_me_reports_staff(staff):
    assert staff.get("/api/me").json()["is_staff"] is True


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_data_api_roles_cannot_read_tables(db, staff, role):
    _create(staff, title="Secret invoice", amount=1105.29)
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        conn.execute(f"SET ROLE {role}")
        for table in ("admin_deadlines", "admin_deadline_alerts"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"SELECT * FROM {table}")
            conn.rollback()
            conn.execute(f"SET ROLE {role}")


def test_rls_enabled_and_no_policies(db):
    rows = db.execute(
        "SELECT relname, relrowsecurity FROM pg_class "
        "WHERE relname IN ('admin_deadlines','admin_deadline_alerts')"
    ).fetchall()
    assert all(rls for _, rls in rows) and len(rows) == 2
    assert db.execute(
        "SELECT count(*) FROM pg_policies WHERE tablename LIKE 'admin_deadline%'"
    ).fetchone()[0] == 0


# ── CRUD + validation ─────────────────────────────────────────────────────────

def test_create_and_list_sorted_with_urgency(staff):
    _create(staff, title="Later", due_date="2099-01-01")
    _create(staff, title="Overdue", due_date="2000-01-01")
    r = staff.get("/api/admin/deadlines").json()
    titles = [i["title"] for i in r["items"]]
    assert titles == ["Overdue", "Later"]
    assert r["items"][0]["urgency"] == "overdue" and r["items"][0]["days_remaining"] < 0
    assert r["items"][1]["urgency"] == "later"
    assert "today" in r


def test_rejects_javascript_link(staff):
    r = staff.post("/api/admin/deadlines", json={
        "title": "x", "category": "payable", "due_date": "2026-12-13",
        "link_url": "javascript:alert(1)",
    })
    assert r.status_code == 422


def test_duplicate_vendor_ref_is_409(staff):
    _create(staff, vendor="LynkWell", external_ref="INV1539")
    r = staff.post("/api/admin/deadlines", json={
        "title": "dup", "category": "payable", "due_date": "2026-12-13",
        "vendor": "LynkWell", "external_ref": "INV1539",
    })
    assert r.status_code == 409


def test_category_filter_and_show_completed(staff):
    a = _create(staff, category="tax")
    _create(staff, category="payable")
    staff.post(f"/api/admin/deadlines/{a['id']}/complete")
    assert staff.get("/api/admin/deadlines?category=tax").json()["items"] == []
    hist = staff.get("/api/admin/deadlines?status=open,scheduled,done&category=tax").json()["items"]
    assert [i["status"] for i in hist] == ["done"]


def test_schedule_snooze_reopen_cancel(staff):
    d = _create(staff)
    r = staff.post(f"/api/admin/deadlines/{d['id']}/schedule", json={"scheduled_on": "2026-12-10"}).json()
    assert r["status"] == "scheduled" and r["scheduled_on"] == "2026-12-10"
    r = staff.post(f"/api/admin/deadlines/{d['id']}/snooze", json={"until": "2026-12-05"}).json()
    assert r["snoozed_until"] == "2026-12-05" and r["status"] == "scheduled"
    r = staff.patch(f"/api/admin/deadlines/{d['id']}", json={"status": "open"}).json()
    assert r["status"] == "open" and r["scheduled_on"] is None
    assert staff.delete(f"/api/admin/deadlines/{d['id']}").json()["status"] == "cancelled"
    assert staff.get("/api/admin/deadlines").json()["items"] == []


def test_patch_clears_nullable_field(staff):
    d = _create(staff, amount=10, vendor="KPB")
    r = staff.patch(f"/api/admin/deadlines/{d['id']}", json={"amount": None}).json()
    assert r["amount"] is None and r["vendor"] == "KPB"


def test_bad_id_is_404(staff):
    assert staff.post("/api/admin/deadlines/not-a-uuid/complete").status_code == 404


# ── Recurrence ────────────────────────────────────────────────────────────────

def test_complete_recurring_spawns_exactly_one_next(staff, db):
    d = _create(staff, title="KPB sales tax", category="tax", recurrence="quarterly",
                due_date="2026-11-30", external_ref="Q3-2026", vendor="KPB")
    r1 = staff.post(f"/api/admin/deadlines/{d['id']}/complete").json()
    assert r1["item"]["status"] == "done"
    nxt = r1["next"]
    assert nxt["due_date"] == "2027-02-28"
    assert nxt["series_id"] == d["series_id"] and nxt["source"] == "recurrence"
    assert nxt["external_ref"] is None  # invoice/filing refs are per-instance

    # Completing again (double click, second tab) creates nothing.
    r2 = staff.post(f"/api/admin/deadlines/{d['id']}/complete").json()
    assert r2["next"] is None
    count = db.execute("SELECT count(*) FROM admin_deadlines WHERE series_id = %s",
                       (d["series_id"],)).fetchone()[0]
    assert count == 2

    # The next one keeps the series' anchor day: Feb 28 → May 30, not May 28.
    r3 = staff.post(f"/api/admin/deadlines/{nxt['id']}/complete").json()
    assert r3["next"]["due_date"] == "2027-05-30"


def test_reopen_and_recomplete_does_not_duplicate(staff, db):
    d = _create(staff, recurrence="monthly", due_date="2027-01-31")
    staff.post(f"/api/admin/deadlines/{d['id']}/complete")
    staff.patch(f"/api/admin/deadlines/{d['id']}", json={"status": "open"})
    r = staff.post(f"/api/admin/deadlines/{d['id']}/complete").json()
    assert r["next"] is None
    dues = [x[0].isoformat() for x in db.execute(
        "SELECT due_date FROM admin_deadlines WHERE series_id = %s ORDER BY due_date",
        (d["series_id"],)).fetchall()]
    assert dues == ["2027-01-31", "2027-02-28"]


def test_non_recurring_complete_has_no_next(staff):
    d = _create(staff)
    assert staff.post(f"/api/admin/deadlines/{d['id']}/complete").json()["next"] is None


# ── Mailed checks ─────────────────────────────────────────────────────────────

def test_mailed_check_counts_down_to_mail_by_date(staff):
    d = _create(staff, due_date="2026-12-13", lead_business_days=7,
                document_url="https://rechargealaska.sharepoint.com/inv1539.pdf")
    assert d["action_date"] == "2026-12-02"
    assert d["days_to_due"] - d["days_remaining"] == 11
    assert d["document_url"].endswith("inv1539.pdf")


def test_check_sorts_by_mail_by_date(staff):
    _create(staff, title="Online, due Dec 5", due_date="2026-12-05")
    _create(staff, title="Check, due Dec 13", due_date="2026-12-13", lead_business_days=7)
    assert [i["title"] for i in staff.get("/api/admin/deadlines").json()["items"]] == \
        ["Check, due Dec 13", "Online, due Dec 5"]


def test_scheduled_check_reports_arrival_and_lateness(staff):
    d = _create(staff, due_date="2026-12-13", lead_business_days=7)
    ok = staff.post(f"/api/admin/deadlines/{d['id']}/schedule", json={"scheduled_on": "2026-12-02"}).json()
    assert ok["expected_arrival"] == "2026-12-11" and ok["arrives_late"] is False
    late = staff.post(f"/api/admin/deadlines/{d['id']}/schedule", json={"scheduled_on": "2026-12-07"}).json()
    assert late["expected_arrival"] == "2026-12-16" and late["arrives_late"] is True


def test_rejects_unsafe_document_link(staff):
    r = staff.post("/api/admin/deadlines", json={
        "title": "x", "category": "payable", "due_date": "2026-12-13",
        "document_url": "javascript:alert(1)"})
    assert r.status_code == 422


def test_recurrence_keeps_lead_time_but_not_invoice_file(staff):
    d = _create(staff, recurrence="monthly", lead_business_days=7,
                document_url="https://example.com/nov.pdf")
    nxt = staff.post(f"/api/admin/deadlines/{d['id']}/complete").json()["next"]
    assert nxt["lead_business_days"] == 7 and nxt["document_url"] is None


# ── Government calendars ──────────────────────────────────────────────────────

def test_kpb_quarterly_rolls_and_repeats_forever(staff):
    # Entered as the normal date; May 1 2027 is a Saturday.
    d = _create(staff, title="KPB sales tax (quarterly)", category="tax", recurrence="quarterly",
                due_date="2027-05-01", roll_to_business_day=True)
    assert d["due_date"] == "2027-05-03" and d["anchor_day"] == 1
    dues = []
    for _ in range(3):
        d = staff.post(f"/api/admin/deadlines/{d['id']}/complete").json()["next"]
        dues.append(d["due_date"])
    assert dues == ["2027-08-02", "2027-11-01", "2028-02-01"]
    assert d["roll_to_business_day"] is True and d["anchor_day"] == 1


def test_switching_roll_on_moves_a_weekend_date(staff):
    d = _create(staff, due_date="2027-05-01")
    assert d["due_date"] == "2027-05-01"
    r = staff.patch(f"/api/admin/deadlines/{d['id']}", json={"roll_to_business_day": True}).json()
    assert r["due_date"] == "2027-05-03"


def test_making_an_item_recurring_records_its_day(staff):
    d = _create(staff, due_date="2027-01-31")
    assert d["anchor_day"] is None
    r = staff.patch(f"/api/admin/deadlines/{d['id']}", json={"recurrence": "monthly"}).json()
    assert r["anchor_day"] == 31


def test_recurring_seed_runs_twice_and_kpb_repeats(staff, db):
    from pathlib import Path
    seed = (Path(__file__).resolve().parents[1] / "app/seeds/20261121_recurring_deadlines.sql").read_text()
    db.execute(seed)
    db.execute(seed)  # re-running adds nothing
    items = {i["title"]: i for i in staff.get("/api/admin/deadlines").json()["items"]}
    assert {t: items[t]["due_date"] for t in items} == {
        "KPB sales tax return (quarterly)": "2027-02-01",
        "Business insurance renewal": "2027-08-01",
        "Alaska biennial report (LLC)": "2028-01-02",
        "Copper Valley Subway land lease": "2027-01-01",
        "SAM.gov registration renewal": "2027-08-01",
    }
    lease = items["Copper Valley Subway land lease"]
    assert lease["amount"] == 1200 and lease["lead_business_days"] == 7
    assert lease["action_date"] == "2026-12-21"  # mail by: skips Christmas and New Year's Day
    assert items["KPB sales tax return (quarterly)"]["lead_business_days"] == 0
    kpb = items["KPB sales tax return (quarterly)"]
    nxt = staff.post(f"/api/admin/deadlines/{kpb['id']}/complete").json()["next"]
    assert nxt["due_date"] == "2027-05-03" and nxt["external_ref"] is None
    bien = items["Alaska biennial report (LLC)"]
    assert staff.post(f"/api/admin/deadlines/{bien['id']}/complete").json()["next"]["due_date"] == "2030-01-02"
