-- v3.3 Nayax CCR integration — Supabase schema
-- Companion to payter_schema.sql. Deliberately mirrors it so the dashboard
-- and export coalesce can treat both CCR vendors through one code path.
--
-- Source is NOT the Lynx API (still 403 "Insufficient permissions" since
-- 2026-07-16). Rows come from the Nayax Core portal's Dynamic Transactions
-- Monitor, which is backed by a JSON-RPC facade — see collectors/nayax.py.

-- ── Credentials ───────────────────────────────────────────────────────────────
-- The table already exists from the Lynx probe (id/token/enabled). Extend it
-- rather than replacing it so the Lynx token survives for when/if Nayax ever
-- enables API permissions and we can drop the portal path entirely.
ALTER TABLE nayax_credentials
    ADD COLUMN IF NOT EXISTS username    text,
    ADD COLUMN IF NOT EXISTS password    text,
    -- Base32 seed from the MoMa authenticator enrolment QR (otpauth://totp/DCS).
    -- Nayax's MFA turned out to be standard TOTP, which is the only reason this
    -- collector can run unattended. Password-equivalent: RLS below, never logged.
    ADD COLUMN IF NOT EXISTS totp_secret text,
    -- Oldest transaction to pull on the first run. Portal history reaches back
    -- to roughly 2025-10; earlier ranges return empty rather than erroring.
    -- PROD is set to '2026-01-04 00:00:00-09:00', not this default: Kris chose
    -- to start at January to skip the one Oct-2025 refund while the gross/net
    -- question below is open. Lowering it and re-running --backfill is safe
    -- (everything upserts on nayax_id).
    ADD COLUMN IF NOT EXISTS backfill_floor timestamptz NOT NULL
        DEFAULT '2025-10-01T00:00:00Z';

-- Match the Payter posture: RLS on, no policies, so PostgREST/anon cannot read
-- it. The collector connects via DATABASE_URL as table owner, which RLS does
-- not restrict.
ALTER TABLE nayax_credentials ENABLE ROW LEVEL SECURITY;

-- ── Transactions ──────────────────────────────────────────────────────────────
-- One row per Nayax card transaction. `raw` keeps the full facade row so new
-- fields can be mined later without re-scraping. Session matching happens in
-- nayax_session_matches, never here.
CREATE TABLE IF NOT EXISTS nayax_transactions (
    nayax_id            bigint PRIMARY KEY,       -- facade transaction_id
    device_number       text NOT NULL,            -- ex_device_number → chargers.nayax_serial
    machine_name        text,                     -- "Glennallen Subway", "Delta Charger - 1"
    site_id             integer,

    -- TIMEZONE: the portal reports machineAuTime/machineSeTime in ALASKA LOCAL
    -- time (account TZ is "(GMT-09:00) Alaska", DST automatic → UTC-8 summer,
    -- UTC-9 winter). updated_dt is the same instant already in UTC. The
    -- collector converts the local fields through ZoneInfo("America/Anchorage");
    -- a fixed offset silently breaks at each DST changeover. All three columns
    -- here are true UTC timestamptz.
    txn_timestamp       timestamptz,              -- machineAuTime — card presented
    settle_timestamp    timestamptz,              -- machineSeTime — final capture
    updated_dt          timestamptz NOT NULL,     -- native UTC; incremental cursor

    -- MONEY: the facade reports DOLLARS (seValue 10.13). Payter reports minor
    -- units. We normalise to CENTS on the way in so both vendors' columns mean
    -- the same thing and the revenue coalesce needs no per-vendor branch.
    authorized_amount   integer,                  -- auValue — always the $35 pre-auth
    committed_amount    integer,                  -- seValue — actual session revenue
    currency            text,

    entry_mode          text,                     -- card_type_desc, e.g. "Contactless Reader"
    card_brand          text,                     -- credit_card_type: CREDIT / DEBIT
    masked_pan          text,                     -- card_string "3786 xxxx xxxx 2002"
    payment_service     text,                     -- "Credit Card(CLS)"

    -- 12 = Settled (the APPROVED+COMMITTED analogue). 22 Cancelled By Consumer,
    -- 23 Cancelled by machine, 250 Declined during Authorization. Non-settled
    -- rows carry committed_amount 0 but authorized_amount 3500, so anything
    -- reading revenue MUST filter tran_status_id = 12 — the same trap that bit
    -- the Payter integration with its $35 pre-auth on CANCELED rows.
    --
    -- NULLABLE, deliberately: refund rows carry no status at all. Found
    -- 2025-10-21 during the first backfill (a -$33.27 MOTO refund on
    -- Glennallen). Was NOT NULL and rejected the whole batch.
    tran_status_id      integer,
    -- 0 = sale, 1 = refund. Refunds arrive with negative auValue/seValue,
    -- entry_mode "MOTO for Refund" and payment_service "Credit Card(MOTO)".
    -- See the revenue note at the foot of this file.
    transaction_type_id integer,
    duration_seconds    integer,                  -- time_taken

    raw                 jsonb NOT NULL,
    collected_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_nayax_txn_device_time
    ON nayax_transactions (device_number, txn_timestamp);
CREATE INDEX IF NOT EXISTS idx_nayax_txn_updated
    ON nayax_transactions (updated_dt);
CREATE INDEX IF NOT EXISTS idx_nayax_txn_settled
    ON nayax_transactions (tran_status_id) WHERE tran_status_id = 12;

ALTER TABLE nayax_transactions ENABLE ROW LEVEL SECURITY;

-- ── Tap → session matches ─────────────────────────────────────────────────────
-- Same shape and same rules as payter_session_matches: one transaction claims
-- at most one session and vice versa; unmatched taps stay visible in
-- nayax_transactions for the monthly audit.
CREATE TABLE IF NOT EXISTS nayax_session_matches (
    nayax_id       bigint PRIMARY KEY REFERENCES nayax_transactions(nayax_id) ON DELETE CASCADE,
    station_id     text NOT NULL,             -- chargers.external_id
    connector_id   integer,
    transaction_id text NOT NULL,             -- meter_values_parsed.transaction_id
    session_start  timestamptz NOT NULL,
    gap_seconds    numeric NOT NULL,          -- session_start - card tap
    confidence     text NOT NULL DEFAULT 'high',  -- 'high' | 'ambiguous'
    matched_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (station_id, connector_id, transaction_id)
);

-- ── Refunds and net revenue ───────────────────────────────────────────────────
-- OPEN QUESTION, deliberately left for a decision rather than silently chosen.
--
-- `tran_status_id = 12` selects settled sales and EXCLUDES refunds, because
-- refunds carry a NULL status. So the obvious revenue query reports GROSS, not
-- net — a refunded session still counts its original charge in full.
--
-- Whether that is right depends on what the dashboard figure is meant to mean.
-- Gross matches "what the chargers earned"; net matches "what hit the bank".
-- The Payter side has no equivalent because that API models refunds
-- differently, so choosing net here would make the two vendors inconsistent
-- unless Payter is revisited too.
--
-- To count net, union in the refunds (negative amounts already, so a plain
-- SUM does the right thing):
--     WHERE tran_status_id = 12 OR transaction_type_id = 1
--
-- ── Charger mapping ───────────────────────────────────────────────────────────
-- chargers.nayax_serial was populated 2026-07-16 for the Lynx attempt. The
-- portal's ex_device_number returns exactly those values — but the two DELTA
-- serials were entered SWAPPED, and nothing could detect that until something
-- correlated them against session times. Corrected 2026-08-04:
--   4434332025143598 → Charger - Left  (as_xTUHfTKoOvKSfYZhhdlhT)
--   4434332025143149 → Charger - Right (as_oXoa7HXphUu5riXsSW253)
--   4434331924181150 → Glennallen      (as_LYHe6mZTRKiFfziSNJFvJ)
--
-- Evidence: under the old mapping both Deltas matched 0/24 while Glennallen
-- matched 103/104. Searching every station for each unmatched tap's nearest
-- session put them on the opposite chargers with +11s..+91s gaps — the same
-- tap-then-plug-in signature Payter shows at 3-57s.
--
-- A swapped identifier is invisible until correlated. If other per-charger
-- identifiers were entered in that same sitting, they are worth re-checking
-- the same way rather than assumed good.
