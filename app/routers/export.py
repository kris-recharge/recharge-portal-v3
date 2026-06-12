"""GET /api/export — XLSX download of charging sessions + vendor faults.

v3.2: Timestamps are now written as native Excel datetime cells (not strings) so
Excel recognises them without manual reformatting; Vendor Error Code is written as
a real number (no "Number stored as Text" warning); rows are ordered by session
START time, newest on top; and the CSV format option has been removed (xlsx only).
"""

from __future__ import annotations

import math
import io
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from ..auth import CurrentUser, filter_evse_ids
from ..config import DEV_BYPASS_AUTH
from ..constants import connector_type_for, display_name, get_all_station_ids, location_label
from ..db import acquire
from .utility import ADMIN_EMAIL, UTILITY_EFFICIENCY_SQL  # v3.2: shared metered-vs-dispensed query

router = APIRouter(prefix="/api/export", tags=["export"])

_AK = ZoneInfo("America/Anchorage")

# v3.2: Excel display format for the datetime columns — "m/dd/yy h:mm:ss".
_XLSX_DT_FORMAT = "m/dd/yy h:mm:ss"


def _ak_dt(dt: datetime | None) -> datetime | None:
    """Convert a UTC datetime to a naive Alaska-local datetime for Excel.

    v3.2: returns a real datetime (Excel stores it as a date serial) instead of a
    preformatted string, so Excel recognises the cell as a date/time natively.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Drop tzinfo — Excel has no concept of timezone; values are AK-local wall time.
    return dt.astimezone(_AK).replace(tzinfo=None)


def _pct(val) -> str:
    """Format SoC value (0–100) as '45%' string; blank if absent."""
    if val is None or val == "":
        return ""
    return f"{float(val):.0f}%"


_CC_TAGS = {"FE6DD7B2C3904F", "161D77C442099C"}


def _auth_method(auth_tag: str) -> str:
    """Derive Authentication Method from the raw auth tag.

    VID:*  → AutoCharge (vehicle-initiated via Autocharge protocol)
    known CC tag IDs → CC
    anything else    → App
    """
    if not auth_tag:
        return "App"
    if auth_tag.startswith("VID:"):
        return "AutoCharge"
    if auth_tag in _CC_TAGS:
        return "CC"
    return "App"


@router.get("")
async def export_sessions(
    user: CurrentUser,
    start_date: str = Query(..., description="YYYY-MM-DD Alaska local"),
    end_date: str = Query(..., description="YYYY-MM-DD Alaska local"),
    station_id: list[str] | None = Query(None),
    format: str = Query("xlsx", pattern="^(xlsx)$"),  # v3.2: CSV removed, xlsx only
):
    all_ids = get_all_station_ids()
    allowed = filter_evse_ids(all_ids, user.allowed_evse_ids)
    if station_id:
        allowed = [s for s in station_id if s in allowed]

    start_utc = (
        datetime.fromisoformat(f"{start_date}T00:00:00")
        .replace(tzinfo=_AK)
        .astimezone(timezone.utc)
    )
    end_utc = (
        datetime.fromisoformat(f"{end_date}T23:59:59")
        .replace(tzinfo=_AK)
        .astimezone(timezone.utc)
    )

    async with acquire() as conn:
        # ── Sessions ──────────────────────────────────────────────────────────
        rows = await conn.fetch(
            """
            WITH sessions AS (
                SELECT
                    m.station_id,
                    m.connector_id,
                    m.transaction_id,
                    MIN(m.ts)                             AS start_utc,
                    MAX(m.ts)                             AS end_utc,
                    MAX(m.power_w)                        AS max_power_w,
                    MAX(m.energy_wh) - MIN(m.energy_wh)  AS energy_wh_delta,
                    -- first SoC reading
                    (SELECT mv2.soc FROM meter_values_parsed mv2
                     WHERE mv2.station_id    = m.station_id
                       AND mv2.connector_id  = m.connector_id
                       AND mv2.transaction_id = m.transaction_id
                       AND mv2.soc IS NOT NULL
                     ORDER BY mv2.ts ASC LIMIT 1)           AS soc_start,
                    -- first non-zero SoC reading (used to skip bogus leading 0%)
                    (SELECT mv2.soc FROM meter_values_parsed mv2
                     WHERE mv2.station_id    = m.station_id
                       AND mv2.connector_id  = m.connector_id
                       AND mv2.transaction_id = m.transaction_id
                       AND mv2.soc IS NOT NULL
                       AND mv2.soc > 0
                     ORDER BY mv2.ts ASC LIMIT 1)           AS soc_first_nonzero,
                    -- last SoC reading
                    (SELECT mv2.soc FROM meter_values_parsed mv2
                     WHERE mv2.station_id    = m.station_id
                       AND mv2.connector_id  = m.connector_id
                       AND mv2.transaction_id = m.transaction_id
                       AND mv2.soc IS NOT NULL
                     ORDER BY mv2.ts DESC LIMIT 1)          AS soc_end
                FROM meter_values_parsed m
                WHERE m.station_id     = ANY($1::text[])
                  AND m.transaction_id IS NOT NULL
                  AND m.ts >= $2
                  -- v3.2: filter by session START time. Widen the reading window
                  -- 1 day past end so sessions that START in range are aggregated
                  -- in full (not truncated); the outer WHERE drops later starts.
                  AND m.ts <= $3::timestamptz + INTERVAL '1 day'
                GROUP BY m.station_id, m.connector_id, m.transaction_id
            )
            SELECT
                s.*,
                -- Authentication: raw id_tag from authorize_methods (all auth methods)
                (SELECT a.id_tag FROM authorize_methods a
                 WHERE a.asset_id = s.station_id
                   AND a.start_received_at BETWEEN s.start_utc - INTERVAL '60 minutes'
                                               AND s.start_utc + INTERVAL '5 minutes'
                 ORDER BY ABS(EXTRACT(EPOCH FROM (a.start_received_at - s.start_utc))) ASC
                 LIMIT 1) AS auth_tag,
                -- VID: only VID:-prefixed tags from ocpp_events Authorize (499 rows, matches v2)
                (SELECT oe.action_payload->>'idTag'
                 FROM ocpp_events oe
                 WHERE oe.asset_id = s.station_id
                   AND oe.action   = 'Authorize'
                   AND oe.action_payload->>'idTag' LIKE 'VID:%'
                   AND oe.received_at BETWEEN s.start_utc - INTERVAL '60 minutes'
                                          AND s.start_utc + INTERVAL '5 minutes'
                 ORDER BY ABS(EXTRACT(EPOCH FROM (oe.received_at - s.start_utc))) ASC
                 LIMIT 1) AS vid_tag,
                ep.price_per_kwh,
                ep.connection_fee
            FROM sessions s
            LEFT JOIN LATERAL (
                SELECT price_per_kwh, connection_fee
                FROM evse_pricing
                WHERE station_id = s.station_id
                  AND effective_start <= s.start_utc
                  AND (effective_end IS NULL OR effective_end > s.start_utc)
                ORDER BY effective_start DESC LIMIT 1
            ) ep ON true
            WHERE s.start_utc <= $3   -- v3.2: keep only sessions that START in range
            ORDER BY s.start_utc DESC  -- v3.2: newest start on top
            """,
            allowed, start_utc, end_utc,
        )

        # ── Vendor Faults ─────────────────────────────────────────────────────
        fault_rows = await conn.fetch(
            """
            SELECT
                oe.received_at,
                oe.asset_id,
                oe.connector_id,
                oe.action_payload->>'errorCode'       AS error_code,
                oe.action_payload->>'vendorErrorCode' AS vendor_error_code,
                oe.action_payload->>'status'          AS status,
                tec.description                       AS vendor_description
            FROM ocpp_events oe
            LEFT JOIN tritium_error_codes tec
                ON tec.code = (
                    CASE WHEN oe.action_payload->>'vendorErrorCode' ~ '^[0-9]+$'
                         THEN (oe.action_payload->>'vendorErrorCode')::integer
                    END
                )
            WHERE oe.asset_id = ANY($1::text[])
              AND oe.action   = 'StatusNotification'
              AND oe.action_payload->>'errorCode' != 'NoError'
              AND oe.received_at >= $2
              AND oe.received_at <= $3
            ORDER BY oe.received_at DESC
            """,
            allowed, start_utc, end_utc,
        )

        # ── Utility Reads (v3.2) — daily metered vs dispensed per meter ────────
        # Charger-aware: a meter narrows to one unit (charger_id) or covers a
        # whole site. Newest day on top, scoped to the user's allowed EVSEs.
        # Unrestricted users exporting without an EVSE filter also get
        # metered-only meters (no site mapping, e.g. CEA 2630156). Mirror the
        # /efficiency endpoint: admin email and DEV_BYPASS count as unrestricted
        # even when the user has an explicit allowed_evse_ids list.
        is_admin = user.email == ADMIN_EMAIL or DEV_BYPASS_AUTH
        unrestricted_export = (
            (is_admin
             or user.allowed_evse_ids is None
             or "__ALL__" in (user.allowed_evse_ids or []))
            and not station_id
        )
        utility_rows = await conn.fetch(
            UTILITY_EFFICIENCY_SQL.replace(
                "ORDER BY m.site_name, m.account_number, u.day",
                "ORDER BY m.site_name, m.account_number, u.day DESC",
            ),
            unrestricted_export,  # $1
            allowed,              # $2 allowed station ids
            start_utc,            # $3 (timestamptz cast to ::date)
            end_utc,              # $4
        )

    # ── Build sessions rows ───────────────────────────────────────────────────
    session_columns = [
        "Start Date/Time (AK)",   # A
        "End Date/Time (AK)",     # B
        "EVSE",                   # C
        "Location",               # D
        "Connector #",            # E
        "Connector Type",         # F
        "Max Power (kW)",         # G
        "Energy Delivered (kWh)", # H
        "Duration (min)",         # I
        "SoC Start (%)",          # J
        "SoC End (%)",            # K
        "Authentication",         # L
        "Authentication Method",  # M
        "Est. Revenue (USD)",     # N
        "VID",                    # O
    ]

    data_rows: list[list] = []
    for r in rows:
        sid        = r["station_id"]
        conn_id    = r["connector_id"]
        start_dt   = r["start_utc"]
        end_dt     = r["end_utc"]
        dur_min    = (
            round((end_dt - start_dt).total_seconds() / 60.0, 2)
            if start_dt and end_dt else ""
        )
        energy_wh  = r["energy_wh_delta"]
        energy_kwh = round(float(energy_wh) / 1000.0, 3) if energy_wh else ""
        max_kw     = round(float(r["max_power_w"]) / 1000.0, 2) if r["max_power_w"] else ""
        p_kwh      = float(r["price_per_kwh"] or 0)
        c_fee      = float(r["connection_fee"] or 0)
        est_rev    = (
            math.floor((c_fee + (float(energy_wh or 0) / 1000.0) * p_kwh) * 100) / 100
            if (p_kwh or c_fee) else ""
        )

        # SoC — mirror _resolve_soc() from sessions.py:
        # 1. Scale normalisation: chargers that report 0-1 fraction instead of 0-100
        soc_start_raw     = r["soc_start"]
        soc_first_nonzero = r["soc_first_nonzero"]
        soc_end_raw       = r["soc_end"]
        ref = soc_end_raw if soc_end_raw is not None else soc_start_raw
        scale = 100.0 if (ref is not None and float(ref) <= 1.0) else 1.0

        soc_end_val = round(float(soc_end_raw) * scale, 1) if soc_end_raw is not None else None

        # 2. Bogus-zero filter: skip leading 0% readings if first non-zero > 1.5%
        soc_start_val: float | None = None
        if soc_start_raw is not None:
            soc_start_val = float(soc_start_raw) * scale
            if soc_start_val == 0.0 and soc_first_nonzero is not None:
                nonzero_val = float(soc_first_nonzero) * scale
                if nonzero_val > 1.5:
                    soc_start_val = nonzero_val
            soc_start_val = round(soc_start_val, 1)

        data_rows.append([
            _ak_dt(start_dt),   # A — native datetime cell (v3.2)
            _ak_dt(end_dt),     # B — native datetime cell (v3.2)
            display_name(sid),
            location_label(sid),
            conn_id,
            connector_type_for(sid, conn_id or 0, start_dt),
            max_kw,
            energy_kwh,
            dur_min,
            _pct(soc_start_val),
            _pct(soc_end_val),
            r["auth_tag"] or "",                        # L — Authentication (raw tag)
            _auth_method(r["auth_tag"] or ""),          # M — Authentication Method
            est_rev,                                    # N — Est. Revenue
            r["vid_tag"] or "",                         # O — VID (VID:-prefixed only)
        ])

    # ── Build faults rows ─────────────────────────────────────────────────────
    fault_columns = [
        "Timestamp (AK)",
        "EVSE",
        "Location",
        "Connector #",
        "Error Code",
        "Vendor Error Code",
        "Description",
        "Status",
    ]
    fault_data: list[list] = []
    for fr in fault_rows:
        # v3.2: Vendor Error Code (col F) written as a real number when numeric,
        # so Excel stops flagging "Number stored as Text".
        vec_raw = fr["vendor_error_code"]
        vec: int | str = int(vec_raw) if (vec_raw and str(vec_raw).isdigit()) else (vec_raw or "")
        fault_data.append([
            _ak_dt(fr["received_at"]),   # A — native datetime cell (v3.2)
            display_name(fr["asset_id"]),
            location_label(fr["asset_id"]),
            fr["connector_id"] or "",
            fr["error_code"] or "",
            vec,                          # F — numeric Vendor Error Code (v3.2)
            fr["vendor_description"] or "",
            fr["status"] or "",
        ])

    # ── Build Utility Reads rows (v3.2) ───────────────────────────────────────
    utility_columns = [
        "Date",                 # A
        "Site",                 # B
        "Unit",                 # C (blank = whole-site meter)
        "Utility",              # D
        "Account #",            # E
        "Metered kWh",          # F
        "Dispensed kWh",        # G
        "Efficiency (%)",       # H
    ]
    utility_data: list[list] = []
    for ur in utility_rows:
        metered   = float(ur["metered_kwh"]) if ur["metered_kwh"] is not None else 0.0
        # Metered-only meters (no site mapping) have no dispensed side: blank cells
        dispensed = float(ur["dispensed_kwh"]) if ur["dispensed_kwh"] is not None else None
        eff = (
            round(dispensed / metered * 100.0, 1)
            if dispensed is not None and metered > 0 else ""
        )
        utility_data.append([
            ur["day"],                                     # A — native date cell
            ur["site_name"] or ur["display_name"] or "",   # site-less: display name
            ur["unit_name"] or "",
            ur["utility"],
            ur["account_number"],
            round(metered, 3),
            round(dispensed, 3) if dispensed is not None else "",
            eff,
        ])

    filename = f"sessions_{start_date}_to_{end_date}"

    # ── XLSX: two sheets (v3.2: CSV option removed) ───────────────────────────
    import openpyxl

    wb = openpyxl.Workbook()

    # Sheet 1 — Sessions
    ws1 = wb.active
    ws1.title = "Sessions"
    ws1.append(session_columns)
    for row in data_rows:
        ws1.append(row)
    # v3.2: format the two datetime columns (A=Start, B=End) as real dates.
    for col in ("A", "B"):
        for cell in ws1[col][1:]:   # skip header row
            cell.number_format = _XLSX_DT_FORMAT

    # Sheet 2 — Vendor Faults
    ws2 = wb.create_sheet(title="Vendor Faults")
    ws2.append(fault_columns)
    for row in fault_data:
        ws2.append(row)
    # v3.2: format the timestamp column (A) as a real date.
    for cell in ws2["A"][1:]:
        cell.number_format = _XLSX_DT_FORMAT

    # Sheet 3 — Utility Reads (v3.2)
    ws3 = wb.create_sheet(title="Utility Reads")
    ws3.append(utility_columns)
    for row in utility_data:
        ws3.append(row)
    # Date column (A) as a real date.
    for cell in ws3["A"][1:]:
        cell.number_format = "m/dd/yy"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}.xlsx"'},
    )
