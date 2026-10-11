-- Close three holes in Supabase's public Data API (the anon key ships to every
-- browser, so anything granted to anon/authenticated is effectively public).
--
-- Nothing in the portal uses these paths: FastAPI and the Payter/Nayax
-- collectors all connect via DATABASE_URL as `postgres`, which owns every
-- object below and bypasses RLS. The browser uses Supabase only for OTP login.
-- v2 and Edge Functions were checked too — no callers.

-- ── 1. card_transactions leaked card data to anon ─────────────────────────────
-- It was a security-definer view (Postgres's default), so it ran as its owner
-- and ignored RLS on payter_*/nayax_* — and Supabase's default privileges had
-- handed anon/authenticated ALL on it, TRUNCATE included. A rolled-back anon
-- read returned all 797 rows. Invoker mode makes the underlying tables' RLS
-- (enabled, no policies → deny) apply even if a grant ever creeps back.
ALTER VIEW public.card_transactions SET (security_invoker = true);
REVOKE ALL ON public.card_transactions FROM anon, authenticated;

-- ── 2. Anonymous writes to sites/chargers ─────────────────────────────────────
-- Security-definer RPCs from v1, callable at /rest/v1/rpc/* with the anon key.
-- EXECUTE goes to PUBLIC by default, which anon inherits, so revoke that too.
REVOKE EXECUTE ON FUNCTION public.upsert_chargers(jsonb)      FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.upsert_sites_by_name(jsonb) FROM PUBLIC, anon, authenticated;

-- ── 3. Any logged-in tenant could read every site and charger ─────────────────
-- USING (true) for authenticated bypassed allowed_evse_ids entirely (AEA could
-- list Alyeska Tire's units). With RLS still on, dropping them restores deny.
DROP POLICY IF EXISTS portal_read_sites    ON public.sites;
DROP POLICY IF EXISTS portal_read_chargers ON public.chargers;

-- user_site_access is deliberately left alone: it holds 4 v1 org_admin rows
-- and its policy only exposes a user's own rows.
