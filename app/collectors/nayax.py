"""Nayax CCR collector — pulls card transactions into nayax_transactions.

Source is the Nayax Core portal, not the Lynx API. The Lynx token authenticates
but every endpoint returns 403 "Insufficient permissions" (open with Nayax
support since 2026-07-16), so we read the same data out of the portal instead.

Why this isn't a fragile scrape
-------------------------------
Nayax Core is an ASP.NET shell over a single JSON-RPC endpoint. Every screen in
the product is a POST to `facade.aspx` with a `model` and an `action`; the
Dynamic Transactions Monitor grid is just one of them. So we never parse HTML
for data — we call the same RPC the page calls and get structured JSON back:

    POST /core/public/facade.aspx
         ?responseType=json
         &model=operations/dynamicTransactionsMonitorMega
         &action=DynamicTransactionsMonitor.Dynamic_Transactions_Monitor_Mega
         &<filters>
    Header: X-Nayax-Validation-Token: <csrf>

Two-phase design
----------------
Playwright is used ONLY to log in — it handles the CAPTCHA and the TOTP second
factor, then hands off a cookie jar. Everything after that is plain httpx: fetch
the report page once to scrape the CSRF token, then POST the RPC. That keeps the
15-minute poll as cheap as the Payter collector and means a browser launches
only when the session actually dies, which matters on a 1-core VPS.

MFA
---
Nayax's MoMa authenticator turned out to be standard TOTP (the enrolment QR is
a plain `otpauth://totp/DCS?secret=…`), so `pyotp` generates the six-digit code
and the login is genuinely unattended. Push approval is the portal's default
factor; we deliberately use the manual-code fallback field instead, because push
requires a human tapping a phone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
import httpx

logger = logging.getLogger("rca.collectors.nayax")

BASE = "https://my.nayax.com/core"
LOGIN_URL = f"{BASE}/LoginPage.aspx"
MODEL = "operations/dynamicTransactionsMonitorMega"
REPORT_URL = f"{BASE}/public/f?model={MODEL}"
FACADE_URL = f"{BASE}/public/facade.aspx"
REPORT_ACTION = "DynamicTransactionsMonitor.Dynamic_Transactions_Monitor_Mega"

# The portal reports transaction times in the account's local zone
# ("(GMT-09:00) Alaska", DST automatic). A fixed offset would break twice a
# year, so always convert through the zone. `updated_dt` is already UTC.
AK_TZ = ZoneInfo("America/Anchorage")

# 12 = Settled. Everything else (22 Cancelled By Consumer, 23 Cancelled by
# machine, 250 Declined during Authorization) reports committed 0 against the
# same $35 pre-auth, so revenue queries must filter on this. Same trap as the
# Payter CANCELED rows.
STATUS_SETTLED = 12

# How far back each incremental poll re-reads. The report filters on transaction
# date at day granularity, so there's no finer cursor to use; re-reading a week
# is ~20 rows and upserts idempotently. Cheap insurance against a late
# settlement landing after we'd moved past its day.
INCREMENTAL_LOOKBACK_DAYS = 7

# Backfill walks in chunks rather than one giant range — a single request is
# capped by num_of_rows and there's no pagination on this action.
BACKFILL_CHUNK_DAYS = 90
MAX_ROWS = 20000

STATE_PATH = Path(os.environ.get("NAYAX_STATE_PATH", "/data/nayax_state.json"))
TWOCAPTCHA_KEY = os.environ.get("TWOCAPTCHA_API_KEY", "").strip()


# ── Date/number helpers ───────────────────────────────────────────────────────

def _portal_date(value: datetime) -> str:
    """Format for start_date/end_date.

    TRAP: the portal parses these as D/M/YYYY, not US M/D/YYYY. Sending
    '8/4/2026' meaning 4 August silently returns everything up to 8 April
    instead — with rs:SUCCESS and no error, just a short result set.
    """
    return f"{value.day}/{value.month}/{value.year}"


def _parse_local(value) -> datetime | None:
    """Parse an Alaska-local portal timestamp into UTC."""
    if not value:
        return None
    try:
        naive = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return naive.replace(tzinfo=AK_TZ).astimezone(timezone.utc)


def _parse_utc(value) -> datetime | None:
    """Parse `updated_dt`, which the portal already reports in UTC."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _to_cents(value) -> int | None:
    """Dollars → cents. Payter stores minor units; we normalise to match."""
    if value is None:
        return None
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── CAPTCHA ───────────────────────────────────────────────────────────────────
#
# The login page carries two independent challenges and which one fires depends
# on the risk score of the connecting IP — a datacenter address like the VPS's
# scores worse than a residential one, so expect these to trigger more often in
# production than in local testing.
#
#   1. Google reCAPTCHA (loaded render=explicit)
#   2. Nayax's own base64 image CAPTCHA (captchaComponent.js, "Enter Security
#      Code", with an audio fallback)
#
# 2Captcha solves both; they just need different submission methods.

