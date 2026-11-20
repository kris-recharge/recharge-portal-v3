-- Admin Deadlines: ReCharge's recurring obligations (LynkWell invoices, State
-- filings, KPB sales tax, …) with a countdown board in Admin and, in Phase 2,
-- a dedicated alert channel.
--
-- Deliberately NOT part of main.py's _MIGRATION_SQL: that runs on every API
-- start, and the local .env points at production. Apply this file by hand
-- once it has been reviewed.
--
-- Access model (same as every other portal table): FastAPI connects as
-- `postgres`, which owns these tables and bypasses RLS, and enforces the
-- @rechargealaska.net staff check itself (app/routers/deadlines.py). The
-- browser only uses Supabase for OTP login and never queries tables. So RLS is
-- ON with NO policies, and anon/authenticated get no grants. The Data API
-- returns nothing for these tables to anyone, AEA's login included.
-- 20261010_revoke_anon_authenticated_defaults.sql already strips default
-- grants; the REVOKEs below make that explicit for these two tables.
--
-- No functions or triggers: updated_at is set by the API. Nothing is added to
-- the advisor's mutable-search-path or SECURITY DEFINER findings.

CREATE TABLE IF NOT EXISTS public.admin_deadlines (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    series_id         uuid NOT NULL DEFAULT gen_random_uuid(),  -- groups instances of one recurring obligation
    title             text NOT NULL CHECK (length(btrim(title)) > 0),
    category          text NOT NULL CHECK (category IN
                        ('payable','state_filing','tax','insurance','permit','grant','maintenance','other')),
    vendor            text,
    amount            numeric(12,2) CHECK (amount IS NULL OR amount >= 0),
    due_date          date NOT NULL,              -- a calendar date in America/Anchorage
    notes             text,
    link_url          text CHECK (link_url IS NULL OR link_url ~* '^https?://'),  -- payment portal / filing page
    document_url      text CHECK (document_url IS NULL OR document_url ~* '^https?://'),  -- invoice file, e.g. a OneDrive share link
    -- Business days the payment needs in transit. A Wells Fargo Bill Pay check
    -- is ~7, so it must be mailed 7 business days before the due date; the
    -- board and the alerts count down to that mail-by date, not the due date.
    lead_business_days int NOT NULL DEFAULT 0 CHECK (lead_business_days BETWEEN 0 AND 30),
    external_ref      text,                       -- invoice number, filing ID; dedupe key for email ingest
    status            text NOT NULL DEFAULT 'open' CHECK (status IN ('open','scheduled','done','cancelled')),
    scheduled_on      date,
    completed_at      timestamptz,
    snoozed_until     date,                       -- suppresses alerts only; never hides from the board
    recurrence        text NOT NULL DEFAULT 'none' CHECK (recurrence IN
                        ('none','monthly','quarterly','yearly','biennial')),
    alert_offsets     int[] NOT NULL DEFAULT '{14,7,3,1,0}',
    source            text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','email_ingest','recurrence')),
    source_message_id text,                       -- email Message-ID for ingested items
    created_by        text,                       -- staff email that created it
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT admin_deadlines_vendor_ref_key UNIQUE (vendor, external_ref),
    CONSTRAINT admin_deadlines_scheduled_check CHECK (status <> 'scheduled' OR scheduled_on IS NOT NULL)
);

-- Belt and braces for recurrence: one live instance per series per due date,
-- so completing the same item twice can never spawn two next instances.
CREATE UNIQUE INDEX IF NOT EXISTS admin_deadlines_series_due_key
    ON public.admin_deadlines (series_id, due_date)
    WHERE status <> 'cancelled';

CREATE INDEX IF NOT EXISTS admin_deadlines_open_due_idx
    ON public.admin_deadlines (due_date)
    WHERE status IN ('open', 'scheduled');

CREATE TABLE IF NOT EXISTS public.admin_deadline_alerts (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    deadline_id uuid NOT NULL REFERENCES public.admin_deadlines(id) ON DELETE CASCADE,
    alert_key   text NOT NULL,     -- 'T-7', 'T-0', 'OVERDUE-2026-12-15', 'NEW', 'CONFIRM'
    channel     text NOT NULL,
    sent_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT admin_deadline_alerts_once_key UNIQUE (deadline_id, alert_key, channel)
);

ALTER TABLE public.admin_deadlines       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.admin_deadline_alerts ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON public.admin_deadlines       FROM PUBLIC, anon, authenticated;
REVOKE ALL ON public.admin_deadline_alerts FROM PUBLIC, anon, authenticated;
