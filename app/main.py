"""ReCharge Alaska Portal v3 — FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .alerts import start_alert_thread
from .collectors.scheduler import start_collector_scheduler, stop_collector_scheduler
from .config import ALLOWED_ORIGINS
from .db import close_pool, create_pool
from .routers import (
    admin, alerts_config, alerts_push, alerts_sse, analytics,
    connectivity, export, sessions, status,
)
from .routers import utility
from .routers import maintenance
from .routers import me
from .routers import connector_count

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("rca.main")


_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS alert_subscriptions (
    user_id     UUID        NOT NULL,
    alert_type  TEXT        NOT NULL,
    enabled     BOOLEAN     NOT NULL DEFAULT false,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, alert_type)
);

-- ── Alert delivery channels (v3.4) ───────────────────────────────────────────
-- `enabled` stays the master switch: it decides whether the user is subscribed
-- at all, and drives BOTH the Alert History tab and the in-app banner. The two
-- columns below pick which channels a subscribed alert is actually delivered
-- through. email_enabled defaults true so the migration is a no-op for existing
-- subscribers (they keep getting the emails they get today); push_enabled
-- defaults false so nobody is opted into push without asking for it.
ALTER TABLE alert_subscriptions
    ADD COLUMN IF NOT EXISTS email_enabled BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE alert_subscriptions
    ADD COLUMN IF NOT EXISTS push_enabled  BOOLEAN NOT NULL DEFAULT false;

-- ── Web Push device registrations (v3.4) ─────────────────────────────────────
-- One row per browser/device that has granted notification permission. The
-- endpoint is the push service URL issued by Apple/Google and is unique per
-- device+origin, so it doubles as the natural key: re-subscribing the same
-- device updates the existing row instead of accumulating duplicates.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID        NOT NULL,
    endpoint     TEXT        NOT NULL UNIQUE,
    p256dh       TEXT        NOT NULL,
    auth         TEXT        NOT NULL,
    user_agent   TEXT        NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error   TEXT
);
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user
    ON push_subscriptions (user_id);

-- device_label is supplied by the browser, not parsed from the user agent.
-- iPadOS 13+ requests desktop sites by default and sends a macOS user agent
-- ("Macintosh; Intel Mac OS X 10_15_7"), so an iPad is indistinguishable from a
-- Mac by UA alone -- it showed up in the device list as "Mac". The client can
-- tell the difference (a Macintosh reporting touch points is an iPad) and sends
-- the answer along.
ALTER TABLE push_subscriptions
    ADD COLUMN IF NOT EXISTS device_label TEXT NOT NULL DEFAULT '';

-- ── In-app banner scope (v3.4) ───────────────────────────────────────────────
-- The SSE banner used to fan out every alert to every connected client with no
-- per-user filtering, so a tenant scoped to one charger could see toasts for
-- other tenants' sites. EVSE scope is now enforced server-side and is NOT
-- optional.
--
-- This flag controls only the *alert type* filter for the in-app views (banner
-- toasts + Alert History). It DEFAULTS TRUE: while you are logged in you see
-- everything happening on your own chargers — vendor fault codes, offline,
-- suspicious VID, PM — not only the types you subscribed to. Subscriptions
-- decide what gets pushed or emailed to you when you are NOT looking; they are
-- not meant to hide events from the dashboard itself. Set false to narrow the
-- in-app views to your subscribed types only.
ALTER TABLE portal_users
    ADD COLUMN IF NOT EXISTS banner_all_alert_types BOOLEAN NOT NULL DEFAULT true;
-- Correct the default even if an earlier build already added the column as
-- DEFAULT false (rows created under that default keep their stored value).
ALTER TABLE portal_users
    ALTER COLUMN banner_all_alert_types SET DEFAULT true;

CREATE TABLE IF NOT EXISTS fired_alerts (
    id          UUID        NOT NULL DEFAULT gen_random_uuid(),
    fired_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    alert_type  TEXT        NOT NULL,
    asset_id    TEXT        NOT NULL,
    evse_name   TEXT        NOT NULL,
    message     TEXT        NOT NULL,
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_fired_alerts_fired_at ON fired_alerts (fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_fired_alerts_asset_id  ON fired_alerts (asset_id);

-- ── Portal user capabilities ─────────────────────────────────────────────────
-- can_submit_pm: grants a non-admin user the right to log PMs / repairs on the
-- units in their allowed_evse_ids. Defaults false so it must be granted
-- explicitly from the Admin center.
ALTER TABLE portal_users ADD COLUMN IF NOT EXISTS can_submit_pm BOOLEAN NOT NULL DEFAULT false;

-- ── Utility data collection tables ───────────────────────────────────────────

CREATE TABLE IF NOT EXISTS utility_accounts (
    id                      SERIAL      PRIMARY KEY,
    utility                 TEXT        NOT NULL,
    account_number          TEXT        NOT NULL,
    display_name            TEXT        NOT NULL DEFAULT '',
    service_location_number TEXT,
    customer_number         TEXT,
    system_of_record        TEXT        NOT NULL DEFAULT 'UTILITY',
    meter_group_id          TEXT,
    enabled                 BOOLEAN     NOT NULL DEFAULT TRUE,
    last_collected          TIMESTAMPTZ,
    last_error              TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (utility, account_number)
);

CREATE TABLE IF NOT EXISTS utility_credentials (
    id         SERIAL      PRIMARY KEY,
    utility    TEXT        NOT NULL UNIQUE,
    username   TEXT        NOT NULL,
    password   TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS utility_usage (
    id              BIGSERIAL   PRIMARY KEY,
    utility         TEXT        NOT NULL,
    account_number  TEXT        NOT NULL,
    meter_id        TEXT,
    interval_start  TIMESTAMPTZ NOT NULL,
    interval_end    TIMESTAMPTZ NOT NULL,
    kwh             NUMERIC(10, 4),
    is_estimated    BOOLEAN     NOT NULL DEFAULT FALSE,
    granularity_min INTEGER     NOT NULL,
    collected_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (utility, account_number, interval_start)
);

CREATE INDEX IF NOT EXISTS idx_utility_usage_lookup
    ON utility_usage (utility, account_number, interval_start DESC);

-- ── RLS: lock down credentials table ─────────────────────────────────────────
-- Enable RLS — service role bypasses automatically; anon/authenticated are denied.
ALTER TABLE utility_credentials ENABLE ROW LEVEL SECURITY;

-- ═══════════════════════════════════════════════════════════════════════════════
-- v3.1  Maintenance Tracker
-- All tables are idempotent (CREATE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS).
-- ═══════════════════════════════════════════════════════════════════════════════

-- ── Extend chargers table ─────────────────────────────────────────────────────
-- Allow site_id to be NULL for fleet units not yet assigned to a site
ALTER TABLE chargers ALTER COLUMN site_id DROP NOT NULL;
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS serial_number              TEXT;
ALTER TABLE maintenance_records ADD COLUMN IF NOT EXISTS onsite_hours    NUMERIC(6,2);
ALTER TABLE maintenance_records ADD COLUMN IF NOT EXISTS mobilized_hours NUMERIC(6,2);
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS unit_type_id               UUID;
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS warranty_start             DATE;
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS warranty_end               DATE;
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS warranty_notes             TEXT;
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS owner_name                 TEXT;
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS maintenance_responsibility  TEXT NOT NULL DEFAULT 'RCA';
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS network_platform           TEXT;
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS network_platform_notes     TEXT;
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS port_count                 INTEGER;
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS commission_date            DATE;
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS status                     TEXT NOT NULL DEFAULT 'active';
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS retired_at                 TIMESTAMPTZ;
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS retired_reason             TEXT;
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS retired_by                 UUID;
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS parts_on_order             BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- ── unit_types ────────────────────────────────────────────────────────────────
-- mirror_type_id: if no pm_templates exist for this unit type, use that type's
-- templates instead (e.g. ABB Terra53 mirrors Autel).
CREATE TABLE IF NOT EXISTS unit_types (
    id                        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    type_name                 TEXT        NOT NULL UNIQUE,
    manufacturer              TEXT        NOT NULL,
    mirror_type_id            UUID,
    default_pm_template_id    UUID,
    interval_quarterly_months INTEGER     DEFAULT 3,
    interval_semiannual_months INTEGER    DEFAULT 6,
    interval_annual_months    INTEGER     NOT NULL DEFAULT 12,
    hyperdoc_required         BOOLEAN     NOT NULL DEFAULT false,
    notes                     TEXT,
    is_active                 BOOLEAN     NOT NULL DEFAULT true,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── unit_location_history ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS unit_location_history (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    charger_id  UUID        NOT NULL REFERENCES chargers(id),
    site_id     UUID        NOT NULL REFERENCES sites(id),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    assigned_by UUID,
    notes       TEXT
);

-- ── pm_templates ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pm_templates (
    id               UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    template_name    TEXT    NOT NULL,
    unit_type_id     UUID    NOT NULL REFERENCES unit_types(id),
    pm_interval      TEXT    NOT NULL CHECK (pm_interval IN ('quarterly','semi_annual','annual')),
    source_document  TEXT,
    template_version TEXT    NOT NULL DEFAULT 'v1.0',
    is_active        BOOLEAN NOT NULL DEFAULT true,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── pm_template_tasks ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pm_template_tasks (
    id                UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id       UUID    NOT NULL REFERENCES pm_templates(id),
    task_code         TEXT    NOT NULL,
    task_order        INTEGER NOT NULL,
    task_category     TEXT    NOT NULL,
    task_name         TEXT    NOT NULL,
    task_description  TEXT    NOT NULL,
    input_type        TEXT    NOT NULL CHECK (input_type IN
                        ('pass_fail','completed','measured_value','text','pass_fail_action')),
    unit_of_measure   TEXT,
    is_required       BOOLEAN NOT NULL DEFAULT true,
    is_conditional    BOOLEAN NOT NULL DEFAULT false,
    conditional_label TEXT,
    critical_fail     BOOLEAN NOT NULL DEFAULT false,
    fail_guidance     TEXT
);

-- ── maintenance_records ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS maintenance_records (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    charger_id            UUID        NOT NULL REFERENCES chargers(id),
    site_id               UUID        REFERENCES sites(id),
    record_timestamp      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    record_type           TEXT        NOT NULL CHECK (record_type IN
                            ('pm_quarterly','pm_semi_annual','pm_annual','pm_general',
                             'repair','warranty','inspection','other')),
    pm_template_id        UUID        REFERENCES pm_templates(id),
    pm_template_version   TEXT,
    overall_result        TEXT        CHECK (overall_result IN ('pass','conditional','fail')),
    firmware_version      TEXT,
    technician_name       TEXT        NOT NULL,
    technician_user_id    UUID,
    work_description      TEXT,
    additional_work_needed BOOLEAN    NOT NULL DEFAULT false,
    planned_future_work   TEXT,
    hyperdoc_required     BOOLEAN     NOT NULL DEFAULT false,
    hyperdoc_submitted    BOOLEAN     NOT NULL DEFAULT false,
    hyperdoc_submitted_at DATE,
    created_by            UUID,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_maintenance_records_charger
    ON maintenance_records (charger_id, record_timestamp DESC);

-- ── pm_task_results ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pm_task_results (
    id                    UUID  PRIMARY KEY DEFAULT gen_random_uuid(),
    record_id             UUID  NOT NULL REFERENCES maintenance_records(id) ON DELETE CASCADE,
    task_id               UUID  NOT NULL REFERENCES pm_template_tasks(id),
    result_pass_fail      TEXT  CHECK (result_pass_fail IN ('pass','fail','na')),
    result_completed      BOOLEAN,
    result_measured_value TEXT,
    result_text           TEXT,
    task_notes            TEXT
);
CREATE INDEX IF NOT EXISTS idx_pm_task_results_record
    ON pm_task_results (record_id);

-- ── maintenance_parts_replaced ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS maintenance_parts_replaced (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    record_id   UUID NOT NULL REFERENCES maintenance_records(id) ON DELETE CASCADE,
    part_name   TEXT NOT NULL,
    part_number TEXT,
    action_taken TEXT NOT NULL CHECK (action_taken IN ('replaced','repaired','cleaned','adjusted')),
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS idx_parts_replaced_record
    ON maintenance_parts_replaced (record_id);

-- ── maintenance_photos (Phase 5 — NTH) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS maintenance_photos (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    record_id       UUID        NOT NULL REFERENCES maintenance_records(id) ON DELETE CASCADE,
    storage_path    TEXT        NOT NULL,
    file_name       TEXT        NOT NULL,
    file_size_bytes INTEGER,
    mime_type       TEXT,
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    uploaded_by     UUID
);

-- ── RLS: lock down all application tables ────────────────────────────────────
-- Service role (used by this app via DATABASE_URL and service role key) bypasses
-- RLS automatically. Anon/authenticated roles have no direct table access —
-- all data access goes through the FastAPI backend only.
ALTER TABLE alert_subscriptions       ENABLE ROW LEVEL SECURITY;
ALTER TABLE push_subscriptions        ENABLE ROW LEVEL SECURITY;
ALTER TABLE fired_alerts              ENABLE ROW LEVEL SECURITY;
ALTER TABLE utility_accounts          ENABLE ROW LEVEL SECURITY;
ALTER TABLE utility_usage             ENABLE ROW LEVEL SECURITY;
ALTER TABLE unit_types                ENABLE ROW LEVEL SECURITY;
ALTER TABLE unit_location_history     ENABLE ROW LEVEL SECURITY;
ALTER TABLE pm_templates              ENABLE ROW LEVEL SECURITY;
ALTER TABLE pm_template_tasks         ENABLE ROW LEVEL SECURITY;
ALTER TABLE maintenance_records       ENABLE ROW LEVEL SECURITY;
ALTER TABLE pm_task_results           ENABLE ROW LEVEL SECURITY;
ALTER TABLE maintenance_parts_replaced ENABLE ROW LEVEL SECURITY;
ALTER TABLE maintenance_photos        ENABLE ROW LEVEL SECURITY;

-- ── Timestamp immutability triggers ──────────────────────────────────────────
CREATE OR REPLACE FUNCTION trg_fn_immutable_record_timestamp()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.record_timestamp = OLD.record_timestamp;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION trg_fn_immutable_location_history()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'unit_location_history rows are immutable — insert a new row instead';
END;
$$;

DROP TRIGGER IF EXISTS trg_maintenance_records_ts ON maintenance_records;
CREATE TRIGGER trg_maintenance_records_ts
    BEFORE UPDATE ON maintenance_records
    FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_record_timestamp();

DROP TRIGGER IF EXISTS trg_location_history_immutable ON unit_location_history;
CREATE TRIGGER trg_location_history_immutable
    BEFORE UPDATE ON unit_location_history
    FOR EACH ROW EXECUTE FUNCTION trg_fn_immutable_location_history();

-- ═══════════════════════════════════════════════════════════════════════════════
-- Seed: unit_types  (fixed UUIDs for cross-reference stability)
-- ═══════════════════════════════════════════════════════════════════════════════
INSERT INTO unit_types
    (id, type_name, manufacturer, mirror_type_id,
     interval_quarterly_months, interval_semiannual_months, interval_annual_months,
     hyperdoc_required, notes)
VALUES
  ('aaaaaaaa-aaaa-aaaa-aaaa-000000000001',
   'Autel DC Fast', 'Autel', NULL, 3, 6, 12, false, NULL),

  ('aaaaaaaa-aaaa-aaaa-aaaa-000000000002',
   'Tritium MSC', 'Tritium',
   'aaaaaaaa-aaaa-aaaa-aaaa-000000000001',
   3, 6, 12, false,
   'No OEM PM schedule — mirrors Autel checklist. Out of warranty.'),

  ('aaaaaaaa-aaaa-aaaa-aaaa-000000000003',
   'ABB Terra53', 'ABB',
   'aaaaaaaa-aaaa-aaaa-aaaa-000000000001',
   3, 6, 12, false,
   'Third-party owned. Out of warranty.'),

  ('aaaaaaaa-aaaa-aaaa-aaaa-000000000004',
   'ABB Terra184', 'ABB',
   'aaaaaaaa-aaaa-aaaa-aaaa-000000000001',
   3, 6, 12, false,
   'Third-party owned. Non-LynkWell platform.'),

  ('aaaaaaaa-aaaa-aaaa-aaaa-000000000005',
   'Alpitronics HYC_400UL', 'Alpitronic', NULL,
   NULL, NULL, 12, true,
   'Annual only. Hyperdoc required. Operator credentials required.')
-- v3.2: key on the fixed UUID (id), not type_name, so DB-side renames
-- (e.g. Tritium MSC → Tritium RTM) don't cause a PK collision on boot.
ON CONFLICT (id) DO NOTHING;

-- ═══════════════════════════════════════════════════════════════════════════════
-- Seed: pm_templates  (Autel Q / SA / Annual + Alpitronics Annual)
-- ABB Terra53 uses Autel templates via mirror_type_id on unit_types.
-- ═══════════════════════════════════════════════════════════════════════════════
INSERT INTO pm_templates
    (id, template_name, unit_type_id, pm_interval, source_document, template_version)
VALUES
  ('bbbbbbbb-bbbb-bbbb-bbbb-000000000001',
   'Autel DC Fast Quarterly v1.0',
   'aaaaaaaa-aaaa-aaaa-aaaa-000000000001',
   'quarterly', 'Autel MaxiCharger DC Fast Part 4 — Maintenance', 'v1.0'),

  ('bbbbbbbb-bbbb-bbbb-bbbb-000000000002',
   'Autel DC Fast Semi-Annual v1.0',
   'aaaaaaaa-aaaa-aaaa-aaaa-000000000001',
   'semi_annual', 'Autel MaxiCharger DC Fast Part 4 — Maintenance', 'v1.0'),

  ('bbbbbbbb-bbbb-bbbb-bbbb-000000000003',
   'Autel DC Fast Annual v1.0',
   'aaaaaaaa-aaaa-aaaa-aaaa-000000000001',
   'annual', 'Autel MaxiCharger DC Fast Part 4 — Maintenance', 'v1.0'),

  ('bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'Alpitronics HYC_400UL Annual v1.0',
   'aaaaaaaa-aaaa-aaaa-aaaa-000000000005',
   'annual', 'Alpitronics HYC_400UL Installation and Maintenance Manual v1-5 Section 10 Table 25', 'v1.0')
ON CONFLICT (id) DO NOTHING;

-- Set default_pm_template_id to the annual template for each type that has one
UPDATE unit_types SET default_pm_template_id = 'bbbbbbbb-bbbb-bbbb-bbbb-000000000003'
WHERE id = 'aaaaaaaa-aaaa-aaaa-aaaa-000000000001'
  AND default_pm_template_id IS NULL;
UPDATE unit_types SET default_pm_template_id = 'bbbbbbbb-bbbb-bbbb-bbbb-000000000004'
WHERE id = 'aaaaaaaa-aaaa-aaaa-aaaa-000000000005'
  AND default_pm_template_id IS NULL;

-- ═══════════════════════════════════════════════════════════════════════════════
-- Seed: pm_template_tasks
-- Quarterly template: Q-01, Q-02
-- Semi-Annual template: Q-01, Q-02, S-01
-- Annual template: Q-01, Q-02, S-01, A-01, A-02
-- Alpitronics Annual: AP-01–09, AE-01–05, AF-01–05
-- ═══════════════════════════════════════════════════════════════════════════════

-- ── Autel Quarterly (template bbbbbbbb-...0001) ───────────────────────────────
INSERT INTO pm_template_tasks
    (id, template_id, task_code, task_order, task_category,
     task_name, task_description, input_type, is_required, is_conditional,
     critical_fail, fail_guidance)
VALUES
  ('cccccccc-0001-0000-0000-000000000001',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000001',
   'Q-01', 1, 'Physical Inspection',
   'Connector — all ports',
   'Check for cracks or ruptures on the connector(s). For dual-port units inspect each port independently.',
   'pass_fail', true, false, false,
   'Flag for follow-up — note severity and which port(s) affected'),

  ('cccccccc-0001-0000-0000-000000000002',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000001',
   'Q-02', 2, 'Physical Inspection',
   'Input / Charge Cable — all cables',
   'Check for cracks or ruptures on the cable(s). Inspect full cable length and strain relief.',
   'pass_fail', true, false, false,
   'Flag for follow-up — note severity and which cable(s) affected')
ON CONFLICT (id) DO NOTHING;

-- ── Autel Semi-Annual (template bbbbbbbb-...0002) ────────────────────────────
INSERT INTO pm_template_tasks
    (id, template_id, task_code, task_order, task_category,
     task_name, task_description, input_type, is_required, is_conditional,
     critical_fail, fail_guidance)
VALUES
  ('cccccccc-0002-0000-0000-000000000001',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000002',
   'Q-01', 1, 'Physical Inspection',
   'Connector — all ports',
   'Check for cracks or ruptures on the connector(s). For dual-port units inspect each port independently.',
   'pass_fail', true, false, false,
   'Flag for follow-up — note severity and which port(s) affected'),

  ('cccccccc-0002-0000-0000-000000000002',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000002',
   'Q-02', 2, 'Physical Inspection',
   'Input / Charge Cable — all cables',
   'Check for cracks or ruptures on the cable(s). Inspect full cable length and strain relief.',
   'pass_fail', true, false, false,
   'Flag for follow-up — note severity and which cable(s) affected'),

  ('cccccccc-0002-0000-0000-000000000003',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000002',
   'S-01', 3, 'Physical Inspection',
   'Cabinet exterior',
   'Clean and inspect cabinet for physical damage. Check air filter condition as part of cabinet review.',
   'pass_fail_action', true, false, false,
   'Flag for follow-up — note damage location or filter condition')
ON CONFLICT (id) DO NOTHING;

-- ── Autel Annual (template bbbbbbbb-...0003) ─────────────────────────────────
INSERT INTO pm_template_tasks
    (id, template_id, task_code, task_order, task_category,
     task_name, task_description, input_type, is_required, is_conditional,
     critical_fail, fail_guidance)
VALUES
  ('cccccccc-0003-0000-0000-000000000001',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000003',
   'Q-01', 1, 'Physical Inspection',
   'Connector — all ports',
   'Check for cracks or ruptures on the connector(s). For dual-port units inspect each port independently.',
   'pass_fail', true, false, false,
   'Flag for follow-up — note severity and which port(s) affected'),

  ('cccccccc-0003-0000-0000-000000000002',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000003',
   'Q-02', 2, 'Physical Inspection',
   'Input / Charge Cable — all cables',
   'Check for cracks or ruptures on the cable(s). Inspect full cable length and strain relief.',
   'pass_fail', true, false, false,
   'Flag for follow-up — note severity and which cable(s) affected'),

  ('cccccccc-0003-0000-0000-000000000003',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000003',
   'S-01', 3, 'Physical Inspection',
   'Cabinet exterior',
   'Clean and inspect cabinet for physical damage. Check air filter condition as part of cabinet review.',
   'pass_fail_action', true, false, false,
   'Flag for follow-up — note damage location or filter condition'),

  ('cccccccc-0003-0000-0000-000000000004',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000003',
   'A-01', 4, 'Replacement',
   'Inlet Air Filter',
   'Replace the inlet air filter. Record part in Parts Replaced section.',
   'completed', true, false, false, NULL),

  ('cccccccc-0003-0000-0000-000000000005',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000003',
   'A-02', 5, 'Replacement',
   'Outlet Air Filter',
   'Replace the outlet air filter. Record part in Parts Replaced section.',
   'completed', true, false, false, NULL)
ON CONFLICT (id) DO NOTHING;

-- ── Alpitronics Annual (template bbbbbbbb-...0004) ───────────────────────────
INSERT INTO pm_template_tasks
    (id, template_id, task_code, task_order, task_category,
     task_name, task_description, input_type, unit_of_measure,
     is_required, is_conditional, conditional_label, critical_fail, fail_guidance)
VALUES
  ('dddddddd-0004-0000-0000-000000000001',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AP-01', 1, 'Physical Inspection',
   'External visual inspection',
   'Inspect condition of housing and structural stability, NEMA 3R compliance, site accessibility, and CCT unit if installed.',
   'pass_fail', NULL, true, false, NULL, false, NULL),

  ('dddddddd-0004-0000-0000-000000000002',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AP-02', 2, 'Physical Inspection',
   'Charging cables & plugs',
   'Inspect all cable parts: sleeve, plug, mating face, and pins — no damage, sheath intact, no cracks, pins undamaged, cable intact at transfer point. Verify cable glands are tight.',
   'pass_fail', NULL, true, false, NULL, false,
   'Remove unit from service if cable integrity is compromised — do not allow charging until repaired'),

  ('dddddddd-0004-0000-0000-000000000003',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AP-03', 3, 'Physical Inspection',
   'Sealing of input conductors',
   'Verify all conduit and input conductors are properly sealed to prevent dust ingress.',
   'pass_fail', NULL, true, false, NULL, false,
   'Re-seal before returning to service'),

  ('dddddddd-0004-0000-0000-000000000004',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AP-04', 4, 'Physical Inspection',
   'Internal screw connections',
   'Visual random check of internal screws and tightening torques (cabinet open). LOTO REQUIRED — de-energize unit completely before opening cabinet.',
   'pass_fail', NULL, true, false, NULL, false,
   'Re-torque loose connections per OEM spec before re-energizing'),

  ('dddddddd-0004-0000-0000-000000000005',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AP-05', 5, 'Physical Inspection',
   'Cooling unit (oil-cooled cable)',
   'Check filling level, connections, absence of air pockets and creases in cooling circuit.',
   'pass_fail', NULL, true, true, 'N/A — no oil-cooled cable installed', false, NULL),

  ('dddddddd-0004-0000-0000-000000000006',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AP-06', 6, 'Physical Inspection',
   'Interior cleanliness',
   'Check cleanliness inside charger. Clean with dry cloth and vacuum if needed.',
   'pass_fail_action', NULL, true, false, NULL, false, NULL),

  ('dddddddd-0004-0000-0000-000000000007',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AP-07', 7, 'Physical Inspection',
   'Condensation check',
   'Visually inspect for condensation in all viewable areas including SiC Stacks (no disassembly required).',
   'pass_fail', NULL, true, false, NULL, true,
   'HARD STOP — do NOT re-energize. Isolate unit and contact Alpitronic support immediately before any further action.'),

  ('dddddddd-0004-0000-0000-000000000008',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AP-08', 8, 'Physical Inspection',
   'Filter mats',
   'Check integrity and contamination of filter mats. Replace if degraded or heavily contaminated.',
   'pass_fail_action', NULL, true, false, NULL, false,
   'Record replacement in Parts Replaced section'),

  ('dddddddd-0004-0000-0000-000000000009',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AP-09', 9, 'Physical Inspection',
   'Touch protection',
   'Verify all protective covers are correctly attached and secured.',
   'pass_fail', NULL, true, false, NULL, false,
   'Re-attach any missing or improperly secured covers before re-energizing'),

  ('dddddddd-0004-0000-0000-000000000010',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AE-01', 10, 'Electrical Testing',
   'Earthing system',
   'Visual inspection of earthing connections, test earthing resistance, and verify continuity of equipotential bonding.',
   'measured_value', 'Ω', true, false, NULL, false,
   'Resistance must meet local electrical code — consult Alpitronic documentation for acceptance criteria'),

  ('dddddddd-0004-0000-0000-000000000011',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AE-02', 11, 'Electrical Testing',
   'DC outlet insulation resistance',
   'Check insulation resistance of pins on each DC charging outlet using a megohmmeter.',
   'measured_value', 'MΩ', true, false, NULL, false,
   'Low insulation resistance indicates potential insulation breakdown — do not return to service until cause is identified'),

  ('dddddddd-0004-0000-0000-000000000012',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AE-03', 12, 'Electrical Testing',
   'Supply line check',
   'Insulation resistance on input switchgear busbars, confirm protective device, check short-circuit current. Mark N/A if commissioning protocol is on file.',
   'pass_fail', NULL, true, true, 'N/A — commissioning protocol on file', false, NULL),

  ('dddddddd-0004-0000-0000-000000000013',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AE-04', 13, 'Electrical Testing',
   'Overvoltage protection',
   'Check optical defect display of overvoltage protection device for any fault indication.',
   'pass_fail', NULL, true, false, NULL, false,
   'Replace overvoltage protection device if defect display is active'),

  ('dddddddd-0004-0000-0000-000000000014',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AE-05', 14, 'Electrical Testing',
   'Residual current devices',
   'Perform functional test of all circuit breakers with residual current monitoring using RCD tester.',
   'pass_fail', NULL, true, false, NULL, false,
   'Replace or repair any RCD that fails functional test before re-energizing'),

  ('dddddddd-0004-0000-0000-000000000015',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AF-01', 15, 'Functional Testing',
   'RFID reader',
   'Perform functional test of RFID reader with a known valid RFID card.',
   'pass_fail', NULL, true, false, NULL, false,
   'Log fault — unit may still charge via other auth methods but RFID issue must be resolved'),

  ('dddddddd-0004-0000-0000-000000000016',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AF-02', 16, 'Functional Testing',
   'SIM — Alpitronic backend',
   'Verify SIM connection to Alpitronic backend (confirm in Hyperdoc / Alpitronic portal).',
   'pass_fail', NULL, true, false, NULL, false,
   'Contact Alpitronic support — warranty compliance depends on backend connectivity'),

  ('dddddddd-0004-0000-0000-000000000017',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AF-03', 17, 'Functional Testing',
   'SIM — ReCharge Alaska backend',
   'Verify OCPP connection to LynkWell dashboard and confirm charger appears online.',
   'pass_fail', NULL, true, false, NULL, false,
   'Check LynkWell Connectivity tab — contact network support if offline'),

  ('dddddddd-0004-0000-0000-000000000018',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AF-04', 18, 'Functional Testing',
   'Display elements & buttons',
   'Perform functional test of HMI display, physical buttons, and CCT touchscreen if installed.',
   'pass_fail', NULL, true, false, NULL, false, NULL),

  ('dddddddd-0004-0000-0000-000000000019',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000004',
   'AF-05', 19, 'Functional Testing',
   'LED rings on connectors',
   'Perform functional test of LED status rings on all charging connectors.',
   'pass_fail', NULL, true, false, NULL, false, NULL)
ON CONFLICT (id) DO NOTHING;

-- ═══════════════════════════════════════════════════════════════════════════════
-- Populate chargers: serial numbers, warranty, unit_type_id
-- ═══════════════════════════════════════════════════════════════════════════════

-- Glennallen — Autel MaxiCharger
UPDATE chargers SET
    serial_number               = 'DL0120B1GS6V00056D',
    warranty_end                = '2027-09-25',
    maintenance_responsibility  = 'RCA',
    network_platform            = 'LynkWell',
    unit_type_id                = 'aaaaaaaa-aaaa-aaaa-aaaa-000000000001'
WHERE external_id = 'as_LYHe6mZTRKiFfziSNJFvJ'
  AND serial_number IS NULL;

-- Delta - Right — Autel MaxiCharger
UPDATE chargers SET
    serial_number               = 'DL0120B1GR9V000105',
    warranty_end                = '2028-01-30',
    maintenance_responsibility  = 'RCA',
    network_platform            = 'LynkWell',
    unit_type_id                = 'aaaaaaaa-aaaa-aaaa-aaaa-000000000001'
WHERE external_id = 'as_oXoa7HXphUu5riXsSW253'
  AND serial_number IS NULL;

-- Delta - Left — Autel MaxiCharger
UPDATE chargers SET
    serial_number               = 'DL0120B1GR8V000058',
    warranty_end                = '2028-01-30',
    maintenance_responsibility  = 'RCA',
    network_platform            = 'LynkWell',
    unit_type_id                = 'aaaaaaaa-aaaa-aaaa-aaaa-000000000001'
WHERE external_id = 'as_xTUHfTKoOvKSfYZhhdlhT'
  AND serial_number IS NULL;

-- ARG - Left — Tritium RTM
UPDATE chargers SET
    serial_number               = 'veefil-62100164',
    maintenance_responsibility  = 'RCA',
    network_platform            = 'LynkWell',
    unit_type_id                = 'aaaaaaaa-aaaa-aaaa-aaaa-000000000002'
WHERE external_id = 'as_cnIGqQ0DoWdFCo7zSrN01'
  AND serial_number IS NULL;

-- ARG - Right — Tritium RTM
UPDATE chargers SET
    serial_number               = 'veefil-602200077',
    maintenance_responsibility  = 'RCA',
    network_platform            = 'LynkWell',
    unit_type_id                = 'aaaaaaaa-aaaa-aaaa-aaaa-000000000002'
WHERE external_id = 'as_c8rCuPHDd7sV1ynHBVBiq'
  AND serial_number IS NULL;

-- ABB Terra184 — non-LynkWell, third-party owned (INSERT if not already present)
INSERT INTO chargers
    (id, external_id, name, serial_number, site_id, make, connector_types,
     status, owner_name, maintenance_responsibility,
     network_platform, network_platform_notes, unit_type_id)
SELECT
    'eeeeeeee-eeee-eeee-eeee-000000000001'::uuid,
    NULL,
    'Terra184 (TBD)',
    'T184-IT1-3423-020',
    NULL,
    'ABB',
    '[]'::jsonb,
    'active',
    'Third Party',
    'RCA',
    'Other',
    'ABB remote management — no OCPP webhook to LynkWell',
    'aaaaaaaa-aaaa-aaaa-aaaa-000000000004'::uuid
WHERE NOT EXISTS (
    SELECT 1 FROM chargers WHERE id = 'eeeeeeee-eeee-eeee-eeee-000000000001'::uuid
);

-- ═══════════════════════════════════════════════════════════════════════════════
-- v3.2  ABB Terra184 native PM templates
--       Source: ABB Terra x4 User Manual, Sections 4.8.1 (Owner PM) & 7.3 (SE PM)
--       Unit: T184-IT1-3423-020 — CC config (dual CCS, no CHAdeMO)
-- ═══════════════════════════════════════════════════════════════════════════════

INSERT INTO pm_templates
    (id, template_name, unit_type_id, pm_interval, source_document, template_version)
VALUES
  ('bbbbbbbb-bbbb-bbbb-bbbb-000000000005',
   'ABB Terra184 Quarterly v1.0',
   'aaaaaaaa-aaaa-aaaa-aaaa-000000000004',
   'quarterly',
   'ABB Terra x4 User Manual — Sections 4.8.1 (Owner PM) & 7.3 (Service Engineer PM)',
   'v1.0'),

  ('bbbbbbbb-bbbb-bbbb-bbbb-000000000006',
   'ABB Terra184 Semi-Annual v1.0',
   'aaaaaaaa-aaaa-aaaa-aaaa-000000000004',
   'semi_annual',
   'ABB Terra x4 User Manual — Sections 4.8.1 (Owner PM) & 7.3 (Service Engineer PM)',
   'v1.0'),

  ('bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'ABB Terra184 Annual v1.0',
   'aaaaaaaa-aaaa-aaaa-aaaa-000000000004',
   'annual',
   'ABB Terra x4 User Manual — Sections 4.8.1 (Owner PM) & 7.3 (Service Engineer PM)',
   'v1.0')
ON CONFLICT (id) DO NOTHING;

-- Remove Autel mirror from Terra184 — native templates now exist
UPDATE unit_types SET mirror_type_id = NULL
WHERE id   = 'aaaaaaaa-aaaa-aaaa-aaaa-000000000004'
  AND mirror_type_id = 'aaaaaaaa-aaaa-aaaa-aaaa-000000000001';

-- Set default annual template for Terra184 (idempotent)
UPDATE unit_types SET default_pm_template_id = 'bbbbbbbb-bbbb-bbbb-bbbb-000000000007'
WHERE id = 'aaaaaaaa-aaaa-aaaa-aaaa-000000000004'
  AND default_pm_template_id IS NULL;

-- ── ABB Terra184 Quarterly tasks (template ...0005) ───────────────────────────
-- Section 4.8.1: cables/connectors every 3 months; cabinet clean every 4 months
-- Section 7.3:   cables/connectors & gun holders inspect annually → check each visit
INSERT INTO pm_template_tasks
    (id, template_id, task_code, task_order, task_category,
     task_name, task_description, input_type,
     is_required, is_conditional, conditional_label, critical_fail, fail_guidance)
VALUES
  ('eeeeeeee-0005-0000-0000-000000000001',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000005',
   'Q-01', 1, 'Physical Inspection',
   'EV charge cables & connectors — all ports',
   'Inspect all CCS charge cables and connectors (both outlets on CC config) for cuts, '
   'cracks, abrasion, or connector damage. Inspect full cable length and strain relief. '
   '(Section 4.8.1 — every 3 months; Section 7.3 — inspect annually)',
   'pass_fail', true, false, NULL, false,
   'Remove affected outlet from service. Note severity and which port(s) affected.'),

  ('eeeeeeee-0005-0000-0000-000000000002',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000005',
   'Q-02', 2, 'Physical Inspection',
   'Cabinet cleaning',
   'Clean exterior of cabinet. Remove dust, debris, and contaminants from surfaces and ventilation openings. '
   '(Section 4.8.1 — every 4 months; performed at each quarterly visit)',
   'completed', true, false, NULL, false, NULL),

  ('eeeeeeee-0005-0000-0000-000000000003',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000005',
   'Q-03', 3, 'Physical Inspection',
   'Gun holders — all ports',
   'Inspect gun holders on all CCS outlets for physical damage or improper seating. '
   'Verify rain caps are present and undamaged. (Section 7.3 — inspect annually; checked each visit)',
   'pass_fail', true, false, NULL, false,
   'Replace damaged gun holder. Parts: XAB.V2N21.01 (CCS Type 1 UL) / XAB.V2N17.01 (CCS Type 2 CE). '
   'Rain cap: XEE.V2N50.01 (Type 1) / XEE.V2N49.01 (Type 2).')
ON CONFLICT (id) DO NOTHING;

-- ── ABB Terra184 Semi-Annual tasks (template ...0006) ─────────────────────────
-- Cumulative: all quarterly tasks + cabinet damage check + detailed connector inspection
INSERT INTO pm_template_tasks
    (id, template_id, task_code, task_order, task_category,
     task_name, task_description, input_type,
     is_required, is_conditional, conditional_label, critical_fail, fail_guidance)
VALUES
  ('eeeeeeee-0006-0000-0000-000000000001',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000006',
   'Q-01', 1, 'Physical Inspection',
   'EV charge cables & connectors — all ports',
   'Inspect all CCS charge cables and connectors (both outlets on CC config) for cuts, '
   'cracks, abrasion, or connector damage. Inspect full cable length and strain relief. '
   '(Section 4.8.1 — every 3 months)',
   'pass_fail', true, false, NULL, false,
   'Remove affected outlet from service. Note severity and which port(s) affected.'),

  ('eeeeeeee-0006-0000-0000-000000000002',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000006',
   'Q-02', 2, 'Physical Inspection',
   'Cabinet cleaning',
   'Clean exterior of cabinet. Remove dust, debris, and contaminants. (Section 4.8.1 — every 4 months)',
   'completed', true, false, NULL, false, NULL),

  ('eeeeeeee-0006-0000-0000-000000000003',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000006',
   'S-01', 3, 'Physical Inspection',
   'Cabinet exterior — damage inspection',
   'Inspect cabinet for physical damage: dents, corrosion, door seal integrity, latch operation, '
   'ventilation openings unobstructed. (Section 4.8.1 — every 6 months)',
   'pass_fail_action', true, false, NULL, false,
   'Document damage with photos. Schedule repair if structural integrity or weatherproofing is compromised.'),

  ('eeeeeeee-0006-0000-0000-000000000004',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000006',
   'S-02', 4, 'Physical Inspection',
   'CCS connector & cable — detailed inspection',
   'Inspect CCS connector mating face and pins for corrosion, deformation, or contact damage. '
   'Verify cable glands are tight. Confirm rain caps present and undamaged. '
   '(Section 7.3 — inspect annually; Part: ZLH.01085 per outlet; Rain cap: XEE.V2N50.01 / XEE.V2N49.01)',
   'pass_fail', true, false, NULL, false,
   'Replace connector/cable assembly if mating face or pins are damaged. Part ZLH.01085 (qty 1 per outlet).'),

  ('eeeeeeee-0006-0000-0000-000000000005',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000006',
   'S-03', 5, 'Physical Inspection',
   'CHAdeMO connector & cable',
   'Inspect CHAdeMO connector and cable. Mark N/A if unit is CC config (CCS-only). '
   '(Section 7.3 — inspect annually; Part: YVD.V3G25.0 per outlet)',
   'pass_fail', true, true, 'N/A — CC config, no CHAdeMO installed', false,
   'Replace assembly if damaged. Part: YVD.V3G25.0 (qty 1 per outlet).'),

  ('eeeeeeee-0006-0000-0000-000000000006',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000006',
   'S-04', 6, 'Physical Inspection',
   'Gun holders — all ports',
   'Inspect gun holders on all outlets for physical damage or improper seating. '
   '(Section 7.3; Parts: XAB.V2N21.01 CCS Type 1 / XAB.V2N17.01 CCS Type 2 / XAB.V2N18.01 CHAdeMO)',
   'pass_fail', true, false, NULL, false,
   'Replace damaged gun holder using appropriate ABB part for connector type.')
ON CONFLICT (id) DO NOTHING;

-- ── ABB Terra184 Annual tasks (template ...0007) ──────────────────────────────
-- Cumulative: all quarterly + semi-annual tasks + filter replacements + lifecycle items
-- Lifecycle replacement schedule (Section 7.3):
--   Fans, DC fuses, power supplies → Years 5 / 10 / 15
--   Power modules                  → Year 10
--   CPI, contactors, touchscreen   → Year 15
INSERT INTO pm_template_tasks
    (id, template_id, task_code, task_order, task_category,
     task_name, task_description, input_type,
     is_required, is_conditional, conditional_label, critical_fail, fail_guidance)
VALUES
  ('eeeeeeee-0007-0000-0000-000000000001',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'Q-01', 1, 'Physical Inspection',
   'EV charge cables & connectors — all ports',
   'Inspect all CCS charge cables and connectors for cuts, cracks, abrasion, or connector damage. '
   'Inspect full cable length and strain relief. (Section 4.8.1 — every 3 months; Section 7.3 — inspect annually)',
   'pass_fail', true, false, NULL, false,
   'Remove affected outlet from service. Note which port(s) affected.'),

  ('eeeeeeee-0007-0000-0000-000000000002',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'Q-02', 2, 'Physical Inspection',
   'Cabinet cleaning',
   'Clean exterior of cabinet. Remove dust, debris, and contaminants from surfaces and ventilation openings. '
   '(Section 4.8.1 — every 4 months)',
   'completed', true, false, NULL, false, NULL),

  ('eeeeeeee-0007-0000-0000-000000000003',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'S-01', 3, 'Physical Inspection',
   'Cabinet exterior — damage inspection',
   'Inspect cabinet for physical damage: dents, corrosion, door seal integrity, latch operation, '
   'ventilation openings unobstructed. (Section 4.8.1 — every 6 months)',
   'pass_fail_action', true, false, NULL, false,
   'Document with photos. Schedule repair if structural integrity or weatherproofing is compromised.'),

  ('eeeeeeee-0007-0000-0000-000000000004',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'S-02', 4, 'Physical Inspection',
   'CCS connector & cable — detailed inspection',
   'Inspect CCS connector mating face and pins for corrosion, deformation, or contact damage. '
   'Verify cable glands tight. Confirm rain caps present. (Section 7.3 — inspect all years; never replaced per schedule)',
   'pass_fail', true, false, NULL, false,
   'Replace assembly if mating face or pins are damaged. Part: ZLH.01085 (qty 1 per outlet).'),

  ('eeeeeeee-0007-0000-0000-000000000005',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'S-03', 5, 'Physical Inspection',
   'CHAdeMO connector & cable',
   'Inspect CHAdeMO connector and cable. Mark N/A if CC config (CCS-only). (Section 7.3 — inspect all years)',
   'pass_fail', true, true, 'N/A — CC config, no CHAdeMO installed', false,
   'Replace assembly if damaged. Part: YVD.V3G25.0 (qty 1 per outlet).'),

  ('eeeeeeee-0007-0000-0000-000000000006',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'S-04', 6, 'Physical Inspection',
   'Gun holders — all ports',
   'Inspect gun holders for damage or improper seating. (Section 7.3 — inspect all years; never replaced per schedule)',
   'pass_fail', true, false, NULL, false,
   'Replace if damaged. Parts: XAB.V2N21.01 (CCS Type 1) / XAB.V2N17.01 (CCS Type 2) / XAB.V2N18.01 (CHAdeMO).'),

  ('eeeeeeee-0007-0000-0000-000000000007',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'A-01', 7, 'Replacement',
   'Inlet air filter — replace',
   'Replace inlet air filter(s). Record part in Parts Replaced section. '
   'Part: XUF.N0001.0 (qty 1 per unit). (Section 4.8.1 + Section 7.3 — replace every year, years 1–15)',
   'completed', true, false, NULL, false, NULL),

  ('eeeeeeee-0007-0000-0000-000000000008',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'A-02', 8, 'Replacement',
   'Outlet air filter — inspect (odd service years) / replace (year 1 and even service years)',
   'Year 1: replace. Even service years (2, 4, 6 …): replace. Odd service years (3, 5, 7 …): inspect only. '
   'Note current service year and action taken in task notes. '
   'Part: XUF.N0002.0 (qty 1 per unit). (Section 7.3)',
   'pass_fail_action', true, false, NULL, false,
   'Replace if degraded regardless of scheduled year. Record whether inspected or replaced.'),

  ('eeeeeeee-0007-0000-0000-000000000009',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'A-03', 9, 'Lifecycle — Inspect/Replace',
   'Cooling fan',
   'Inspect cooling fan(s) for noise, vibration, or reduced airflow. '
   'Replace at service years 5, 10, and 15; inspect at all other years. '
   'Note service year, condition, and whether replaced. Part: ZFV.V2N00.01 (qty 1). (Section 7.3)',
   'pass_fail_action', true, false, NULL, false,
   'Replace immediately if fan is noisy, vibrating, or underperforming, regardless of scheduled year.'),

  ('eeeeeeee-0007-0000-0000-000000000010',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'A-04', 10, 'Lifecycle — Inspect/Replace',
   'DC fuse 500A',
   'Visually inspect DC fuse(s) for heat damage, discoloration, or housing cracks. '
   'Replace at service years 5, 10, and 15; inspect at all other years. '
   'Note service year and fuse condition. Part: ZEG.00132 (qty 2 for CC config). '
   '(Section 7.3 — listed as "DC fuse 200A"; spare parts Section 7.4 lists 500A ZEG.00132 — verify correct fuse rating on unit)',
   'pass_fail_action', true, false, NULL, false,
   'Replace immediately if fuse shows signs of damage. Use ABB part ZEG.00132.'),

  ('eeeeeeee-0007-0000-0000-000000000011',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'A-05', 11, 'Lifecycle — Inspect/Replace',
   'Power modules (x6 for Terra184)',
   'Inspect all 6 power modules for fault indicator LEDs, unusual heat, or active alarm codes on display. '
   'Replace full set at service year 10; inspect all other years. '
   'Note service year and any fault codes. Part: YPA.00267 (qty 6 for Terra 184). (Section 7.3)',
   'pass_fail_action', true, false, NULL, false,
   'Log any active fault codes. Contact ABB service if module shows fault. Replace full set at year 10.'),

  ('eeeeeeee-0007-0000-0000-000000000012',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'A-06', 12, 'Lifecycle — Inspect/Replace',
   'CPI Combo CCS interface',
   'Inspect CPI Combo CCS interface for physical damage or communication faults. '
   'Replace at service year 15; inspect all other years. '
   'Part: YVD.V39A3.0 (qty 2 for CC config). (Section 7.3)',
   'pass_fail', true, false, NULL, false,
   'Log any faults. Replace at year 15 per lifecycle schedule.'),

  ('eeeeeeee-0007-0000-0000-000000000013',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'A-07', 13, 'Lifecycle — Inspect/Replace',
   'DC outlet contactors',
   'Inspect DC outlet contactor(s) for proper operation and signs of arcing or wear. '
   'Replace at service year 15; inspect all other years. '
   'Part: ZEQQ.00104 (qty 2 for CC config). (Section 7.3)',
   'pass_fail', true, false, NULL, false,
   'Report operational issues to ABB service. Replace at year 15.'),

  ('eeeeeeee-0007-0000-0000-000000000014',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'A-08', 14, 'Lifecycle — Inspect/Replace',
   'Touchscreen / CPU',
   'Inspect touchscreen and CPU for proper display, responsiveness, and error codes. '
   'Replace at service year 15; inspect all other years. '
   'Part: YVD.V2N11.01 (qty 1). (Section 7.3)',
   'pass_fail', true, false, NULL, false,
   'Document any display issues or error codes. Replace at year 15.'),

  ('eeeeeeee-0007-0000-0000-000000000015',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000007',
   'A-09', 15, 'Lifecycle — Inspect/Replace',
   'Auxiliary power supply',
   'Inspect auxiliary power supply for proper operation and output within spec. '
   'Replace at service years 5, 10, and 15; inspect all other years. '
   'Part: YPA.00205 (qty 1). (Section 7.3)',
   'pass_fail_action', true, false, NULL, false,
   'Replace at years 5, 10, 15 per lifecycle schedule. Report voltage faults immediately.')
ON CONFLICT (id) DO NOTHING;

-- ═══════════════════════════════════════════════════════════════════════════════
-- v3.3  Tritium RTM native PM template (ARG Left & ARG Right)
--       Source: Tritium INS.1716 v8 — RTM Preventive Maintenance Schedule
--       Date:   10 March 2026
--       Units:  veefil-62100164 (ARG Left), veefil-602200077 (ARG Right)
-- ═══════════════════════════════════════════════════════════════════════════════

-- Rename unit type and remove Autel mirror (idempotent: WHERE guards re-run)
UPDATE unit_types SET
    type_name                  = 'Tritium RTM',
    mirror_type_id             = NULL,
    interval_quarterly_months  = NULL,
    interval_semiannual_months = NULL,
    notes = 'ARG Left (veefil-62100164) and ARG Right (veefil-602200077). Annual PM from Tritium INS.1716 v8 (10 Mar 2026).'
WHERE id = 'aaaaaaaa-aaaa-aaaa-aaaa-000000000002'
  AND type_name = 'Tritium MSC';

INSERT INTO pm_templates
    (id, template_name, unit_type_id, pm_interval, source_document, template_version)
VALUES
  ('bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'Tritium RTM Annual v1.0',
   'aaaaaaaa-aaaa-aaaa-aaaa-000000000002',
   'annual',
   'Tritium INS.1716 v8 — RTM Preventive Maintenance Schedule (10 March 2026)',
   'v1.0')
ON CONFLICT (id) DO NOTHING;

UPDATE unit_types SET default_pm_template_id = 'bbbbbbbb-bbbb-bbbb-bbbb-000000000008'
WHERE id = 'aaaaaaaa-aaaa-aaaa-aaaa-000000000002';

-- ── Tritium RTM Annual tasks (template ...0008) ───────────────────────────────
-- Checklist from INS.1716 v8 Section 3.2 (18 items)
-- Charger Exterior: TR-01–TR-11 | Charger Interior: TR-12–TR-18
INSERT INTO pm_template_tasks
    (id, template_id, task_code, task_order, task_category,
     task_name, task_description, input_type,
     is_required, is_conditional, conditional_label, critical_fail, fail_guidance)
VALUES
  -- ── Charger Exterior ──────────────────────────────────────────────────────
  ('ffffffff-0008-0000-0000-000000000001',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-01', 1, 'Charger Exterior',
   'HMI screen',
   'Inspect HMI screen for cracks, damage, or touch-response failure. (INS.1716 §3.2 #4 — if damaged see FSP.090)',
   'pass_fail_action', true, false, NULL, false,
   'If damaged: see FSP.090 for screen replacement procedure.'),

  ('ffffffff-0008-0000-0000-000000000002',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-02', 2, 'Charger Exterior',
   'HMI buttons',
   'Inspect and test all HMI buttons for physical damage or failure to actuate. (INS.1716 §3.2 #5 — if damaged see FSP.2241)',
   'pass_fail_action', true, false, NULL, false,
   'If damaged: see FSP.2241 for HMI button replacement procedure.'),

  ('ffffffff-0008-0000-0000-000000000003',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-03', 3, 'Charger Exterior',
   'CCR / RFID reader',
   'Inspect CCR/RFID reader for physical damage and verify operation with a known valid card. (INS.1716 §3.2 #6 — if damaged see FSP.1626 / 1708)',
   'pass_fail_action', true, false, NULL, false,
   'If damaged: see FSP.1626 (CCR) or FSP.1708 (RFID reader) for replacement procedure.'),

  ('ffffffff-0008-0000-0000-000000000004',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-04', 4, 'Charger Exterior',
   'Exterior panels',
   'Clean exterior panels and inspect for physical damage, corrosion, or deformation. Report damage — do not field-repair panels. (INS.1716 §3.2 #7)',
   'pass_fail_action', true, false, NULL, false,
   'Clean panels. Report any structural damage or significant corrosion.'),

  ('ffffffff-0008-0000-0000-000000000005',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-05', 5, 'Charger Exterior',
   'DC Meter display enclosure',
   'Clean DC meter display enclosure and inspect for damage or seal failure. Report any damage. (INS.1716 §3.2 #8)',
   'pass_fail_action', true, false, NULL, false,
   'Clean enclosure. Report any cracking, seal failure, or display damage.'),

  ('ffffffff-0008-0000-0000-000000000006',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-06', 6, 'Charger Exterior',
   'Charge cables',
   'Inspect all charge cables for cuts, abrasion, kinking, connector damage, or jacket failure. Replace if damaged. (INS.1716 §3.2 #9 — see FSP.088 / FSP.089)',
   'pass_fail_action', true, false, NULL, false,
   'Replace damaged cable: see FSP.088 (charge cable) and FSP.089 (cable management retrieval cord). Record in Parts Replaced.'),

  ('ffffffff-0008-0000-0000-000000000007',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-07', 7, 'Charger Exterior',
   'Status indicator, downlights, 4G antenna',
   'Inspect status indicator light, downlights, and 4G antenna for physical damage or operational failure. (INS.1716 §3.2 #11 — if damaged see FSP.1547)',
   'pass_fail_action', true, false, NULL, false,
   'If damaged: see FSP.1547 for replacement procedure.'),

  ('ffffffff-0008-0000-0000-000000000008',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-08', 8, 'Charger Exterior',
   'Main radiator fan',
   'Clean main radiator fan blades and housing. Inspect for damage, excessive noise, or bearing wear. (INS.1716 §3.2 #12 — if damaged see FSP.1648)',
   'pass_fail_action', true, false, NULL, false,
   'Clean fan. If damaged: see FSP.1648 for fan replacement procedure.'),

  ('ffffffff-0008-0000-0000-000000000009',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-09', 9, 'Charger Exterior',
   'Main radiator',
   'Clean main radiator fins and inspect for damage, corrosion, or fin blockage affecting airflow. (INS.1716 §3.2 #13 — if damaged see FSP.2240)',
   'pass_fail_action', true, false, NULL, false,
   'Clean radiator fins. If damaged: see FSP.2240 for radiator replacement procedure.'),

  ('ffffffff-0008-0000-0000-000000000010',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-10', 10, 'Charger Exterior',
   'Radiator coolant hose & UV protection sleeves',
   'Inspect radiator coolant hose for cracks, leaks, or swelling. Inspect UV protection sleeves for deterioration. '
   'Replacement schedule: 2 years (high usage) / 4 years (low usage) — see FSP.2489. (INS.1716 §3.2 #1)',
   'pass_fail_action', true, false, NULL, false,
   'If damaged or due for replacement: see FSP.2489. Install UV protection sleeves on all replacements. Record in Parts Replaced.'),

  ('ffffffff-0008-0000-0000-000000000011',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-11', 11, 'Charger Exterior',
   'Door latches',
   'Inspect all door latches for proper engagement, wear, or damage. Verify doors close and seal correctly. (INS.1716 §3.2 #16 — if damaged see FSP.2238)',
   'pass_fail_action', true, false, NULL, false,
   'If damaged: see FSP.2238 for door latch replacement procedure.'),

  -- ── Charger Interior ──────────────────────────────────────────────────────
  ('ffffffff-0008-0000-0000-000000000012',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-12', 12, 'Charger Interior',
   'Door seal foam',
   'Inspect door seal foam for tears, compression set, missing sections, or loss of sealing effectiveness. (INS.1716 §3.2 #2 — if damaged see FSP.2236)',
   'pass_fail_action', true, false, NULL, false,
   'If damaged: see FSP.2236 for door seal foam replacement procedure.'),

  ('ffffffff-0008-0000-0000-000000000013',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-13', 13, 'Charger Interior',
   'Door seal EMC gasket',
   'Inspect EMC gasket for continuity, gaps, compression set, or physical damage. EMC integrity is required for certification compliance. Report if damaged. (INS.1716 §3.2 #3)',
   'pass_fail', true, false, NULL, false,
   'Report any EMC gasket damage immediately. Do not return to service with a compromised EMC gasket without engineering authorisation.'),

  ('ffffffff-0008-0000-0000-000000000014',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-14', 14, 'Charger Interior',
   'Internal fans',
   'Inspect all internal cooling fans for proper operation, unusual noise, vibration, or obstructions. (INS.1716 §3.2 #10 — if damaged see FSP.1650)',
   'pass_fail_action', true, false, NULL, false,
   'If damaged: see FSP.1650 for internal fan replacement procedure.'),

  ('ffffffff-0008-0000-0000-000000000015',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-15', 15, 'Charger Interior',
   'Coolant level',
   'Check coolant level in the cooling circuit reservoir. Top up (re-juice) with approved coolant if required. (INS.1716 §3.2 #14 — pump replacement see FSP.1643)',
   'pass_fail_action', true, false, NULL, false,
   'Top up coolant if low. If level drops rapidly between visits, inspect for leaks before refilling. Record any top-up in Parts Replaced.'),

  ('ffffffff-0008-0000-0000-000000000016',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-16', 16, 'Charger Interior',
   'Internal heat exchanger',
   'Inspect internal heat exchanger for physical damage, coolant leaks, or fouling of heat transfer surfaces. (INS.1716 §3.2 #15 — if damaged see FSP.2239)',
   'pass_fail_action', true, false, NULL, false,
   'If damaged: see FSP.2239 for heat exchanger replacement procedure.'),

  ('ffffffff-0008-0000-0000-000000000017',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-17', 17, 'Charger Interior',
   'Torque check — input cable connections',
   'Inspect and retorque all input cable connections per Tritium torque specifications. '
   'LOTO REQUIRED — fully de-energize charger before opening cabinet. (INS.1716 §3.2 #17)',
   'completed', true, false, NULL, false,
   'LOTO must be in place before opening cabinet. Retorque to Tritium specification. Document any loose connections found.'),

  ('ffffffff-0008-0000-0000-000000000018',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000008',
   'TR-18', 18, 'Charger Interior',
   'SPD — status check',
   'Inspect the surge protection device (SPD) status indicator window. Verify SPD is operational and not in a fault or end-of-life state. (INS.1716 §3.2 #18)',
   'pass_fail', true, false, NULL, false,
   'If SPD indicator shows fault or replacement required, replace SPD before returning charger to service.')
ON CONFLICT (id) DO NOTHING;

-- ── v3.2: AC input torque checks on annual PMs ────────────────────────────────
-- Autel Annual: add AC input torque verification (Autel manual §5.5.2)
INSERT INTO pm_template_tasks
    (id, template_id, task_code, task_order, task_category,
     task_name, task_description, input_type, is_required, is_conditional,
     critical_fail, fail_guidance)
VALUES
  ('cccccccc-0003-0000-0000-000000000006',
   'bbbbbbbb-bbbb-bbbb-bbbb-000000000003',
   'A-03', 6, 'Electrical',
   'Torque check — AC input connections',
   'Verify torque on the AC input power cable terminations and PE wire busbar fasteners: '
   '15.12 ± 1.84 ft·lb (20.5 ± 2.5 N·m) per Autel MaxiCharger manual §5.5.2. '
   'LOTO REQUIRED — fully de-energize charger before opening cabinet.',
   'completed', true, false, false,
   'LOTO must be in place before opening cabinet. Retorque any loose connections to spec and document them.')
ON CONFLICT (id) DO NOTHING;

-- Tritium TR-17: embed the explicit torque value (idempotent UPDATE)
UPDATE pm_template_tasks
SET task_description =
      'Inspect and retorque all AC input cable connections — L1/L2/L3 and Earth M8 lugs to 20 Nm '
      '(Tritium INS.046 Cable Termination; INS.1716 §3.2 #17). Apply a torque mark to stud and nut. '
      'LOTO REQUIRED — fully de-energize charger before opening cabinet.'
WHERE id = 'ffffffff-0008-0000-0000-000000000017'
  AND task_description NOT LIKE '%20 Nm%';

-- ══════════════════════════════════════════════════════════════════════════════
-- v3.2: EVSE registry consolidation — make public.chargers the single source of
-- truth for the dashboard (display name, connector types, manufacturer/platform).
-- Previously these lived in app/constants.py + runtime_overrides.json (a VPS file
-- that a container rebuild wiped). All backfills below are guarded to fill blanks
-- ONLY, so they run once and never clobber later Admin-tab edits on restart.
-- ══════════════════════════════════════════════════════════════════════════════

-- Dashboard display name, separate from chargers.name (which Fleet/Maintenance uses).
ALTER TABLE chargers ADD COLUMN IF NOT EXISTS display_name TEXT;

-- Backfill operational display names for the 5 legacy units (was EVSE_DISPLAY).
UPDATE chargers SET display_name = 'ARG - Left'    WHERE external_id = 'as_cnIGqQ0DoWdFCo7zSrN01' AND display_name IS NULL;
UPDATE chargers SET display_name = 'ARG - Right'   WHERE external_id = 'as_c8rCuPHDd7sV1ynHBVBiq' AND display_name IS NULL;
UPDATE chargers SET display_name = 'Delta - Left'  WHERE external_id = 'as_xTUHfTKoOvKSfYZhhdlhT' AND display_name IS NULL;
UPDATE chargers SET display_name = 'Delta - Right' WHERE external_id = 'as_oXoa7HXphUu5riXsSW253' AND display_name IS NULL;
UPDATE chargers SET display_name = 'Glennallen'    WHERE external_id = 'as_LYHe6mZTRKiFfziSNJFvJ' AND display_name IS NULL;
-- Everything else (CL-A/B/C, future units) falls back to its Fleet name.
UPDATE chargers SET display_name = name WHERE display_name IS NULL AND name IS NOT NULL;

-- Per-port connector types (was CONNECTOR_TYPE). Delta conn 1's historical
-- CHAdeMO→NACS change (2026-01-30) stays a code special-case in constants.py;
-- the table holds the current state.
UPDATE chargers SET connector_types = '{"1":"CCS","2":"CCS"}'::jsonb
  WHERE external_id = 'as_c8rCuPHDd7sV1ynHBVBiq'  AND (connector_types IS NULL OR connector_types = '{}'::jsonb);
UPDATE chargers SET connector_types = '{"1":"NACS","2":"CCS"}'::jsonb
  WHERE external_id IN ('as_cnIGqQ0DoWdFCo7zSrN01','as_LYHe6mZTRKiFfziSNJFvJ','as_oXoa7HXphUu5riXsSW253','as_xTUHfTKoOvKSfYZhhdlhT')
    AND (connector_types IS NULL OR connector_types = '{}'::jsonb);

-- CL-A/B/C are Alpitronic HYC400s — assign the unit type so the dashboard derives
-- manufacturer (and Fleet shows a Type instead of "—").
UPDATE chargers
  SET unit_type_id = (SELECT id FROM unit_types WHERE type_name = 'Alpitronics HYC_400UL' LIMIT 1)
  WHERE external_id IN ('as_DFszjoLCWkAmXd1a6S9aZ','as_Qd90MUEn4nq8AlS5OOYIS','as_w5M5mKhbjW6Guw5dItpTz')
    AND unit_type_id IS NULL;

-- ═══════════════════════════════════════════════════════════════════════════════
-- v3.2  Alpitronic (HYC400) OCPP error codes
-- Vendor error-code lookup for HYC400 units (CL-A/B/C/D). Tritium units keep
-- using tritium_error_codes; Status History / Export pick the table by the
-- station's manufacturer. Seeded from Alpitronic 'OCPP Error codes SW v3.0.x'.
-- solve_actions is stored for future use; not surfaced in the dashboard yet.
-- ═══════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS alpitronic_error_codes (
    error_code     INTEGER PRIMARY KEY,
    ocpp_name      TEXT,
    hyperlog_name  TEXT,
    description    TEXT,
    solve_actions  TEXT,
    severity       TEXT,
    change_log     TEXT
);
ALTER TABLE alpitronic_error_codes ENABLE ROW LEVEL SECURITY;

INSERT INTO alpitronic_error_codes
    (error_code, ocpp_name, hyperlog_name, description, solve_actions, severity, change_log)
VALUES
    (0, 'No Error', 'NoError', 'No error.', NULL, 'Info', NULL),
    (1, 'PLC Error', 'PLC_Error', 'Communication failure with powerline modem of CCS connector, Powerline modem does not respond or communication gets interrupted', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (2, 'SLAC Error', 'DEPRECATED__SLAC_Timeout', 'Timeout while waiting for SLAC match', 'Reconnect vehicle, Move EV, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (3, 'SLAC interrupted', 'SLAC_Interrupted', 'Connection interrupted while waiting for SLAC match,Cable disconnect has been detected. Loose connection / Charge cable may not be plugged in correctly', 'Reconnect vehicle, Move EV, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (4, 'Link Error', 'Link_Timeout', 'Timeout while waiting for GPhy LINK after SLAC match', 'Reconnect vehicle, Move EV, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (5, 'Link Error', 'Link_Interrupted', 'Connection interrupted while waiting for GPhy LINK after SLAC match, Loose connection / Charging cable may not be plugged in correctly', 'Reconnect vehicle, Move EV, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (6, 'SDP Error', 'SDP_Timeout', 'Timeout waiting for SECCDiscover request from EV', 'Reconnect vehicle, Move EV, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (7, 'SDP Error', 'SDP_Interrupted', 'Cable disconnect detected while waiting for SECCDiscover request, Loose connection / Charging cable may not be plugged in correctly', 'Reconnect vehicle, Move EV, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (8, 'TCP Error', 'TCP_AcceptError', 'TCP socket to EV failed', 'Reconnect vehicle, Move EV, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (9, 'TCP Error', 'TCP_EmptyPacket', 'TCP socket to EV failed', 'Reconnect vehicle, Move EV, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (10, 'TCP Error', 'TCP_Error', 'TCP socket to EV failed', 'Reconnect vehicle, Move EV, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (11, 'TCP Error', 'TCP_POLLERR', 'POLLERR flag active while reading TCP socket. Usually indicating a Reset-flag (RST - 0x04) sent by the EV. Reset, indicates a harsh shutdown. Usually to be used in case of errors', 'Reconnect vehicle, Move EV, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (12, 'TCP Error', 'TCP_POLLHUP', 'POLLHUP flag active. Connection terminated by local low-level socket handling. Usually due to an internal socket handling error', 'Reconnect vehicle, Move EV, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (13, 'TCP Error', 'TCP_POLLRDHUP', 'POLLRDHUP flag active. Usually indicating a Finish-flag (FIN - 0x01) sent by the EV. Finish indicates an intended termination of the socket connection in a good case scenario. However, FIN is often used instead of RST, even in error scenarios', 'Reconnect vehicle, Move EV, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (14, 'V2G Sequence Error', 'V2G_Error', 'V2G Sequence error, The received V2G message is not allowed during the current state', 'Reconnect vehicle, Move EV, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (15, 'Connector Lock Fault', 'Lock_Fault', 'A connector lock fault was reported by the EV (CCS: Field: EVError, CHAdeMO: control signal on cable, monitored and mapped on a CAN signal by CTRL_IO). Charging cable may not be plugged in correctly / Failure of the locking actuator', 'Reconnect vehicle / Resend connector unlock command via Backend, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (16, 'Connector Lock Fault', 'Lock_OpenLoad', 'Connector locking circuit detected no current flow. Locking actuator not connected properly to CTRL_IO board (KF3) / Failure of the locking actuator', 'Reconnect vehicle / Resend connector unlock command via Backend, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (17, 'Connector Lock Fault', 'Lock_Overcurrent', 'Connector locking circuit detected a high current flow, exceeding the permitted limits. Charging cable may not be plugged in correctly / Failure of the connector locking actuator', 'Reconnect vehicle / Resend connector unlock command via Backend, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (18, 'Isolation Fault', 'ISO_Fault', 'The RCD device reported an error, Residual current device (RCD) tripped', 'Reconnect vehicle, Soft reset, Hard reset, Try different vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (19, 'Stack Error', 'Stack_Error', 'A general Power-Stack error has been diagnosed. Log files need to be analysed for further details, at least one Power-Stack is in fault state. Check individual errors - if any', 'Reconnect vehicle, Hard reset, Try different vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (20, 'Cable Error', 'Cable_Error_unknown', 'Cable cooling unit error, Cable not properly connected to CTRL_IO board (KF3) / Cable damaged', 'Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (23, 'Authorization Timeout', 'Authorization_Timeout', 'The customer did not authenticate in time', 'Reconnect vehicle', 'Info', NULL),
    (24, 'Isolation Fault', 'ISO_Fault_Check', 'An isolation fault was detected during isolation check', 'Reconnect vehicle, Soft reset, Hard reset, Try different vehicle', 'Error', NULL),
    (25, 'Isolation Fault', 'ISO_Fault_Charging', 'An isolation fault was detected during an ongoing power transfer', 'Reconnect vehicle, Soft reset, Hard reset, Try different vehicle', 'Error', NULL),
    (26, 'Isolation Fault', 'Discharge_Slow', 'Overall timeout of 20s for CH_SUB_03 (insulation/latch check) and the subsequent output voltage descent was exceeded', 'Reconnect vehicle, Soft reset, Hard reset, Try different vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (27, 'Isolation Fault', 'Output_Voltage_Low', 'Output voltage too low to perform isolation check', 'Reconnect vehicle, Hard reset, Try different vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (28, 'Isolation Warning', 'ISO_Warning_Check', 'An isolation warning was detected during isolation check, Isolation resistance dropped below warning threshold', 'No further action required', 'Error', NULL),
    (29, 'Isolation Warning', 'ISO_Warning_Charging', 'An isolation warning was detected during an ongoing power transfer, Isolation resistance dropped below warning threshold', 'No further action required', 'Info', NULL),
    (30, 'Coolant level low', 'Cooler_Error_Level', 'Cable cooling unit: coolant level low', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (31, 'Cooler Error', 'Cooler_Error_Communication', 'Communication with cable cooling unit failed', 'Fault analysis through alpitronic, Technician on site', 'Error', 'Updated'),
    (32, 'Cooler Error', 'Cooler_Error_Fan1', 'Cable cooling unit: Fan 1 defective', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (33, 'Cooler Error', 'Cooler_Error_Fan2', 'Cable cooling unit: Fan 2 defective', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (34, 'Cooler Error', 'Cooler_Error_Temp_Sensor', 'Cable cooling unit: temperature sensor failed', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (35, 'Cooler Error', 'Cooler_Error_Flow_Sensor', 'Cable cooling unit: flow sensor failed', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (36, 'Cooler Error', 'Cooler_Error_Level_Sensor', 'Cable cooling unit: coolant level sensor failed', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (37, 'Cooler Physical Exclusivity', 'Cooler_Physical_Exclusivity', 'Cooling unit is used by another cable and it is physical exclusive', NULL, 'Info', NULL),
    (38, 'Logical Exclusivity', 'Logical_Exclusivity', 'Another connector of a configured logical exclusivity group is used', NULL, 'Info', 'Updated'),
    (39, 'Surge Arrester Error', 'Surge_Arrester_Error', 'Surge arrester device indicated a fault condition', NULL, 'Error', NULL),
    (40, 'Cable Plugged during ISO Selftest', 'ISO_Selftest_Cable_Plugged', 'A connector was re-plugged into the EV during an ongoing isolation self test', NULL, 'Error', NULL),
    (41, 'Meter Pairing Error', 'Meter_Error_Pairing', 'Pairing with meter failed', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (42, 'Meter Eichlog full', 'Meter_Error_Eichlog_Full', 'Meter log is full', 'Technician on site', 'Error', NULL),
    (43, 'Meter communication Error', 'Meter_Error_Communication', 'Communication with meter failed. In case of TYPE 2: RCD Tripped', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (44, 'Meter fatal Error', 'Meter_Error_Fatal', 'Meter reported fatal error', 'Technician on site', 'Error', NULL),
    (45, 'Meter full charging Error', 'Meter_Error_Charging_Full', 'Meter charge log full', 'Technician on site', 'Error', NULL),
    (46, 'Meter no pairing Error', 'Meter_Error_No_Pairing_Executed', 'Meter is not paired with SECC', 'Technician on site', 'Error', NULL),
    (47, 'Meter latest start charge refused Error', 'Meter_Error_Latest_Start_Charge_Refused', 'Error while trying to start a metering session', NULL, 'Error', NULL),
    (48, 'Meter Pairing Controller Error', 'Meter_Error_Pairing_Controller', 'Pairing controller (part of the hypercharger service) indicates an error', 'Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (50, 'EV voltage present before start charging', 'EV_Error_VoltagePresent', 'Voltage > 60 V Measured at charging connector before charging session has been initialized', 'Reconnect vehicle, Soft reset, Hard reset, Try different vehicle, Fault analysis through alpitronic, Technician on site', 'Error', 'Updated'),
    (51, 'EV Error other', 'EV_Error_Other', 'The EV has reported an error', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (52, 'EV Wrong voltage requested', 'EV_Error_WrongVoltageReq', 'The EV requested an abnormal (out of bounds) target voltage', 'Reconnect vehicle, Soft reset, Hard reset, Try different vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (53, 'EV Wrong CP state', 'EV_Error_WrongCPState', 'The EV requested power delivery, but its CP status is not C or D', 'Reconnect vehicle, Soft reset, Hard reset, Try different vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (54, 'EV Wrong precharge voltage', 'EV_Error_WrongVoltagePrecharge', 'The EV requested an abnormal (out of bounds) target voltage', 'Reconnect vehicle, Soft reset, Hard reset, Try different vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (55, 'EV No current flow', 'EV_Error_NoCurrentFlow', 'No current flow to EV', 'Reconnect vehicle, Soft reset, Hard reset, Try different vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (60, 'Cable communication Error', 'Cable_Error_Communication', 'CAN timeout for cable-related message', 'Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (61, 'Cable leakage Error', 'Cable_Error_Leakage', 'Leakage of cooling liquid detected', 'Technician on site', 'Error', NULL),
    (62, 'Cable temphigh Error', 'Cable_Error_TempHigh', 'Any of the measured temperatures is too high', 'Reconnect vehicle', 'Error', NULL),
    (63, 'Cable tempDelta Error', 'Cable_Error_TempDelta', 'Temperature delta between DC+ and DC- too high', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (64, 'Cable tempSensorDc Error', 'Cable_Error_TempSensorDc', 'Summarized temperature status', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (65, 'Cable tempSensorHousing Error', 'Cable_Error_TempSensorHousing', 'Housing temperature status', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (66, 'Cable HW fault Error', 'Cable_Error_HW_Fault', 'Cable Good signal reported (non-critical) error, not covered by the errors above', NULL, 'Error', NULL),
    (67, 'Cable critical lever Error', 'Cable_Error_CriticalLevel', 'Cable Good signal reported critical error', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (68, 'Cable theft', 'Cable_Error_Theft', 'Posible cable theft/vandalisation was detected', NULL, 'Error', NULL),
    (70, 'CHAdeMo timeout 1 Error', 'CHAdeMO_Error_Timeout1', 'Timeout of 120s expired while waiting for vehicle CAN communication being initiated by the EV', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (71, 'CHAdeMo timeout 3 Error', 'CHAdeMO_Error_Timeout3', 'Timeout of 8s expired while waiting for activation of both redundant enable charging signals by the EV', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (72, 'CHAdeMo timeout 5 Error', 'CHAdeMO_Error_Timeout5', 'EV contactors weren''t closed within 4s after activating switch d2 (charge sequence signal 2)', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (73, 'CHAdeMo timeout 6 Error', 'CHAdeMO_Error_Timeout6', 'Precharge and closing of charging station output contactors wasn''t completed within 4s after closing of EV contactors', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (74, 'CHAdeMo timeout 9 Error', 'CHAdeMO_Error_Timeout9', 'EV failed to reset the Vehicle charge permission (opto coupler j) within 4s after the transmission of an active chargingstop control-flag', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (75, 'CHAdeMo timeout 11 Error', 'CHAdeMO_Error_Timeout11', 'EV contactor welding detection not completed within 10s', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (76, 'CHAdeMo timeout 14 Error', 'CHAdeMO_Error_Timeout14', 'After completion of the welding detection and deactivation of j1 and j2, an unenergized state wasn''t detected within 4s (CHAdeMO version < v1.1 → 6s)', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (77, 'CHAdeMo timeout 15 Error', 'CHAdeMO_Error_Timeout15', 'The EV didn''t stop transmitting its CAN messages within 2.5s (3s for CHAdeMO version < v1.1>)', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (78, 'CHAdeMo can timeout Error', 'CHAdeMO_Error_Timeout_CAN', 'Either of the CAN messages expected from the vehicle wasn''t received in time (timeout: 400ms), while monitoring routine (CH_SUB_06) was active', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (79, 'CHAdeMo vehicle normal stop Error', 'CHAdeMO_Error_VehicleNormalStop', 'Vehicle set the Normal stop request beforecharging-flag (CAN message 102)', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (80, 'CHAdeMO vehicle fault flags voltage Error', 'CHAdeMO_Error_VehicleFaultFlags_Voltage', 'The EV set the over-voltage and under-voltage flags (CAN message 0x102), indicating that the measured battery voltage has exceeded/fallen below the upper/lower voltage limits (defined by EV). This error can also show a problem between the measured battery voltage (EV measurement) and the reported output present current (measured by the charging station).', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (81, 'CHAdeMO vehicle fault flags current Error', 'CHAdeMO_Error_VehicleFaultFlags_Current', 'current deviation-flag was set by EV (CAN message 0x102)', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (82, 'CHAdeMO vehicle fault flags temperature Error', 'CHAdeMO_Error_VehicleFaultFlags_Temperature', 'High battery temperature-flag was set by EV (CAN message 0x102). This flag is set when the battery temperature exceeds a pre-defined temperature limit.', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (83, 'CHAdeMO vehicle fault flags charging system Error', 'CHAdeMO_Error_VehicleFaultFlags_ChargingSystem', 'The EV detected either a vehicle error or a charging station error', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (84, 'CHAdeMO vehicle fault flags shift position Error', 'CHAdeMO_Error_VehicleFaultFlags_ShiftPosition', 'The gear lever isn''t set to parking', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (90, 'GBT wrong protocol version Error', 'GBT_Error_WrongProtocolVersion', 'GB/T protocol version error', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (91, 'GBT BRM timeout Error', 'GBT_Error_BRM_Timeout', 'GB/T BRM timeout', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (92, 'GBT BCP timeout Error', 'GBT_Error_BCP_Timeout', 'GB/T BCP timeout', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (93, 'GBT BRO 00 timeout Error', 'GBT_Error_BRO_00_Timeout', 'GB/T BRO timeout', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (94, 'GBT BRO AA timeout Error', 'GBT_Error_BRO_AA_Timeout', 'GB/T BRO timeout', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (95, 'GBT BCS timeout Error', 'GBT_Error_BCS_Timeout', 'GB/T BCS timeout', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (96, 'GBT BCL timeout Error', 'GBT_Error_BCL_Timeout', 'GB/T BCL timeout', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (97, 'GBT BSM timeout Error', 'GBT_Error_BSM_Timeout', 'GB/T BSM timeout', 'Reconnect vehicle, Soft reset, Hard reset, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (100, 'Stack duplicate Error', 'Stack_Error_Duplicate', 'Power module configuration error', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (101, 'Stack allocation timeout', 'Stack_Allocation_Timeout', 'The stacks weren''t allocated to a connector in time. Usually, the stack is allocated between the authorisation and isolation check.', NULL, 'Error', NULL),
    (150, 'Output disabled iso fault', 'Output_Disabled_IsoFault', 'Connector will be locked from now on, due to a confirmed isolation fault', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (151, 'Output disabled iso fault persistent', 'Output_Disabled_IsoFaultPersistent', 'The connector is persistently locked, i.e. even after a service restart due to a previous lock condition (Output_Disabled_IsoFault, Output_Disabled_IsoFault Persistent)', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (152, 'Output disabled iso warning count', 'Output_Disabled_IsoWarningCount', 'Connector will be locked from now on, due to a high number of isolation warnings during previous isolation checks', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (153, 'Output disabled overtemperature shutdown count', 'Output_Disabled_OverTempShutdownCount', 'Connector will be locked from now on, due to overtemperature shutdowns in two consecutive charging sessions', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (160, 'Output relay plus welded', 'Output_Relay_Plus_Welded', 'A welded DC+ contactor was diagnosed during output contactor welding check', NULL, 'Error', NULL),
    (161, 'Output relay minus welded', 'Output_Relay_Minus_Welded', 'A welded DC- contactor was diagnosed during output contactor welding check', NULL, 'Error', NULL),
    (162, 'Partitioning relay welded', 'Partitioning_Relay_Welded', 'A welded bridge relay (stack interconnection) was diagnosed during stack deallocation', NULL, 'Error', NULL),
    (163, 'SSM_Output_Relay_Mulfunction', 'SSM_Output_Relay_Mulfunction', 'An unexpected state was detected on SSM output relay feedback', NULL, 'Error', 'Added'),
    (170, 'Serial_Port_Open', 'Serial_Port_Open', 'Could not open serial port', NULL, 'Error', NULL),
    (171, 'Serial_Port_Configure', 'Serial_Port_Configure', 'Error while configuring serial port', NULL, 'Error', NULL),
    (172, 'Serial_Port_Rs485', 'Serial_Port_Rs485', 'Error while configuring serial port for RS485 mode', NULL, 'Error', NULL),
    (173, 'Serial_Port_Poll_Error', 'Serial_Port_Poll_Error', 'Serial port error while polling', NULL, 'Error', NULL),
    (174, 'Serial_Port_Unexpected_Poll_Event', 'Serial_Port_Unexpected_Poll_Event', 'Serial port error while polling', NULL, 'Error', NULL),
    (175, 'Serial_Port_Timeout', 'Serial_Port_Timeout', 'Timeout while waiting for serial port data', NULL, 'Error', NULL),
    (176, 'Serial_Port_Write_Error', 'Serial_Port_Write_Error', 'Error while trying to write data to serial port', NULL, 'Error', NULL),
    (177, 'Serial_Port_Read_Error', 'Serial_Port_Read_Error', 'Error while trying to read data from serial port', NULL, 'Error', NULL),
    (180, 'DzgMeteringBus_Timeout', 'DzgMeteringBus_Timeout', 'Timeout on the DZG metering bus: no data received', NULL, 'Error', NULL),
    (190, 'DzgProtocol_Fewer_Bytes_Than_Minimum', 'DzgProtocol_Fewer_Bytes_Than_Minimum', 'DZG protocol error - Error constructing frame from bytes: too few bytes', NULL, 'Error', NULL),
    (191, 'DzgProtocol_Fewer_Bytes_Than_Frame_Element_Length', 'DzgProtocol_Fewer_Bytes_Than_Frame_Element_Length', 'DZG protocol error - Error constructing frame from bytes: fewer bytes in frame than claimed by frame element length', NULL, 'Error', NULL),
    (192, 'DzgProtocol_Trailing_Bytes_Detected', 'DzgProtocol_Trailing_Bytes_Detected', 'DZG protocol error - Error constructing frame from bytes: trailing bytes detected', NULL, 'Error', NULL),
    (193, 'DzgProtocol_Checksum_Mismatch', 'DzgProtocol_Checksum_Mismatch', 'DZG protocol error - Error constructing frame from bytes: checksum mismatch', NULL, 'Error', NULL),
    (194, 'DzgProtocol_Request_Response_Mismatch', 'DzgProtocol_Request_Response_Mismatch', 'DZG protocol error - Received structurally mismatching frame in read attempt loop', NULL, 'Error', NULL),
    (195, 'DzgProtocol_Payload_Error', 'DzgProtocol_Payload_Error', 'DZG protocol error - Generic error while parsing meter message', NULL, 'Error', NULL),
    (196, 'Authorization_Failed', 'Authorization_Failed', 'Plug and Charge authorization declined by backend', NULL, 'Error', NULL),
    (197, 'Sequence_Error', 'Sequence_Error', 'EV sent a message which was not expected at that time', NULL, 'Error', NULL),
    (198, 'EmergencyStopCP_Unplug', 'EmergencyStopCP_Unplug', 'Unexpected CP state transition (X → A) indicating an unplug event', NULL, 'Error', NULL),
    (199, 'EmergencyStopCP_CPError', 'EmergencyStopCP_CPError', 'Unexpected CP state transition (X → E|F) indicating an error state', NULL, 'Error', NULL),
    (200, 'EmergencyStopCP_BeforeChargingLoop', 'EmergencyStopCP_BeforeChargingLoop', 'Unexpected CP state transition before charging loop(C → B) indicating an EV-initiated emergency stop', NULL, 'Error', NULL),
    (201, 'EmergencyStopCP_DuringChargingLoop', 'EmergencyStopCP_DuringChargingLoop', 'Unexpected CP state transition during charging loop(C → B) indicating an EV-initiated emergency stop', NULL, 'Error', NULL),
    (202, 'EmergencyStopCP_AfterChargingLoop', 'EmergencyStopCP_AfterChargingLoop', 'Unexpected CP state transition after charging loop(C → B) indicating an EV-initiated emergency stop', NULL, 'Error', NULL),
    (203, 'EmergencyStopCP_Other', 'EmergencyStopCP_Other', 'Other errors related to unexpected CP states, not covered by the error codes above', NULL, 'Error', NULL),
    (204, 'EmergencyStopPP_ContinuityLoss', 'EmergencyStopPP_ContinuityLoss', '(CCS1) Unexpected PP state transition to unconnected during ongoing TCP communication', NULL, 'Error', NULL),
    (207, 'EmergencyStopCP_WrongTeardownOrderOnUserStop', 'EmergencyStopCP_WrongTeardownOrderOnUserStop', 'Emergency stop activated due to incorrect teardown order during user-initiated stop', NULL, 'Error', NULL),
    (208, 'EmergencyStopCP_PrematureUnplugAfterChargingLoop', 'EmergencyStopCP_PrematureUnplugAfterChargingLoop', 'Emergency stop triggered due to premature unplugging during the charging loop', NULL, 'Error', NULL),
    (209, 'StackSupplyDisabled_OverTempShutdown', 'StackSupplyDisabled_OverTempShutdown', 'Stack supply disabled due to overtemperature event', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (210, 'StackSupplyDisabled_FastEmergencyStop', 'StackSupplyDisabled_FastEmergencyStop', 'Stack supply disabled due to fast emergency stop, caused by unexpected CP/PP state', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (220, 'SLAC_Timeout_ParamReq_AfterPlugin', 'SLAC_Timeout_ParamReq_AfterPlugin', 'EV didn''t initiate SLAC (by sending CM_SLAC_PARM.REQ) after cable was plugged in', NULL, 'Error', NULL),
    (221, 'SLAC_Timeout_ParamReq_AfterEvRestart', 'SLAC_Timeout_ParamReq_AfterEvRestart', 'EV didn''t initiate SLAC (by sending CM_SLAC_PARM.REQ) after a restart initiated by the EV (e.g.: by sending a BCB-toggle after a terminated session)', NULL, 'Error', NULL),
    (222, 'SLAC_Timeout_ParamReq_AfterEvseRestart', 'SLAC_Timeout_ParamReq_AfterEvseRestart', 'EV didn''t initiate SLAC (by sending CM_SLAC_PARM.REQ) after a restart initiated by the charger (e.g.: by sending a B1-B2-toggle / BEB-toggle / BFB-toggle after a terminated session)', NULL, 'Error', NULL),
    (223, 'SLAC_Timeout_ParamReq_Other', 'SLAC_Timeout_ParamReq_Other', 'EV didn''t initiate SLAC (by sending CM_SLAC_PARM.REQ) in other cases, not covered by the more specific error codes mentioned above', NULL, 'Error', NULL),
    (224, 'SLAC_Timeout_StartAttnCharInd', 'SLAC_Timeout_StartAttnCharInd', 'EV didn''t send CM_START_ATTEN_CHAR.IND afer CM_SLAC_PARM.REQ was exchanged', NULL, 'Error', NULL),
    (225, 'SLAC_Timeout_AttnCharRsp', 'SLAC_Timeout_AttnCharRsp', 'EV didn''t reply to CM_ATTEN_CHAR.IND by sending CM_ATTEN_CHAR.RSP', NULL, 'Error', NULL),
    (226, 'SLAC_Timeout_ValidateReq', 'SLAC_Timeout_ValidateReq', 'EV didn''t send a valid CM_VALIDATE.REQ (step 2) after CM_VALIDATE (step 1) was exchanged', NULL, 'Error', NULL),
    (227, 'SLAC_Timeout_MatchReq', 'SLAC_Timeout_MatchReq', 'EV didn''t send CM_SLAC_MATCH.REQ after CM_ATTEN_CHAR was exchanged', NULL, 'Error', NULL),
    (228, 'SLAC_Error_Other', 'SLAC_Error_Other', 'Other SLAC errors not covered by more specific SLAC-related error codes', NULL, 'Error', NULL),
    (250, 'TLSHandshakeError', 'TLSHandshakeError', 'TLS handshake failure (usually due to missing V2GRoot certificate)', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (251, 'CertificateInstallationServerTimeout', 'CertificateInstallationServerTimeout', 'Contract certificate installation via OCPP taking more than 4s', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (252, 'V2GContractCertificateExpired', 'V2GContractCertificateExpired', 'Contract certificate exceeded its validity period', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (253, 'V2GContractCertificateNotYetValid', 'V2GContractCertificateNotYetValid', 'Contract certificate did not reach its validity period yet', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (254, 'V2GContractCertificateRevoked', 'V2GContractCertificateRevoked', 'Contract certificate was revoked by CA, leading to unsuccessful OCSP check', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (255, 'V2GContractCertificateVerificationError', 'V2GContractCertificateVerificationError', 'Could not verify the certificate chain.', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (256, 'V2GContractCertificateNotAllowed', 'V2GContractCertificateNotAllowed', 'The contract certificate / eMAID is not allowed to charge on this charging station.', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (257, 'V2GContractCertificateNotAvailable', 'V2GContractCertificateNotAvailable', 'No contract certificate is available for certificate installation', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (258, 'V2GSignatureInvalid', 'V2GSignatureInvalid', 'The XML signature of the V2G-message is invalid', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (270, 'S3ButtonPressed', 'S3ButtonPressed', 'User stopped session by pressing S3 button. Reason: Either due to a session error or due to misuse of the S3 button', 'No further action required', 'Error', NULL),
    (271, 'PP_LockingFailed', 'PP_LockingFailed', 'CCS1 Connector is not securely plugged in. Please press firmly to ensure connector is latched to vehicle.Example: CP state signals ‘connected’, PP state signals ‘not connected’.', 'Reconnect vehicle', 'Error', NULL),
    (272, 'PP_LineBroken', 'PP_LineBroken', 'Try to Wake up EV through B1/B2 Toggle failed.', 'No further action required', 'Error', 'Added'),
    (280, 'Meter_StartCharge_InvalidUserId', 'Meter_StartCharge_InvalidUserId', 'User-ID sent to energy meter is unknown or invalid (e.g.: too long). ', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (281, 'Meter_StartCharge_NotReady', 'Meter_StartCharge_NotReady', 'Energy meter not ready for power transfer start (not in idle state, ...)', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (282, 'Meter_StartCharge_PersistentError', 'Meter_StartCharge_PersistentError', 'Power transfer start not possible due to persistent energy meter error', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (283, 'Meter_StartCharge_Failed', 'Meter_StartCharge_Failed', 'Energy meter trigger for power transfer start failed', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (284, 'Meter_StartCharge_Timeout', 'Meter_StartCharge_Timeout', 'Energy meter trigger for power transfer start timed out', 'Reconnect vehicle, Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (290, 'NoFreePower', 'NoFreePower', 'Connector/Charger power limit is too low. Reason: power limit from OCPP or load management parameters is too low', 'No further action required', 'Info', NULL),
    (291, 'NoFreeStacks', 'NoFreeStacks', 'No stack is available for this connector. Reason: charger configuration, FCFS, stack status.', 'No further action required', 'Info', NULL),
    (292, 'MaxNbOfParallelSessionsReached_InternalReason', 'MaxNbOfParallelSessionsReached_InternalReason', 'Internal maximum number of parallel charging sessions reached. Reason: Welding of partitioning relays.', 'No further action required', 'Info', NULL),
    (293, 'MaxNbOfParallelSessionsReached_OCPP', 'MaxNbOfParallelSessionsReached_OCPP', 'Maximum number of parallel charging sessions set via OCPP parameter reached. ', 'No further action required', 'Info', NULL),
    (294, 'ControlPilotError', 'ControlPilotError', 'Unexpected CP state (E|F, INV) indicating an error state', 'No further action required', 'Info', NULL),
    (296, 'CCS Tesla Incompatbility Error', 'EV not CCS compatible', 'Error occurs by old Tesla EV''s', 'No further action required', 'Info', NULL),
    (297, 'AC PWM Current limit exceeded by EV', 'AC PWM Current limit exceeded by EV', 'EV draws more current than indicated by the PWM duty cycle for more than 5s', 'No further action required', 'Info', 'Added'),
    (300, 'ISO_Selftest_Overvoltage', 'ISO_Selftest_Overvoltage', 'Voltage during isolation-selftest exceeding 40V (expected: 30V)', 'No further action required', 'Error', NULL),
    (301, 'PowerController_CommunicationError', 'PowerController_CommunicationError', 'Communication error with the power cabinet', 'No further action required', 'Error', 'Added'),
    (302, 'PowerController_NotReady', 'PowerController_NotReady', 'The power cabinet is not yet ready for the requested action.', 'No further action required', 'Info', 'Added'),
    (350, 'Coolant level low', 'Cooler_Error_Level_Plausibility', 'Cable cooling unit: coolant level low', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (351, 'Cooler Error', 'Cooler_Error_Fan1_Plausibility', 'Cable cooling unit: Fan 1 defective', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (352, 'Cooler Error', 'Cooler_Error_Fan2_Plausibility', 'Cable cooling unit: Fan 2 defective', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (353, 'Cooler Error', 'Cooler_Error_Temp_Sensor_Plausibility', 'Cable cooling unit: temperature sensor failed', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (354, 'Cooler Error', 'Cooler_Error_Flow_Sensor_Plausibility', 'Cable cooling unit: flow sensor failed', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (355, 'Cooler Error', 'Cooler_Error_Temp_Sensor_HotSpot', 'Cable cooling unit: temperature sensor failed', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (356, 'Cooler Error', 'Cooler_Error_Flow_Sensor_HotSpot', 'Cable cooling unit: flow sensor failed', 'Fault analysis through alpitronic, Technician on site', 'Error', NULL),
    (1000, 'Door Closed', 'Door Closed', 'Closed signal from door contact switch, All doors of the charger have been closed', 'No further action required', 'Error', NULL),
    (1001, 'Door Opened', 'Door Opened', 'Open signal from door contact switch, At least one door of the charger got opened', 'Close all service doors', 'Error', NULL),
    (1002, 'Emergency StopButton Disengaged', 'Emergency StopButton Disengaged', 'Emergency stop button has been disengaged', 'No further action required', 'Error', NULL),
    (1003, 'Emergency StopButton Engaged', 'Emergency StopButton Engaged', 'Emergency stop button has been engaged.', 'Disengage emergency stop button', 'Error', NULL),
    (1004, 'Uncontrolled Reboot', 'Uncontrolled Reboot', 'Charger has experienced a reboot without undergoing proper shutdown procedures', NULL, 'Error', NULL),
    (1005, 'Unavailable Forced Persistently', 'Unavailable Forced Persistently', 'Connector is forced to unavailable persistently, e.g. by backend', NULL, 'Info', NULL),
    (1006, 'Unavailable Forced Temporarily', 'Unavailable Forced Temporarily', 'Connector is unavailable temporarily, e.g. due to configured physical exclusivity', NULL, 'Info', NULL)
ON CONFLICT (error_code) DO UPDATE SET
    ocpp_name     = EXCLUDED.ocpp_name,
    hyperlog_name = EXCLUDED.hyperlog_name,
    description   = EXCLUDED.description,
    solve_actions = EXCLUDED.solve_actions,
    severity      = EXCLUDED.severity,
    change_log    = EXCLUDED.change_log;
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    logger.info("Starting ReCharge Alaska Portal v3")
    pool = await create_pool()
    async with pool.acquire() as conn:
        await conn.execute(_MIGRATION_SQL)
    logger.info("DB migration complete")
    start_alert_thread()
    start_collector_scheduler()
    yield
    # ── Shutdown ─────────────────────────────────────────────────────────────
    stop_collector_scheduler()
    await close_pool()
    logger.info("Shutdown complete")


app = FastAPI(
    title="ReCharge Alaska Portal API",
    version="3.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["*"],
)

app.include_router(sessions.router)
app.include_router(analytics.router)
app.include_router(status.router)
app.include_router(connectivity.router)
app.include_router(export.router)
app.include_router(admin.router)
app.include_router(alerts_sse.router)
app.include_router(alerts_config.router)
app.include_router(alerts_push.router)
app.include_router(utility.router)
app.include_router(maintenance.router)
app.include_router(me.router)
app.include_router(connector_count.router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "3.1.0"}
