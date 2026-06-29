"""DB-backed EVSE registry — single source of truth from public.chargers.

Builds an in-memory snapshot (refreshed on a short TTL) from chargers + sites +
unit_types and exposes the same dict shapes the ``constants.py`` accessors return.

The sync psycopg path (``get_conn_sync``) is used deliberately so the accessors
in constants.py can stay synchronous — they're called from both async routes and
the alerts background thread, and 15 caller files depend on them being plain
functions. On a DB failure the caller (constants.py) falls back to the hardcoded
maps, so the dashboard never goes blank.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .db import get_conn_sync

logger = logging.getLogger(__name__)

_TTL_SECONDS = 60          # snapshot freshness; new Admin registrations show within this
_lock = threading.Lock()


@dataclass
class Snapshot:
    evse_display:   dict[str, str]              = field(default_factory=dict)  # ext_id -> display name
    evse_location:  dict[str, str]              = field(default_factory=dict)  # ext_id -> site name
    connector_type: dict[tuple[str, int], str]  = field(default_factory=dict)  # (ext_id, conn) -> type
    platform:       dict[str, str]              = field(default_factory=dict)  # ext_id -> unit_type name
    manufacturer:   dict[str, str]              = field(default_factory=dict)  # ext_id -> manufacturer
    active_ids:     list[str]                   = field(default_factory=list)
    archived_ids:   list[str]                   = field(default_factory=list)


_snapshot: Optional[Snapshot] = None
_loaded_at: float = 0.0

_QUERY = """
    SELECT c.external_id, c.display_name, c.connector_types, c.status,
           s.name AS site_name, ut.type_name, ut.manufacturer
    FROM chargers c
    LEFT JOIN sites s       ON s.id = c.site_id
    LEFT JOIN unit_types ut ON ut.id = c.unit_type_id
    WHERE c.external_id IS NOT NULL
"""


def _build() -> Snapshot:
    snap = Snapshot()
    conn = get_conn_sync()
    try:
        with conn.cursor() as cur:
            cur.execute(_QUERY)
            cols = [d.name for d in cur.description]
            rows = cur.fetchall()
    finally:
        conn.close()

    for row in rows:
        r = dict(zip(cols, row))
        ext = r["external_id"]
        if not ext:
            continue
        if r.get("display_name"):
            snap.evse_display[ext] = r["display_name"]
        if r.get("site_name"):
            snap.evse_location[ext] = r["site_name"]
        if r.get("type_name"):
            snap.platform[ext] = r["type_name"]
        if r.get("manufacturer"):
            snap.manufacturer[ext] = r["manufacturer"]

        ct = r.get("connector_types")
        if isinstance(ct, str):
            try:
                ct = json.loads(ct)
            except Exception:
                ct = None
        if isinstance(ct, dict):
            for k, v in ct.items():
                try:
                    snap.connector_type[(ext, int(k))] = v
                except (ValueError, TypeError):
                    continue

        if (r.get("status") or "active") == "active":
            snap.active_ids.append(ext)
        else:
            snap.archived_ids.append(ext)

    return snap


def get_snapshot() -> Optional[Snapshot]:
    """Return the cached registry snapshot, refreshing if stale.

    Returns ``None`` only on a cold-start DB failure, signalling the caller to
    fall back to the hardcoded constants.
    """
    global _snapshot, _loaded_at
    if _snapshot is not None and (time.monotonic() - _loaded_at) < _TTL_SECONDS:
        return _snapshot
    with _lock:
        # Re-check after acquiring the lock — another thread may have refreshed.
        if _snapshot is not None and (time.monotonic() - _loaded_at) < _TTL_SECONDS:
            return _snapshot
        try:
            _snapshot = _build()
            _loaded_at = time.monotonic()
        except Exception as e:
            logger.warning(
                "registry refresh failed (%s); using %s",
                e, "stale snapshot" if _snapshot else "constants fallback",
            )
        return _snapshot


def refresh() -> None:
    """Invalidate the cache so the next access rebuilds. Call after Admin writes."""
    global _loaded_at
    _loaded_at = 0.0
