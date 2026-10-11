-- Maintenance costs, for the Year-End P&L.
--
-- Until now a maintenance record carried labor hours only and each part line
-- had no cost. This adds a labor rate per record and cost/quantity/warranty per
-- line, and two line types for things that aren't parts on the unit: a
-- material (consumables, hardware) and a service (contract labor, remote
-- support). The P&L counts quantity × unit_cost for non-warranty lines plus
-- labor_rate × (onsite + mobilized hours), on the record's date.
--
-- Additive only: existing INSERTs list their columns, and every new column has
-- a default or is nullable, so the running backend is unaffected until rebuilt.

ALTER TABLE public.maintenance_records
    ADD COLUMN IF NOT EXISTS labor_rate numeric(10,2) NOT NULL DEFAULT 0;     -- $/hr

ALTER TABLE public.maintenance_records
    DROP CONSTRAINT IF EXISTS maintenance_records_labor_rate_check,
    ADD CONSTRAINT maintenance_records_labor_rate_check CHECK (labor_rate >= 0);

ALTER TABLE public.maintenance_parts_replaced
    ADD COLUMN IF NOT EXISTS quantity       numeric(10,2) NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS unit_cost      numeric(12,2),                    -- NULL = not costed yet
    ADD COLUMN IF NOT EXISTS warranty       boolean NOT NULL DEFAULT false,   -- covered: shown, not counted
    ADD COLUMN IF NOT EXISTS vendor         text,
    ADD COLUMN IF NOT EXISTS invoice_number text,
    ADD COLUMN IF NOT EXISTS created_at     timestamptz NOT NULL DEFAULT now();

ALTER TABLE public.maintenance_parts_replaced
    DROP CONSTRAINT IF EXISTS maintenance_parts_replaced_action_taken_check,
    ADD CONSTRAINT maintenance_parts_replaced_action_taken_check
        CHECK (action_taken = ANY (ARRAY['replaced', 'repaired', 'cleaned', 'adjusted', 'material', 'service'])),
    DROP CONSTRAINT IF EXISTS maintenance_parts_replaced_cost_check,
    ADD CONSTRAINT maintenance_parts_replaced_cost_check
        CHECK (quantity > 0 AND (unit_cost IS NULL OR unit_cost >= 0));
