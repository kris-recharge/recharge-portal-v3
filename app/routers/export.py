"""GET /api/export — CSV/XLSX download of charging sessions + vendor faults."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from ..auth import CurrentUser, filter_evse_ids
from ..constants import connector_type_for, display_name, get_all_station_ids, location_label
from ..db import acquire

router = APIRouter(prefix="/api/export", tags=["export"])

_AK = ZoneInfo("America/Anchorage")


def _fmt_ak(dt: datetime | None) -> str:
    """Format UTC datetime as mm/dd/yy hh:mm:ss in Alaska time."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_AK).strftime("%m/%d/%y %H:%M:%S")


def _pct(val) -> str:
    """Format SoC value (0–100) as '45%' string; blank if absent."""
    if val is None or val == "":
        return ""
    return f"{float(val):.0f}%"


@router.get("")
async def export_sessions(
    user: CurrentUser,
    start_date: str = Query(..., description="YYYY-MM-DD Alaska local"),
    end_date: str = Query(..., description="YYYY-MM-DD Alaska local"),
    station_id: list[str] | None = Query(None),
    format: str = Query("csv", pattern="^(csv|xlsx)$"),
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
                    -- second SoC reading (used to skip bogus leading 0%)
                    (SELECT mv2.soc FROM meter_values_parsed mv2
                     WHERE mv2.station_id    = m.station_id
                       AND mv2.connector_id  = m.connector_id
                       AND mv2.transaction_id = m.transaction_id
                       AND mv2.soc IS NOT NULL
                     ORDER BY mv2.ts ASC LIMIT 1 OFFSET 1)  AS soc_second,
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
                  AND m.ts <= $3
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
            ORDER BY s.end_utc DESC
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
        "Est. Revenue (USD)",     # M
        "VID",                    # N
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
            round(c_fee + (float(energy_wh or 0) / 1000.0) * p_kwh, 2)
            if (p_kwh or c_fee) else ""
        )

        # SoC start: skip leading bogus 0% — if first=0 and second>1%, use second
        soc_start_raw = r["soc_start"]
        soc_second    = r["soc_second"]
        if (
            soc_start_raw is not None
            and float(soc_start_raw) == 0.0
            and soc_second is not None
            and float(soc_second) > 1.0
        ):
            soc_start_raw = soc_second

        data_rows.append([
            _fmt_ak(start_dt),
            _fmt_ak(end_dt),
            display_name(sid),
            location_label(sid),
            conn_id,
            connector_type_for(sid, conn_id or 0, start_dt),
            max_kw,
            energy_kwh,
            dur_min,
            _pct(soc_start_raw),
            _pct(r["soc_end"]),
            r["auth_tag"] or "",          # L — Authentication (all auth methods)
            est_rev,                      # M — Est. Revenue
            r["vid_tag"] or "",           # N — VID (VID:-prefixed only)
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
        fault_data.append([
            _fmt_ak(fr["received_at"]),
            display_name(fr["asset_id"]),
            location_label(fr["asset_id"]),
            fr["connector_id"] or "",
            fr["error_code"] or "",
            fr["vendor_error_code"] or "",
            fr["vendor_description"] or "",
            fr["status"] or "",
        ])

    filename = f"sessions_{start_date}_to_{end_date}"

    # ── XLSX: two sheets ──────────────────────────────────────────────────────
    if format == "xlsx":
        import openpyxl

        wb = openpyxl.Workbook()

        # Sheet 1 — Sessions
        ws1 = wb.active
        ws1.title = "Sessions"
        ws1.append(session_columns)
        for row in data_rows:
            ws1.append(row)

        # Sheet 2 — Vendor Faults
        ws2 = wb.create_sheet(title="Vendor Faults")
        ws2.append(fault_columns)
        for row in fault_data:
            ws2.append(row)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}.xlsx"'},
        )

    # ── CSV: sessions only (CSV doesn't support multiple sheets) ──────────────
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(session_columns)
    writer.writerows(data_rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
    )
