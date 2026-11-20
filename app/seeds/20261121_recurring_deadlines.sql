-- One-time seed: ReCharge's recurring obligations for Admin → Deadlines.
-- Run once in the Supabase SQL Editor AFTER
-- app/migrations/20261121_deadline_business_day_roll.sql. Safe to re-run:
-- each item is skipped if a live one with the same title already exists.
--
-- Each is a recurring series: marking one Done adds the next one, forever.
--
--  1. KPB sales tax, quarterly. Due the 1st of the second month after the
--     quarter, 5:00 pm, moved to the next business day on a weekend/holiday
--     (kpb.us sales-tax resources, 2027 table). First: Q4 2026 → Mon Feb 1 2027,
--     then May 3, Aug 2, Nov 1, Feb 1 2028, …
--  2. Alaska LLC biennial report. Kris filed in January 2026, so the next is due
--     Jan 2 2028. Opens about 3 months early (first alert 90 days out); late
--     fees after Feb 1.
--  3. Business insurance renewal, every August. Aug 1 is a placeholder until
--     it's changed to the policy's renewal date.
--  4. Copper Valley Subway land lease, $1,200 a year, paid by mailed check
--     (7 business days). Jan 1 2027 is a placeholder until it's changed to the
--     lease's payment date.
--  5. SAM.gov entity registration renewal, yearly (expires 365 days after it's
--     activated). Aug 1 is a placeholder: the real date is the expiration date
--     in SAM.gov's Entity Workspace.

INSERT INTO public.admin_deadlines
    (title, category, vendor, amount, due_date, notes, link_url, external_ref,
     recurrence, alert_offsets, lead_business_days, anchor_day, roll_to_business_day,
     source, created_by)
SELECT v.title, v.category, v.vendor, v.amount::numeric, v.due_date::date, v.notes, v.link_url,
       v.external_ref, v.recurrence, v.alert_offsets::int[], v.lead, v.anchor_day, v.roll, 'manual',
       'kris.hall@rechargealaska.net'
FROM (VALUES
    ('KPB sales tax return (quarterly)', 'tax', 'Kenai Peninsula Borough', NULL, '2027-02-01',
     'Due by 5:00 pm. Filed and paid online (bank transfer). Covers the calendar quarter '
     || 'that ended about a month earlier. Cooper Landing; add Moose Pass once it''s online.',
     'https://salestax.kpb.us/', 'Q4-2026',
     'quarterly', '{14,7,3,1,0}', 0, 1, true),
    ('Alaska biennial report (LLC)', 'state_filing', 'State of Alaska, Division of Corporations',
     NULL, '2028-01-02',
     'Last filed January 2026. Filing opens about 3 months before; late fees after Feb 1.',
     NULL, NULL,
     'biennial', '{90,30,14,7,1,0}', 0, 2, false),
    ('Business insurance renewal', 'insurance', NULL, NULL, '2027-08-01',
     'Placeholder date: change it to the policy''s renewal date. Start quotes a month or two ahead.',
     NULL, NULL,
     'yearly', '{60,30,14,7,1,0}', 0, 1, false),
    ('Copper Valley Subway land lease', 'payable', 'Copper Valley Subway', '1200', '2027-01-01',
     'Annual land lease. Placeholder date: change it to the lease''s payment date. '
     || 'Paid by Wells Fargo Bill Pay check.',
     NULL, NULL,
     'yearly', '{30,14,7,3,1,0}', 7, 1, false),
    ('SAM.gov registration renewal', 'grant', 'SAM.gov (GSA)', NULL, '2027-08-01',
     'Placeholder date: change it to the expiration date shown in SAM.gov''s Entity Workspace. '
     || 'Renewing is free; ignore anyone offering to renew it for a fee. Allow a few weeks.',
     'https://sam.gov', NULL,
     'yearly', '{60,30,14,7,1,0}', 0, 1, false)
) AS v(title, category, vendor, amount, due_date, notes, link_url, external_ref,
       recurrence, alert_offsets, lead, anchor_day, roll)
WHERE NOT EXISTS (
    SELECT 1 FROM public.admin_deadlines d
    WHERE d.title = v.title AND d.status <> 'cancelled'
);
