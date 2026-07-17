"""Payter Data API collector — pulls CCR card transactions into payter_transactions.

API manual: https://docs.payter.com/docs/Integration/specifications/mypayter/data-api/overview
(PDF "Payter Data API Manual", June 2024)

Auth flow
---------
1. POST /API/Auth  { username, password, domain }  →  { tokenId, expiresIn, … }
2. Every subsequent call:  header  Authorization: CURO-TOKEN token="<tokenId>"
3. DELETE /API/Auth/{token} when done (best-effort courtesy).

Data endpoint
-------------
POST /API/Data with the incremental-import recipe from the manual:
sort by ingestTime ascending, range-filter from the last ingested time,
term-filter complete=true, page in batches of 1000 until exhausted.

The boundary row is returned again on the next run by design (multiple
transactions can share one ingestTime), so we upsert on payter_id.

Unlike the utility collectors, one Payter login covers every terminal in a
domain, so this module iterates payter_credentials rows directly instead of
subclassing AbstractCollector (which is shaped around utility_accounts).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import asyncpg
import httpx

logger = logging.getLogger("rca.collectors.payter")

BASE_URL = "https://api.mypayter.com/API"
PAGE_SIZE = 1000


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _parse_ts(value) -> datetime | None:
    """Parse Payter ISO-8601 timestamps like '2020-07-30T08:55:49.340Z'."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _doc_to_row(doc: dict, auth_domain: str) -> tuple | None:
    """Flatten one Transaction document into a payter_transactions record."""
    payter_id = doc.get("id")
    ingest_time = _parse_ts(doc.get("ingestTime"))
    if not payter_id or not ingest_time:
        logger.warning("Skipping document without id/ingestTime: %s", doc)
        return None
    return (
        payter_id,
        doc.get("serialNumber"),
        _parse_ts(doc.get("txnTimestamp") or doc.get("@timestamp")),
        _parse_ts(doc.get("authTimestamp")),
        _parse_ts(doc.get("commitTimestamp")),
        _parse_int(doc.get("authorizedAmount")),
        _parse_int(doc.get("committedAmount")),
        doc.get("currency"),
        doc.get("ifd"),
        doc.get("disposition"),
        doc.get("state"),
        doc.get("type"),
        doc.get("extra-TXN-MASKED-PAN"),
        doc.get("siteDomain"),
        doc.get("terminalDomain"),
        bool(doc.get("complete")),
        ingest_time,
        auth_domain,
        json.dumps(doc),
    )


_UPSERT_SQL = """
INSERT INTO payter_transactions
    (payter_id, serial_number, txn_timestamp, auth_timestamp, commit_timestamp,
     authorized_amount, committed_amount, currency, ifd, disposition, state,
     txn_type, masked_pan, site_domain, terminal_domain, complete,
     ingest_time, auth_domain, raw)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
        $11, $12, $13, $14, $15, $16, $17, $18, $19)
ON CONFLICT (payter_id) DO UPDATE SET
    txn_timestamp     = EXCLUDED.txn_timestamp,
    auth_timestamp    = EXCLUDED.auth_timestamp,
    commit_timestamp  = EXCLUDED.commit_timestamp,
    authorized_amount = EXCLUDED.authorized_amount,
    committed_amount  = EXCLUDED.committed_amount,
    currency          = EXCLUDED.currency,
    ifd               = EXCLUDED.ifd,
    disposition       = EXCLUDED.disposition,
    state             = EXCLUDED.state,
    txn_type          = EXCLUDED.txn_type,
    masked_pan        = EXCLUDED.masked_pan,
    site_domain       = EXCLUDED.site_domain,
    terminal_domain   = EXCLUDED.terminal_domain,
    complete          = EXCLUDED.complete,
    ingest_time       = EXCLUDED.ingest_time,
    raw               = EXCLUDED.raw,
    collected_at      = NOW()
"""


# ── API client ────────────────────────────────────────────────────────────────

