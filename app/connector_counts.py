"""Connector-count odometer — durable lifetime plug-in counter per cord.

Why a stored counter instead of a live query
---------------------------------------------
The count is a *lifetime odometer* for each charging cord, used to drive cord
replacement decisions (Autel/ABB/Alpitronics tie cord life to connection count;
Tritium uses a time-based strategy). It must survive even if raw ``ocpp_events``
are pruned, and it must be able to carry a manual mid-life baseline once a
manufacturer hands over the true historical count. So we persist a rolling sum
in ``connector_counts`` and only ever *add* to it.

What counts as a connection (plug-in)
--------------------------------------
A *transition into* OCPP ``Preparing`` on connector 1 or 2 — i.e. a cable was
inserted. We track ``last_status`` per connector so repeated ``Preparing`` rows
within one plug-in are counted once. Both vendors emit ``Preparing`` reliably
(verified against production data).

Accumulation is incremental: a single-row watermark (``connector_count_state``)
records the last ``ocpp_events.id`` processed, so each tick only scans new rows.
The first run starts at watermark 0 and backfills the full event history.

Called every ~60s from the alerts poll loop (sync psycopg connection).
"""

from __future__ import annotations

import logging
from collections import defaultdict

logger = logging.getLogger("rca.connector_counts")

# Only connectors 1 and 2 are physical cords; connector 0 is the charge point.
_TRACKED_CONNECTORS = (1, 2)

# Cap rows scanned per query so the first (backfill) tick streams in batches
# instead of loading the entire event history into memory at once.
_BATCH_SIZE = 20_000

_DDL = """
CREATE TABLE IF NOT EXISTS connector_counts (
    station_id          text        NOT NULL,
    connector_id        integer     NOT NULL,
    attempts            bigint      NOT NULL DEFAULT 0,
    baseline_attempts   bigint      NOT NULL DEFAULT 0,
    cord_start_attempts bigint      NOT NULL DEFAULT 0,
    last_status         text,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (station_id, connector_id)
);

-- cord_start_attempts marks the odometer reading when the current cord was
-- installed; displayed count = baseline_attempts + (attempts - cord_start_attempts).
ALTER TABLE connector_counts
    ADD COLUMN IF NOT EXISTS cord_start_attempts bigint NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS connector_count_state (
    id            integer     PRIMARY KEY DEFAULT 1,
    last_event_id bigint      NOT NULL DEFAULT 0,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT connector_count_state_singleton CHECK (id = 1)
);

INSERT INTO connector_count_state (id, last_event_id)
VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;

-- Audit log of cord replacements. One row per cord retired; lets us report how
-- long the removed cord lasted (the credibility datapoint) without zeroing the
-- monotonic odometer. The manual reset and the future maintenance-driven reset
-- both write here.
CREATE TABLE IF NOT EXISTS connector_cord_changes (
    id                    bigserial   PRIMARY KEY,
    station_id            text        NOT NULL,
    connector_id          integer     NOT NULL,
    changed_at            timestamptz NOT NULL DEFAULT now(),
    odometer_attempts     bigint      NOT NULL,
    prev_cord_attempts    bigint      NOT NULL,
    changed_by            text,
    source                text        NOT NULL DEFAULT 'manual',
    maintenance_record_id uuid,
    note                  text
);

CREATE INDEX IF NOT EXISTS connector_cord_changes_conn_idx
    ON connector_cord_changes (station_id, connector_id, changed_at DESC);
"""


def ensure_tables(conn) -> None:
    """Create the odometer tables + watermark row if they don't exist (idempotent)."""
    with conn.cursor() as cur:
        cur.execute(_DDL)
    conn.commit()


def accumulate(conn) -> int:
    """Process new StatusNotification events and add plug-ins to the rolling sum.

    Returns the number of new plug-ins counted this run (0 when caught up).
    Safe to call repeatedly — the watermark guarantees each event is counted once.
    """
    new_plugins = 0
    with conn.cursor() as cur:
        cur.execute("SELECT last_event_id FROM connector_count_state WHERE id = 1")
        row = cur.fetchone()
        watermark = row[0] if row else 0

        # last_status per connector carries across runs so a Preparing that
        # spans a batch boundary isn't double-counted.
        cur.execute("SELECT station_id, connector_id, last_status FROM connector_counts")
        last_status: dict[tuple[str, int], str | None] = {
            (r[0], r[1]): r[2] for r in cur.fetchall()
        }
        attempts_delta: dict[tuple[str, int], int] = defaultdict(int)

        while True:
            cur.execute(
                """
                SELECT id, asset_id, connector_id, action_payload->>'status' AS status
                FROM ocpp_events
                WHERE id > %s
                  AND action = 'StatusNotification'
                  AND connector_id = ANY(%s)
                ORDER BY id
                LIMIT %s
                """,
                (watermark, list(_TRACKED_CONNECTORS), _BATCH_SIZE),
            )
            rows = cur.fetchall()
            if not rows:
                break

            for ev_id, asset_id, connector_id, status in rows:
                key = (asset_id, connector_id)
                if status == "Preparing" and last_status.get(key) != "Preparing":
                    attempts_delta[key] += 1
                    new_plugins += 1
                last_status[key] = status
                watermark = ev_id

            if len(rows) < _BATCH_SIZE:
                break

        # Persist deltas + the latest status for every connector we've seen.
        for (asset_id, connector_id), status in last_status.items():
            cur.execute(
                """
                INSERT INTO connector_counts
                    (station_id, connector_id, attempts, last_status, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (station_id, connector_id) DO UPDATE
                    SET attempts    = connector_counts.attempts + EXCLUDED.attempts,
                        last_status = EXCLUDED.last_status,
                        updated_at  = now()
                """,
                (asset_id, connector_id, attempts_delta.get((asset_id, connector_id), 0), status),
            )

        cur.execute(
            "UPDATE connector_count_state SET last_event_id = %s, updated_at = now() WHERE id = 1",
            (watermark,),
        )
    conn.commit()

    if new_plugins:
        logger.info("connector_counts: +%d plug-ins (watermark=%d)", new_plugins, watermark)
    return new_plugins