async def _solve_2captcha(http: httpx.AsyncClient, payload: dict) -> str | None:
    """Submit a job to 2Captcha and poll for the answer."""
    if not TWOCAPTCHA_KEY:
        logger.error("CAPTCHA challenge present but TWOCAPTCHA_API_KEY is unset")
        return None

    submit = await http.post(
        "https://2captcha.com/in.php",
        data={"key": TWOCAPTCHA_KEY, "json": 1, **payload},
    )
    data = submit.json()
    if data.get("status") != 1:
        logger.error("2captcha rejected submission: %s", data)
        return None
    job_id = data["request"]

    # Image CAPTCHAs come back in ~10 s, reCAPTCHA in 15-60 s.
    for _ in range(30):
        await asyncio.sleep(5)
        poll = await http.get(
            "https://2captcha.com/res.php",
            params={"key": TWOCAPTCHA_KEY, "action": "get", "id": job_id, "json": 1},
        )
        result = poll.json()
        if result.get("status") == 1:
            return result["request"]
        if result.get("request") != "CAPCHA_NOT_READY":
            logger.error("2captcha error: %s", result)
            return None

    logger.error("2captcha timed out after 150 s")
    return None


async def _handle_captchas(page, http: httpx.AsyncClient) -> None:
    """Solve whichever CAPTCHA is actually being demanded, if any.

    Presence in the DOM is NOT a challenge. The login page pre-renders the
    image-CAPTCHA component hidden and always carries a reCAPTCHA sitekey,
    whether or not either is enforced — verified 2026-08-04, where a login
    succeeded with both "present" and neither solved. So both checks below
    test visibility, not existence; otherwise every login logs a false alarm
    and a genuine challenge has no distinguishable signal.
    """
    # Nayax's own image CAPTCHA — read the base64 straight out of the <img>,
    # but only when the box is actually laid out on screen.
    image_b64 = await page.evaluate(
        """() => {
            const box = document.querySelector('.captcha-box');
            if (!box || !box.offsetParent) return null;
            const img = box.querySelector('img');
            if (!img || !img.src.startsWith('data:image')) return null;
            return img.src.split(',')[1];
        }"""
    )
    if image_b64:
        logger.warning("Nayax image CAPTCHA is being demanded — solving via 2captcha")
        answer = await _solve_2captcha(http, {"method": "base64", "body": image_b64})
        if answer:
            await page.fill('.captcha-box input[type="text"]', answer)
            await page.click('.captcha-box input[type="button"]')
            await page.wait_for_timeout(2000)

    # Google reCAPTCHA. Only the bframe (the "pick all the crosswalks" grid)
    # means we're actually being challenged; the anchor iframe is always there.
    sitekey = await page.evaluate(
        """() => {
            const bframe = document.querySelector(
                'iframe[src*="recaptcha/api2/bframe"]');
            if (!bframe) return null;
            const rect = bframe.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) return null;
            const el = document.querySelector('[data-sitekey]');
            if (el) return el.getAttribute('data-sitekey');
            const anchor = document.querySelector(
                'iframe[src*="recaptcha/api2/anchor"]');
            return anchor ? new URL(anchor.src).searchParams.get('k') : null;
        }"""
    )
    if sitekey:
        logger.warning("reCAPTCHA challenge is being demanded — solving via 2captcha")
        token = await _solve_2captcha(http, {
            "method": "userrecaptcha",
            "googlekey": sitekey,
            "pageurl": LOGIN_URL,
        })
        if token:
            await page.evaluate(
                """(t) => {
                    const el = document.getElementById('g-recaptcha-response');
                    if (el) { el.style.display = 'block'; el.value = t; }
                    if (window.___grecaptcha_cfg) {
                        try { window.onCaptchaSuccess && window.onCaptchaSuccess(t); }
                        catch (e) {}
                    }
                }""",
                token,
            )


# ── Login ─────────────────────────────────────────────────────────────────────