class PayterClient:
    """Thin async wrapper for the MyPayter Data API (one authenticated session)."""

    def __init__(self, http: httpx.AsyncClient, username: str, password: str, domain: str):
        self.http = http
        self.username = username
        self.password = password
        self.domain = domain
        self.token: str | None = None

    async def login(self) -> None:
        resp = await self.http.post(
            f"{BASE_URL}/Auth",
            json={"username": self.username, "password": self.password, "domain": self.domain},
        )
        resp.raise_for_status()
        self.token = resp.json()["tokenId"]

    async def logout(self) -> None:
        if not self.token:
            return
        try:
            await self.http.delete(f"{BASE_URL}/Auth/{self.token}", headers=self._headers())
        except httpx.HTTPError:
            pass  # courtesy only — token expires server-side anyway

    def _headers(self) -> dict:
        return {"Authorization": f'CURO-TOKEN token="{self.token}"'}

    async def fetch_transactions_page(self, since: datetime | None) -> list[dict]:
        """One page of complete transactions with ingestTime >= since, oldest first."""
        filters: list[dict] = [
            {"type": "term", "field": "complete", "matches": "true"},
        ]
        if since is not None:
            # from/to must be epoch milliseconds (Long). Do NOT send the
            # manual's optional "date" flag — the server 500s/NPEs on it
            # (verified live 2026-07-16).
            filters.append({
                "type": "range",
                "field": "ingestTime",
                "from": int(since.timestamp() * 1000),
            })
        body = {
            "index": "Transactions",
            "query": "",
            "maxResults": PAGE_SIZE,
            "offset": 0,
            "sorts": [{"field": "ingestTime", "asc": True}],
            "filters": filters,
            "aggregations": [],
        }
        resp = await self.http.post(f"{BASE_URL}/Data", json=body, headers=self._headers())
        resp.raise_for_status()
        return resp.json().get("documents", [])


# ── Collection entry point ────────────────────────────────────────────────────

async def collect_payter(pool: asyncpg.Pool) -> int:
    """Run an incremental import for every enabled payter_credentials row.

    First run per domain has no cursor and backfills history from that row's
    backfill_floor. Returns total rows upserted.
    """
    async with pool.acquire() as conn:
        creds = await conn.fetch(
            "SELECT username, password, domain, backfill_floor"
            " FROM payter_credentials WHERE enabled ORDER BY id"
        )

    if not creds:
        logger.info("Payter collect: no enabled rows in payter_credentials — skipping")
        return 0

    total = 0
    async with httpx.AsyncClient(timeout=60) as http:
        for cred in creds:
            try:
                total += await _collect_domain(pool, http, dict(cred))
            except Exception:
                logger.error(
                    "Payter collect failed for domain %s", cred["domain"], exc_info=True
                )

    try:
        await match_sessions(pool)
    except Exception:
        logger.error("Payter session matching failed", exc_info=True)

    return total


async def _collect_domain(pool: asyncpg.Pool, http: httpx.AsyncClient, cred: dict) -> int:
    domain = cred["domain"]

    async with pool.acquire() as conn:
        cursor: datetime | None = await conn.fetchval(
            "SELECT MAX(ingest_time) FROM payter_transactions WHERE auth_domain = $1",
            domain,
        )
    if cursor is None:
        # First run for this domain: don't crawl the terminal's whole life,
        # start at the per-credential floor (payter_credentials.backfill_floor).
        cursor = cred.get("backfill_floor")

    client = PayterClient(http, cred["username"], cred["password"], domain)
    await client.login()
    upserted = 0
    try:
        while True:
            docs = await client.fetch_transactions_page(cursor)
            rows = [r for d in docs if (r := _doc_to_row(d, domain)) is not None]
            if not rows and len(docs) >= PAGE_SIZE:
                logger.error("Payter %s: full page with no parsable rows — aborting", domain)
                break
            if rows:
                async with pool.acquire() as conn:
                    await conn.executemany(_UPSERT_SQL, rows)
                upserted += len(rows)
                new_cursor = max(r[16] for r in rows)  # ingest_time position
                if cursor is not None and new_cursor <= cursor:
                    # Cursor stopped advancing — everything on this ingestTime
                    # already stored. Bail rather than loop forever.
                    break
                cursor = new_cursor
            if len(docs) < PAGE_SIZE:
                break
    finally:
        await client.logout()

    logger.info("Payter %s: upserted %d transactions (cursor now %s)", domain, upserted, cursor)
    return upserted


