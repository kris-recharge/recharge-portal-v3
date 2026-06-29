"""EVSE metadata accessors.

The Supabase ``chargers`` table (via ``registry``) is the source of truth for the
EVSE roster, display names, locations, connector types, and manufacturer. The
hard-coded maps below are a cold-start / DB-unreachable fallback only, so the
dashboard never goes blank.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import registry

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

# Product-line (platform) → hardware manufacturer. Drives the cord-replacement
# strategy shown on the Connector Count tab (Tritium = time-based, Autel/ABB/
# Alpitronics = connection-count-based). Add new platforms here as the fleet grows.
MANUFACTURER_MAP: dict[str, str] = {
    "MaxiCharger": "Autel",
    "RTM":         "Tritium",
    "HYC400":      "Alpitronic",   # HYC400 DC fast (connection-count-based cords)
}

# Delta Junction connector 1 changed CHAdeMO → NACS on 2026-01-30 (AKST)
_DELTA_STATIONS       = {"as_oXoa7HXphUu5riXsSW253", "as_xTUHfTKoOvKSfYZhhdlhT"}
_DELTA_CONN1_CUTOFF   = date(2026, 1, 30)
_AK_TZ                = ZoneInfo("America/Anchorage")

# ── Public accessors ──────────────────────────────────────────────────────────
#
# Source of truth is the Supabase ``chargers`` table via ``registry``. The
# hardcoded maps above are a cold-start / DB-unreachable fallback only, so the
# dashboard never goes blank.

def get_evse_display() -> dict[str, str]:
    snap = registry.get_snapshot()
    return dict(snap.evse_display) if snap else dict(EVSE_DISPLAY)


def get_evse_location() -> dict[str, str]:
    snap = registry.get_snapshot()
    return dict(snap.evse_location) if snap else dict(EVSE_LOCATION)


def get_connector_type() -> dict[tuple[str, int], str]:
    snap = registry.get_snapshot()
    return dict(snap.connector_type) if snap else dict(CONNECTOR_TYPE)


def get_platform_map() -> dict[str, str]:
    snap = registry.get_snapshot()
    return dict(snap.platform) if snap else dict(PLATFORM_MAP)


def get_manufacturer_map() -> dict[str, str]:
    return dict(MANUFACTURER_MAP)


def manufacturer_for(station_id: str) -> str:
    """Hardware manufacturer for a station (drives the Connector Count cord strategy).

    Sourced from the station's unit type in the ``chargers`` table; the hardcoded
    platform→manufacturer map is the fallback when the DB is unreachable.
    """
    snap = registry.get_snapshot()
    if snap and station_id in snap.manufacturer:
        return snap.manufacturer[station_id]
    platform = get_platform_map().get(station_id, "")
    return get_manufacturer_map().get(platform, "")


def get_archived_station_ids() -> list[str]:
    snap = registry.get_snapshot()
    return list(snap.archived_ids) if snap else []


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
            if dt.astimezone(_AK_TZ).date() < _DELTA_CONN1_CUTOFF:
                return "CHAdeMO"
            return "NACS"
        except Exception:
            pass

    return ctype
