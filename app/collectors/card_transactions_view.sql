-- Cross-vendor card transaction view.
--
-- The dashboard and export currently read payter_transactions /
-- payter_session_matches directly, which is why Nayax chargers still show
-- "Est. Revenue" and no Card Entry chip. Rather than teach every query about a
-- second vendor, both collapse into one view with a common shape. The app then
-- never learns which terminal brand a charger carries — which also means the
-- ABB units going in at Alyeska Tire need no third round of this.
--
-- Only SETTLED/COMMITTED transactions appear here. Both vendors carry a $35
-- pre-auth on cancelled and declined rows, so anything reading revenue from the
-- underlying tables must filter; this view has that filter baked in so callers
-- cannot get it wrong.

-- ── Entry-mode normalisation ──────────────────────────────────────────────────
-- Keyword matching, deliberately, NOT an exact-value lookup. This integration
-- has twice been surprised by values outside the sample (a refund with a null
-- status that aborted the first backfill; zero magstripe transactions in seven
-- months of Nayax data). When Nayax finally emits a swipe it will say something
-- we have never seen — probably "Magnetic Reader". Keywords catch it; an exact
-- map would render a blank chip and nobody would notice for months.
--
-- Vocabularies measured 2026-08-04:
--   Payter ifd      : CONTACTLESS 135 | MAGSTRIPE 3 | CONTACT 2
--   Nayax entry_mode: Contactless Reader 122 | Near field communication 22
--                     | Contact Reader 8 | (magstripe never seen)
--
-- Output labels are Contactless / Contact / Magstripe — deliberately Payter's
-- existing vocabulary (Kris's call, 2026-08-04). Nayax adopts Payter's words
-- rather than the reverse, so no session that already renders in the dashboard
-- changes wording when this view takes over the mapping.
CREATE OR REPLACE FUNCTION normalize_entry_mode(raw text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT CASE
        WHEN raw IS NULL OR btrim(raw) = '' THEN NULL
        -- ORDER IS LOAD-BEARING: "contactless" contains "contact", so the
        -- contactless test must run first or every tap is labelled a chip
        -- insert. Nayax's "Near field communication" is almost certainly mobile
        -- wallet (its BINs never overlap the contactless BINs, while physical
        -- cards do span modes), but it collapses to Contactless anyway — Payter
        -- cannot distinguish wallet from card, and having the two vendors
        -- disagree about identical customer behaviour would read as a bug.
        WHEN lower(raw) LIKE '%contactless%'
          OR lower(raw) LIKE '%near field%'
          OR lower(raw) LIKE '%nfc%'         THEN 'Contactless'
        WHEN lower(raw) LIKE '%magnetic%'
          OR lower(raw) LIKE '%stripe%'
          OR lower(raw) LIKE '%swipe%'       THEN 'Magstripe'
        WHEN lower(raw) LIKE '%contact%'     THEN 'Contact'
        -- Unknown value: show it rather than swallow it. A slightly odd chip
        -- label is a prompt to come look; a blank one is invisible.
        ELSE initcap(raw)
    END
$$;

-- ── The view ──────────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW card_transactions AS

SELECT
    'payter'                              AS vendor,
    p.payter_id::text                     AS vendor_txn_id,
    m.station_id,
    m.connector_id,
    m.transaction_id,
    m.session_start,
    m.confidence                          AS match_confidence,
    p.txn_timestamp                       AS tap_at,
    p.authorized_amount                   AS authorized_cents,
    p.committed_amount                    AS committed_cents,
    p.currency,
    p.ifd                                 AS entry_mode_raw,
    normalize_entry_mode(p.ifd)           AS entry_mode,
    NULLIF(btrim(p.masked_pan), '')       AS masked_pan,
    NULL::text                            AS card_brand   -- Payter does not report CREDIT/DEBIT
FROM payter_transactions p
JOIN payter_session_matches m ON m.payter_id = p.payter_id
WHERE p.disposition = 'APPROVED'
  AND p.state = 'COMMITTED'

UNION ALL

SELECT
    'nayax',
    n.nayax_id::text,
    m.station_id,
    m.connector_id,
    m.transaction_id,
    m.session_start,
    m.confidence,
    n.txn_timestamp,
    n.authorized_amount,
    n.committed_amount,
    n.currency,
    n.entry_mode,
    normalize_entry_mode(n.entry_mode),
    NULLIF(btrim(n.masked_pan), ''),
    NULLIF(btrim(n.card_brand), '')       -- Nayax sometimes sends '' not NULL
FROM nayax_transactions n
JOIN nayax_session_matches m ON m.nayax_id = n.nayax_id
WHERE n.tran_status_id = 12;              -- 12 = Settled

-- Callers join on (station_id, connector_id, transaction_id), which is UNIQUE
-- in both match tables, so this view has at most one row per charging session.
--
-- Revenue for a session:
--     SELECT committed_cents / 100.0 FROM card_transactions
--      WHERE station_id = ? AND transaction_id = ?
--
-- A row existing here is also authoritative proof the session was card-paid —
-- the same rule established for Payter in db0d003, where Alpitronic CCRs emit
-- 20-character idTags that the heuristic mistook for app tokens.
--
-- ⚠ DO NOT USE THIS VIEW FOR A REVENUE TOTAL.
-- The joins are INNER, so a settled transaction that never matched a session is
-- absent — real money, no row. As of 2026-08-04 that is 1 Nayax tap of 152 and
-- 1 Payter tap of ~140, so a total taken from here understates by those. That
-- is correct for anything keyed BY SESSION (the session card, per-session
-- export rows), and wrong for "what did we earn in July".
--
-- For totals, read the base tables and skip the match entirely:
--     SELECT sum(committed_amount)/100.0 FROM nayax_transactions
--      WHERE tran_status_id = 12 AND txn_timestamp >= ? AND txn_timestamp < ?
--   + SELECT sum(committed_amount)/100.0 FROM payter_transactions
--      WHERE disposition='APPROVED' AND state='COMMITTED' AND txn_timestamp ...
