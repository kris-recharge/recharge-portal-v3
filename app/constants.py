"""EVSE metadata constants — ported directly from v2 constants.py.

Hard-coded maps are the source of truth; DB chargers table adds new EVSEs
dynamically without a redeploy.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ── Static maps ───────────────────────────────────────────────────────────────

EVSE_DISPLAY: dict[str, str] = {
    "as_c8rCuPHDd7sV1ynHBVBiq": "ARG - Right",
    "as_cnIGqQ0DoWdFCo7zSrN01":  "ARG - Left",
    "as_oXoa7HXphUu5riXsSW253":  "Delta - Right",
    "as_xTUHfTKoOvKSfYZhhdlhT":  "Delta - Left",
    "as_LYHe6mZTRKiFfziSNJFvJ":  "Glennallen",
}

EVSE_LOCATION: dict[str, str] = {
    "as_c8rCuPHDd7sV1ynHBVBiq": "ARG",
    "as_cnIGqQ0DoWdFCo7zSrN01":  "ARG",
    "as_oXoa7HXphUu5riXsSW253":  "Delta Junction",
    "as_xTUHfTKoOvKSfYZhhdlhT":  "Delta Junction",
    "as_LYHe6mZTRKiFfziSNJFvJ":  "Glennallen",
}

CONNECTOR_TYPE: dict[tuple[str, int], str] = {
    ("as_c8rCuPHDd7sV1ynHBVBiq", 1): "CCS",
    ("as_c8rCuPHDd7sV1ynHBVBiq", 2): "CCS",
    ("as_cnIGqQ0DoWdFCo7zSrN01",  1): "NACS",
    ("as_cnIGqQ0DoWdFCo7zSrN01",  2): "CCS",
    ("as_LYHe6mZTRKiFfziSNJFvJ",  1): "NACS",
    ("as_LYHe6mZTRKiFfziSNJFvJ",  2): "CCS",
    ("as_oXoa7HXphUu5riXsSW253",  1): "NACS",
    ("as_oXoa7HXphUu5riXsSW253",  2): "CCS",
    ("as_xTUHfTKoOvKSfYZhhdlhT",  1): "NACS",
    ("as_xTUHfTKoOvKSfYZhhdlhT",  2): "CCS",
}

PLATFORM_MAP: dict[str, str] = {
    "as_oXoa7HXphUu5riXsSW253":  "MaxiCharger",  # Delta - Right (Autel)
    "as_xTUHfTKoOvKSfYZhhdlhT":  "MaxiCharger",  # Delta - Left  (Autel)
    "as_c8rCuPHDd7sV1ynHBVBiq":  "RTM",           # ARG - Right   (Tritium)
    "as_cnIGqQ0DoWdFCo7zSrN01":   "RTM",           # ARG - Left    (Tritium)
    "as_LYHe6mZTRKiFfziSNJFvJ":  "MaxiCharger",   # Glennallen    (Autel)
}

# Delta Junction connector 1 changed CHAdeMO → NACS on 2026-01-30 (AKST)
_DELTA_STATIONS       = {"as_oXoa7HXphUu5riXsSW253", "as_xTUHfTKoOvKSfYZhhdlhT"}
_DELTA_CONN1_CUTOFF   = date(2026, 1, 30)
_AK_TZ                = ZoneInfo("America/Anchorage")

# ── Runtime overrides ─────────────────────────────────────────────────────────

_OVR_PATH = Path(__file__).with_name("runtime_overrides.json")


def _load_overrides() -> dict:
    try:
        return json.loads(_OVR_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── Public accessors ──────────────────────────────────────────────────────────

def get_evse_display() -> dict[str, str]:
    out = dict(EVSE_DISPLAY)
    out.update(_load_overrides().get("evse_display", {}))
    return out


def get_evse_location() -> dict[str, str]:
    out = dict(EVSE_LOCATION)
    out.update(_load_overrides().get("evse_location", {}))
    return out


def get_connector_type() -> dict[tuple[str, int], str]:
    out = dict(CONNECTOR_TYPE)
    out.update(_load_overrides().get("connector_type", {}))
    return out


def get_platform_map() -> dict[str, str]:
    out = dict(PLATFORM_MAP)
    out.update(_load_overrides().get("platform_map", {}))
    return out


def get_archived_station_ids() -> list[str]:
    return list(_load_overrides().get("archived_station_ids", []))


def get_all_station_ids() -> list[str]:
    return sorted(
        set(get_evse_display()) | set(get_evse_location()) | set(get_platform_map())
    )


def display_name(station_id: str) -> str:
    return get_evse_display().get(station_id, station_id)


def location_label(station_id: str) -> str:
    return get_evse_location().get(station_id, "")


def connector_type_for(station_id: str, connector_id: int, session_start_utc=None) -> str:
    """Return connector type, applying the Delta CHAdeMO→NACS cutover if relevant."""
    ctype = get_connector_type().get((station_id, connector_id), "")

    if station_id in _DELTA_STATIONS and int(connector_id) == 1 and session_start_utc:
        try:
            if isinstance(session_start_utc, str):
                s = session_start_utc.strip().rstrip("Z") + "+00:00" if session_start_utc.endswith("Z") else session_start_utc
                dt = datetime.fromisoformat(s)
            else:
                dt = session_start_utc
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            if dt.astimezone(_AK_TZ).date() < _DELTA_CONN1_CUTOVER:
                return "CHAdeMO"
            return "NACS"
        except Exception:
            pass

    return ctype
