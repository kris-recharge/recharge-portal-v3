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


# ── Credential tag registry (v3.5) ────────────────────────────────────────────
#
# _auth_method() used to classify an idTag purely by shape, and its last branch
# was "anything I don't recognise is a credit card". That silently swallowed
# every RFID card into the CC bucket — 5 sessions and $53.57 in August 2026
# alone, money with no Payter/Nayax settlement behind it, which is exactly the
# figure that breaks a month-end tie-out against the processor statements.
#
# The two maps below replace that guess with a fact. Both are keyed on the
# UPPERCASE tag; look-ups normalise, because the same physical card arrives
# lower-cased from some firmware.

# Fixed authorisation tags belonging to a card reader (CCR), one per charger.
# These are read off the terminal itself — LynkWell shows them on the charger's
# Payment Terminal panel as "Authorization ID Tag" — and they change when a
# reader is swapped, so this map needs maintaining. ARG-Right has already been
# through three this year.
#
# NOT a complete list of card readers: the Payter Apollo CCRs at Cooper Landing
# (CL-A..D) run "Cloud" integration and mint a FRESH 20-character tag per tap,
# so no static tag exists for them and none can be listed here. Those sessions
# are identified by their settled Payter transaction instead — see the
# card_matched branch in _auth_method, which outranks every rule below.
TERMINAL_TAGS: dict[str, str] = {
    "FE6DD7B2C3904F": "ARG - Left",
    "161D77C442099C": "ARG - Right",      # retired 2026-08-10
    "AAE40F780E97C9": "ARG - Right",      # in service from 2026-08-20
    "F20AA7178114D0": "Glennallen",
    "253F4A3DBECB6C": "Delta - Right",
    "33A95058916CEF": "Delta - Left",
    "9DFA7CE9F392C8": "Delta - Left",     # retired, last seen 2026-01-07
}

# RFID cards issued to drivers. A tag here bills to a driver account, not to a
# card terminal, so it must never land in the CC bucket.
#
# How to spot a new one: a terminal tag is fixed to a single charger, so any
# non-VID, non-app-shaped tag seen starting transactions on MORE THAN ONE
# charger cannot be a reader. 0424689D4F6180 was found exactly that way — it
# appears on CL-B, CL-C and CL-D.
RFID_TAGS: dict[str, str] = {
    "0424689D4F6180": "Driver RFID card",
}

# Deliberately absent: 04AE179C4F6181. It ran twice at CL-D on 2026-06-28 and is
# registered to no driver in LynkWell; both transactions ended reason
# "DeAuthorized" — the charger was offline, authorised it locally, and LynkWell
# cut it off on reconnect after 23.6 kWh had already been delivered. Almost
# certainly a card from another network. It classifies as "Unknown", which is
# the correct outcome: it is neither a reader nor one of ours.


def _norm_tag(tag: str) -> str:
    return (tag or "").strip().upper()


def is_terminal_tag(tag: str) -> bool:
    """True when the tag is a card reader's fixed authorisation tag."""
    return _norm_tag(tag) in TERMINAL_TAGS


def is_rfid_tag(tag: str) -> bool:
    """True when the tag is an RFID card issued to a driver."""
    return _norm_tag(tag) in RFID_TAGS
