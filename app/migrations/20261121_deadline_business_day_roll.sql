-- Admin Deadlines: recurring dates that follow a government calendar.
--
-- KPB's quarterly sales tax return is due the 1st of the second month after
-- the quarter, moved to the next business day when the 1st is a weekend or
-- holiday (May 1 2027 is a Saturday → due Mon May 3). Two columns support that:
--   anchor_day            the day of the month the series is meant to fall on
--                         (1 for KPB), so a rolled date (May 3) doesn't become
--                         the new pattern.
--   roll_to_business_day  move a weekend/holiday due date to the next business day.
--
-- Additive only: existing rows keep working (anchor_day NULL → the API falls
-- back to the series' first due date, as before). The tables keep RLS on with
-- no policies and no anon/authenticated grants (20261120_admin_deadlines.sql);
-- new columns inherit that. Safe to run more than once.
--
-- Apply BEFORE deploying the code that reads these columns.

ALTER TABLE public.admin_deadlines
    ADD COLUMN IF NOT EXISTS anchor_day smallint
        CHECK (anchor_day IS NULL OR anchor_day BETWEEN 1 AND 31),
    ADD COLUMN IF NOT EXISTS roll_to_business_day boolean NOT NULL DEFAULT false;