# ── Tap → session matching ────────────────────────────────────────────────────
#
# A committed Payter transaction has no OCPP session reference; the bridge is
# which charger (serialNumber → chargers.payter_serial → chargers.external_id
# = station_id) plus time. Calibration against live data (2026-07-16): every
# committed tap preceded its session's StartTransaction by 3-57 s, so a
# forward window of 5 minutes is generous. The window start excludes sessions
# already running before the tap (their first meter value predates it).

_MATCH_WINDOW_MIN = 5

_CANDIDATES_SQL = f"""
SELECT x.connector_id, x.transaction_id::text AS transaction_id, x.session_start
FROM (
    SELECT mv.connector_id, mv.transaction_id, MIN(mv.ts) AS session_start
    FROM meter_values_parsed mv
    WHERE mv.station_id = $1
      AND mv.transaction_id IS NOT NULL
      -- Look back 1 h so a session that STARTED before the tap keeps its
      -- pre-tap meter values in the aggregate and is excluded below.
      AND mv.ts BETWEEN $2::timestamptz - INTERVAL '1 hour'
                    AND $2::timestamptz + INTERVAL '{_MATCH_WINDOW_MIN} minutes'
    GROUP BY mv.connector_id, mv.transaction_id
) x
WHERE x.session_start BETWEEN $2::timestamptz
                          AND $2::timestamptz + INTERVAL '{_MATCH_WINDOW_MIN} minutes'
ORDER BY x.session_start ASC
"""


async def match_sessions(pool: asyncpg.Pool) -> int:
    """Link committed Payter transactions to charging sessions.

    Processes every APPROVED+COMMITTED transaction not yet in
    payter_session_matches (new taps each run, plus retries for taps whose
    session data hadn't landed yet). Returns the number of new matches.
    """
    async with pool.acquire() as conn:
        pending = await conn.fetch(
            """
            SELECT p.payter_id, p.txn_timestamp, c.external_id AS station_id
            FROM payter_transactions p
            JOIN chargers c ON c.payter_serial = p.serial_number
            WHERE p.disposition = 'APPROVED'
              AND p.state = 'COMMITTED'
              AND NOT EXISTS (SELECT 1 FROM payter_session_matches m
                              WHERE m.payter_id = p.payter_id)
            ORDER BY p.txn_timestamp
            """
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
                    "Payter match ambiguous for %s: %d sessions started on %s "
                    "within %d min of tap — picked earliest (tx %s)",
                    txn["payter_id"], len(candidates), txn["station_id"],
                    _MATCH_WINDOW_MIN, best["transaction_id"],
                )
            gap = (best["session_start"] - txn["txn_timestamp"]).total_seconds()
            # ON CONFLICT DO NOTHING covers both constraints: a re-run racing
            # itself (payter_id PK) and two taps claiming one session (the
            # session UNIQUE — first tap wins, second stays unmatched).
            result = await conn.execute(
                """
                INSERT INTO payter_session_matches
                    (payter_id, station_id, connector_id, transaction_id,
                     session_start, gap_seconds, confidence)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT DO NOTHING
                """,
                txn["payter_id"], txn["station_id"], best["connector_id"],
                best["transaction_id"], best["session_start"], gap, confidence,
            )
            if result.endswith("1"):
                matched += 1

    logger.info(
        "Payter matcher: %d new matches, %d still unmatched",
        matched, len(pending) - matched,
    )
    return matched
