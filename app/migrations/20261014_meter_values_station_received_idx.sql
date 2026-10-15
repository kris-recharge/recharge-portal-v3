-- Latest meter value per charger, by arrival time.
--
-- The Connectivity tab (every 30 s while open) and the offline-alert loop (every
-- 60 s) both ask "when did each charger last send a meter value?". There was no
-- index on (station_id, received_at), so the answer was a scan of the station's
-- whole history — ~180 ms per call, 466k calls, ~24 h of database time so far.
-- With this index each charger is one index probe.
--
-- CONCURRENTLY so the collector keeps inserting while it builds; it cannot run
-- inside a transaction block, so run this file on its own.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_meter_values_parsed_station_received
    ON public.meter_values_parsed (station_id, received_at DESC);