async def _login(cred: dict, headless: bool = True) -> list[dict]:
    """Drive a full interactive login and return the resulting cookies.

    Called only when there's no usable saved session. Everything after this is
    plain HTTP against the cookies this returns.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:  # pragma: no cover - deployment guard
        raise RuntimeError(
            "playwright is required for Nayax login. This collector runs in the "
            "rca_nayax_v3 container, not the API image."
        )

    import pyotp

    totp = pyotp.TOTP(cred["totp_secret"])

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            # Both flags are for the container, harmless locally.
            # --no-sandbox: the Playwright image runs as root, and Chromium's
            #   setuid sandbox refuses to start as root.
            # --disable-dev-shm-usage: Docker gives /dev/shm only 64 MB by
            #   default, which Chromium exhausts and then crashes mid-page.
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1600, "height": 1000},
        )
        page = await context.new_page()

        async with httpx.AsyncClient(timeout=180) as http:
            await page.goto(LOGIN_URL, wait_until="networkidle", timeout=60_000)

            await page.fill("#txtUser", cred["username"])
            await page.fill("#txtPassword", cred["password"])
            await _handle_captchas(page, http)
            await page.click("#signin")

            # Second factor. The portal defaults to a push notification and
            # polls for approval; we ignore that and use the manual-code field,
            # which is the only branch a machine can satisfy on its own.
            try:
                await page.wait_for_selector(
                    "#second_factor_option_totp_input", state="visible", timeout=45_000
                )
            except Exception:
                await browser.close()
                raise RuntimeError(
                    "TOTP field never appeared — login likely failed before MFA "
                    "(bad password, or an unsolved CAPTCHA)."
                )

            # Don't submit a code that's about to roll over; a rejected code
            # can cost us the whole attempt.
            if totp.interval - (int(datetime.now().timestamp()) % totp.interval) < 5:
                await asyncio.sleep(6)

            await _handle_captchas(page, http)
            await page.fill("#second_factor_option_totp_input", totp.now())
            await page.click("#signin")

            # "Trust this device" cuts how often MFA is demanded. Harmless if
            # the prompt doesn't appear.
            try:
                await page.click("#trustDeviceYes", timeout=15_000)
            except Exception:
                logger.debug("No trust-device prompt")

            try:
                await page.wait_for_url(re.compile(r"/core/public/"), timeout=45_000)
            except Exception:
                await browser.close()
                raise RuntimeError("Login did not reach an authenticated page")

            cookies = await context.cookies()
            await browser.close()

    logger.info("Nayax login succeeded (%d cookies)", len(cookies))
    return cookies


def _load_state() -> list[dict] | None:
    if not STATE_PATH.exists():
        return None
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read %s — will re-login", STATE_PATH)
        return None


def _save_state(cookies: list[dict]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(cookies))
        STATE_PATH.chmod(0o600)
    except OSError:
        logger.warning("Could not persist session to %s", STATE_PATH, exc_info=True)


# ── Facade client ─────────────────────────────────────────────────────────────

class NayaxClient:
    """Authenticated JSON-RPC client for the Core facade."""

    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self.token: str | None = None

    async def refresh_token(self) -> bool:
        """Scrape the CSRF token from the report page.

        Doubles as the session liveness check: an expired session serves the
        login page, which has no `var token = ...`.
        """
        resp = await self.http.get(REPORT_URL)
        if resp.status_code != 200:
            return False
        match = re.search(r"var\s+token\s*=\s*'([^']+)'", resp.text)
        if not match:
            return False
        self.token = match.group(1)
        return True

    async def fetch_range(self, start: datetime, end: datetime) -> list[dict]:
        """One report query over an inclusive local date range."""
        params = {
            "responseType": "json",
            "model": MODEL,
            "action": REPORT_ACTION,
            "num_of_rows": MAX_ROWS,
            "show_unclosed": 1,
            "show_sales": 1,
            "with_cash": 0,
            "with_cashless_external": 0,
            "is_Legend": 1,
            "rs:ClearSession": "true",
            "time_period": 56,  # 56 = Date Range; enables start_date/end_date
            "start_date": _portal_date(start),
            "end_date": _portal_date(end),
            "search_transaction_id": "",
            "search_machine_name": "",
            "user_preset_id": 3,
        }
        resp = await self.http.post(
            FACADE_URL,
            params=params,
            headers={
                "X-Nayax-Validation-Token": self.token or "",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("rs") not in (None, "SUCCESS"):
            logger.error("Facade returned rs=%s for %s..%s",
                         body.get("rs"), params["start_date"], params["end_date"])
            return []
        rows = body.get("data") or []
        if len(rows) >= MAX_ROWS:
            logger.error(
                "Range %s..%s hit the %d-row cap — results are truncated, "
                "narrow BACKFILL_CHUNK_DAYS",
                params["start_date"], params["end_date"], MAX_ROWS,
            )
        return rows


# ── Row mapping ───────────────────────────────────────────────────────────────

def _row_to_record(row: dict) -> tuple | None:
    nayax_id = _to_int(row.get("transaction_id"))
    device = row.get("ex_device_number")
    updated = _parse_utc(row.get("updated_dt"))
    if nayax_id is None or not device or updated is None:
        logger.warning("Skipping row without id/device/updated_dt: %s",
                       {k: row.get(k) for k in ("transaction_id", "ex_device_number")})
        return None
    return (
        nayax_id,
        str(device),
        row.get("machine_name"),
        _to_int(row.get("site_id")),
        _parse_local(row.get("machineAuTime")),
        _parse_local(row.get("machineSeTime")),
        updated,
        _to_cents(row.get("auValue")),
        _to_cents(row.get("seValue")),
        row.get("currency"),
        row.get("card_type_desc"),
        row.get("credit_card_type"),
        row.get("card_string"),
        row.get("payment_service"),
        _to_int(row.get("tran_status_id")),
        _to_int(row.get("transaction_type_id")),
        _to_int(row.get("time_taken")),
        json.dumps(row, default=str),
    )


_UPSERT_SQL = """
INSERT INTO nayax_transactions
    (nayax_id, device_number, machine_name, site_id, txn_timestamp,
     settle_timestamp, updated_dt, authorized_amount, committed_amount,
     currency, entry_mode, card_brand, masked_pan, payment_service,
     tran_status_id, transaction_type_id, duration_seconds, raw)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
        $11, $12, $13, $14, $15, $16, $17, $18)
