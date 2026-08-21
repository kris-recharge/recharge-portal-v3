-- v3.3 Payter CCR integration — Supabase schema
-- Applied 2026-07-16. Kept here as the record of what exists in prod.

-- One row per MyPayter login. Multiple rows supported in case the parent
-- domain can't see both subdomains and we need one login per subdomain.
CREATE TABLE IF NOT EXISTS payter_credentials (
    id         serial PRIMARY KEY,
    username   text NOT NULL,
    password   text NOT NULL,
    domain     text NOT NULL,
    enabled    boolean NOT NULL DEFAULT true,
    -- Oldest transaction to backfill on the first run for this domain.
    backfill_floor timestamptz NOT NULL DEFAULT '2024-01-01T00:00:00Z',
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (username, domain)
);
-- 2026-07-16: RLS enabled on payter_credentials and payter_transactions by
-- Kris (Supabase dashboard). No policies → PostgREST/anon access denied.
-- The collector connects via DATABASE_URL as the table owner, which RLS
-- does not restrict, so collection is unaffected.

-- Immutable log of Payter card transactions, one row per terminal transaction.
-- `raw` holds the full Data API document so new fields can be extracted later
-- without re-fetching. Session matching happens at query time, never here.
CREATE TABLE IF NOT EXISTS payter_transactions (
    payter_id            text PRIMARY KEY,          -- "SERIAL:txnId", stable across re-ingest
    serial_number        text NOT NULL,             -- CCR device S/N
    txn_timestamp        timestamptz,               -- card presented (just before session start)
    auth_timestamp       timestamptz,               -- pre-auth approved
    commit_timestamp     timestamptz,               -- final capture (after session end)
    authorized_amount    integer,                   -- pre-auth hold, minor units (cents)
    committed_amount     integer,                   -- final session cost, minor units (cents)
    currency             text,
    ifd                  text,                      -- entry mode: CONTACTLESS / contact / magstripe
    disposition          text,                      -- APPROVED / DECLINED / ...
    state                text,                      -- COMMITTED / ...
    txn_type             text,                      -- ONLINE / OFFLINE
    masked_pan           text,
    site_domain          text,
    terminal_domain      text,
    complete             boolean,
    ingest_time          timestamptz NOT NULL,      -- Payter-side ingest clock; sync cursor
    auth_domain          text NOT NULL,             -- which credential row fetched it (cursor scope)
    raw                  jsonb NOT NULL,
    collected_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payter_txn_serial_time
    ON payter_transactions (serial_number, txn_timestamp);
CREATE INDEX IF NOT EXISTS idx_payter_txn_ingest
    ON payter_transactions (auth_domain, ingest_time);

-- Materialized tap→session matches, populated by the matcher after each
-- collect (see payter.py match_sessions). One Payter transaction claims at
-- most one session and vice versa; unmatched taps stay visible in
-- payter_transactions for audit. Calibration 2026-07-16: all 45 committed
-- taps matched a StartTransaction within 3-57 s, so the 5-minute window is
-- generous.
CREATE TABLE IF NOT EXISTS payter_session_matches (
    payter_id      text PRIMARY KEY REFERENCES payter_transactions(payter_id) ON DELETE CASCADE,
    station_id     text NOT NULL,             -- chargers.external_id (as_…)
    connector_id   integer,
    transaction_id text NOT NULL,             -- meter_values_parsed.transaction_id
    session_start  timestamptz NOT NULL,      -- first meter value of the matched session
    gap_seconds    numeric NOT NULL,          -- session_start - card tap
    confidence     text NOT NULL DEFAULT 'high',  -- 'high' | 'ambiguous' (2+ candidates in window)
    matched_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (station_id, connector_id, transaction_id)
);

-- CCR terminal → charger mapping (applied 2026-07-16, values from Kris).
-- Join chain to session data: payter_transactions.serial_number
--   → chargers.payter_serial → chargers.external_id (= station_id in
--   meter_values_parsed / session CTEs).
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS payter_serial text UNIQUE;
-- P6820211200171    → veefil-62100164  (ARG - Left,  Tritium RTM75)
-- P6820232100602    → veefil-602200077 (ARG - Right / "Charger - B", Tritium RTM75)
-- POL20245100755-50 → 104007972        (CL-A, Alpitronic HYC400)
-- POL20243401437-72 → 104007978        (CL-B, Alpitronic HYC400)
-- POL20245100346-16 → 104007977        (CL-C, Alpitronic HYC400)
-- POL20245000996-82 → 104007979        (CL-D, Alpitronic HYC400)
--
-- ⚠ When a CCR is physically replaced, this column MUST be updated or the
-- matcher silently stops matching that charger — no revenue, no Card Entry
-- chip, no CC classification, and no error anywhere. ARG - Right's terminal was
-- swapped twice (P6820221300628 → P6820210400097 on 2026-08-10 → P6820232100602
-- on 2026-08-20, chargers updated each time). The 2026-08-20 swap was noticed
-- only because Kris spotted missing cost/auth data in the dashboard, so
-- match_sessions now logs a warning for any committed tap whose serial resolves
-- to no charger. Retiring the old serial is safe *only because* payter_session_matches
-- materialises station_id at match time, so already-matched history is frozen
-- and survives the change. Before swapping a serial, confirm no APPROVED+
-- COMMITTED tap on the OLD serial is still unmatched — those can never match
-- once the join stops resolving. (ARG - Right had 0 pending at swap time.)