ON CONFLICT (nayax_id) DO UPDATE SET
    txn_timestamp       = EXCLUDED.txn_timestamp,
    settle_timestamp    = EXCLUDED.settle_timestamp,
    updated_dt          = EXCLUDED.updated_dt,
    authorized_amount   = EXCLUDED.authorized_amount,
    committed_amount    = EXCLUDED.committed_amount,
    entry_mode          = EXCLUDED.entry_mode,
    card_brand          = EXCLUDED.card_brand,
    masked_pan          = EXCLUDED.masked_pan,
    payment_service     = EXCLUDED.payment_service,
    tran_status_id      = EXCLUDED.tran_status_id,
    transaction_type_id = EXCLUDED.transaction_type_id,
    duration_seconds    = EXCLUDED.duration_seconds,
    raw                 = EXCLUDED.raw,
    collected_at        = NOW()
"""


# ── Collection entry point ────────────────────────────────────────────────────

async def collect_nayax(pool: asyncpg.Pool, *, backfill: bool = False) -> int:
    """Pull Nayax transactions for every enabled credential row.

    Incremental by default (a rolling week). `backfill=True` walks from the
    credential's backfill_floor to now in chunks. Returns rows upserted.
    """
    async with pool.acquire() as conn:
        creds = await conn.fetch(
            "SELECT id, username, password, totp_secret, backfill_floor"
            " FROM nayax_credentials"
            " WHERE enabled AND username IS NOT NULL AND totp_secret IS NOT NULL"
            " ORDER BY id"
        )

    if not creds:
        logger.info("Nayax collect: no usable rows in nayax_credentials — skipping")
        return 0

    total = 0
    for cred in creds:
        try:
            total += await _collect_one(pool, dict(cred), backfill=backfill)
        except Exception:
            logger.error("Nayax collect failed for credential id=%s",
                         cred["id"], exc_info=True)

    try:
        await match_sessions(pool)
    except Exception:
        logger.error("Nayax session matching failed", exc_info=True)

    return total


async def _collect_one(pool: asyncpg.Pool, cred: dict, *, backfill: bool) -> int:
    now_local = datetime.now(AK_TZ)

    if backfill:
        start = cred["backfill_floor"].astimezone(AK_TZ)
    else:
        start = now_local - timedelta(days=INCREMENTAL_LOOKBACK_DAYS)

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as http:
        client = NayaxClient(http)

        # Try the saved session first; only pay for a browser if it's dead.
        cookies = _load_state()
        if cookies:
            for cookie in cookies:
                http.cookies.set(cookie["name"], cookie["value"],
                                 domain=cookie.get("domain", "my.nayax.com"))

        if not await client.refresh_token():
            logger.info("No usable Nayax session — logging in")
            http.cookies.clear()
            cookies = await _login(cred)
            _save_state(cookies)
            for cookie in cookies:
                http.cookies.set(cookie["name"], cookie["value"],
                                 domain=cookie.get("domain", "my.nayax.com"))
            if not await client.refresh_token():
                raise RuntimeError("Login succeeded but the report page has no token")

        upserted = 0
        chunk_start = start
        while chunk_start <= now_local:
            chunk_end = min(chunk_start + timedelta(days=BACKFILL_CHUNK_DAYS), now_local)
            rows = await client.fetch_range(chunk_start, chunk_end)
            records = [r for row in rows if (r := _row_to_record(row)) is not None]
            if records:
                async with pool.acquire() as conn:
                    await conn.executemany(_UPSERT_SQL, records)
                upserted += len(records)
            logger.info("Nayax %s..%s: %d rows, %d stored",
                        _portal_date(chunk_start), _portal_date(chunk_end),
                        len(rows), len(records))
            chunk_start = chunk_end + timedelta(days=1)

    logger.info("Nayax credential id=%s: upserted %d transactions",
                cred["id"], upserted)
    return upserted


# ── Tap → session matching ────────────────────────────────────────────────────
#
# Identical approach to the Payter matcher: a settled card transaction carries
# no OCPP reference, so the bridge is charger identity (device_number →
# chargers.nayax_serial → chargers.external_id) plus time. Unlike the Payter
# serials, the Nayax device numbers need no normalisation — the portal's
# ex_device_number matches chargers.nayax_serial exactly.

_MATCH_WINDOW_MIN = 5

_CANDIDATES_SQL = f"""
SELECT x.connector_id, x.transaction_id::text AS transaction_id, x.session_start
FROM (
    SELECT mv.connector_id, mv.transaction_id, MIN(mv.ts) AS session_start
    FROM meter_values_parsed mv
    WHERE mv.station_id = $1
      AND mv.transaction_id IS NOT NULL
      -- Look back an hour so a session already running before the tap keeps
      -- its earlier meter values in the aggregate and is excluded below.
      AND mv.ts BETWEEN $2::timestamptz - INTERVAL '1 hour'
                    AND $2::timestamptz + INTERVAL '{_MATCH_WINDOW_MIN} minutes'
    GROUP BY mv.connector_id, mv.transaction_id
) x
WHERE x.session_start BETWEEN $2::timestamptz
                          AND $2::timestamptz + INTERVAL '{_MATCH_WINDOW_MIN} minutes'
ORDER BY x.session_start ASC
"""


async def match_sessions(pool: asyncpg.Pool) -> int:
    """Link settled Nayax transactions to charging sessions."""
    async with pool.acquire() as conn:
        pending = await conn.fetch(
            """
            SELECT n.nayax_id, n.txn_timestamp, c.external_id AS station_id
            FROM nayax_transactions n
            JOIN chargers c ON c.nayax_serial = n.device_number
            WHERE n.tran_status_id = $1
              AND n.txn_timestamp IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM nayax_session_matches m
                              WHERE m.nayax_id = n.nayax_id)
            ORDER BY n.txn_timestamp
            """,
            STATUS_SETTLED,
        )
        if not pending:
            return 0

        matched = 0
        for txn in pending:
            candidates = await conn.fetch(
                _CANDIDATES_SQL, txn["station_id"], txn["txn_timestamp"]
            )
            if not candidates:
                continue  # session data may not have landed yet — retried next run
            best = candidates[0]
            confidence = "high" if len(candidates) == 1 else "ambiguous"
            if confidence == "ambiguous":
                logger.warning(
                    "Nayax match ambiguous for %s: %d sessions started on %s "
                    "within %d min of tap — picked earliest (tx %s)",
                    txn["nayax_id"], len(candidates), txn["station_id"],
                    _MATCH_WINDOW_MIN, best["transaction_id"],
                )
            gap = (best["session_start"] - txn["txn_timestamp"]).total_seconds()
            # DO NOTHING covers both constraints: a re-run racing itself
            # (nayax_id PK) and two taps claiming one session (the session
            # UNIQUE — first tap wins, second stays unmatched for the audit).
            result = await conn.execute(
                """
                INSERT INTO nayax_session_matches
                    (nayax_id, station_id, connector_id, transaction_id,
                     session_start, gap_seconds, confidence)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT DO NOTHING
                """,
                txn["nayax_id"], txn["station_id"], best["connector_id"],
                best["transaction_id"], best["session_start"], gap, confidence,
            )
            if result.endswith("1"):
                matched += 1

    logger.info("Nayax matcher: %d new matches, %d still unmatched",
                matched, len(pending) - matched)
    return matched
