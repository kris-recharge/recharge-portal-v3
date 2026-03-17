"""Pydantic response models for all API endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


# ── Charging Sessions ─────────────────────────────────────────────────────────

class ChargingSession(BaseModel):
    transaction_id: str
    station_id: str
    evse_name: str
    location: str
    connector_id: int | None
    connector_type: str
    start_dt: str          # formatted AKST e.g. "2026-03-14 10:23"
    end_dt: str | None
    duration_min: float | None
    max_power_kw: float | None
    energy_kwh: float | None
    soc_start: int | None
    soc_end: int | None
    id_tag: str | None
    est_revenue_usd: float | None


class SessionsResponse(BaseModel):
    sessions: list[ChargingSession]
    total: int
    page: int
    page_size: int
    total_energy_kwh: float = 0.0
    total_revenue_usd: float = 0.0
    avg_duration_min: float | None = None


# ── Status History ────────────────────────────────────────────────────────────

class StatusEvent(BaseModel):
    id: int
    station_id: str
    evse_name: str
    connector_id: int | None
    status: str
    error_code: str | None
    vendor_error_code: str | None
    vendor_error_description: str | None = None
    received_at: datetime
    received_at_ak: str


class StatusHistoryResponse(BaseModel):
    events: list[StatusEvent]
    total: int


# ── Connectivity ──────────────────────────────────────────────────────────────

class ConnectivityRecord(BaseModel):
    station_id: str
    evse_name: str
    location: str
    last_seen_utc: datetime | None
    last_seen_ak: str | None
    last_action: str | None
    connection_id: str | None
    minutes_since_last_message: float | None
    is_online: bool


class ConnectivityResponse(BaseModel):
    chargers: list[ConnectivityRecord]
    as_of_utc: datetime


# ── Alerts (SSE) ──────────────────────────────────────────────────────────────

class AlertEvent(BaseModel):
    alert_type: str          # "offline_idle" | "offline_mid_session" | "fault" | "suspicious_vid"
    station_id: str
    evse_name: str
    connector_id: int | None
    message: str
    timestamp_utc: datetime
    timestamp_ak: str
    extra: dict[str, Any] = {}


# ── Session Detail (time-series meter values for chart) ───────────────────────

class MeterValuePoint(BaseModel):
    ts_ak: str                       # "2026-03-14 19:05"
    power_kw: float | None
    power_offered_kw: float | None   # Autel only (power_offered_w)
    current_offered_a: float | None  # Tritium only
    soc: float | None                # 0–100 %
    energy_kwh_delta: float | None   # kWh delivered since session start
    voltage_v: float | None          # HVB voltage


class SessionDetailResponse(BaseModel):
    station_id: str
    evse_name: str
    transaction_id: str
    start_dt: str
    end_dt: str | None
    points: list[MeterValuePoint]


# ── Analytics (Daily Totals + Session-Start Density) ─────────────────────────

class DailyTotal(BaseModel):
    date: str        # "YYYY-MM-DD" (Alaska local date of session start)
    count: int
    energy_kwh: float


class DensityPoint(BaseModel):
    dow: int         # 0 = Sunday … 6 = Saturday
    hour: int        # 0–23 (Alaska local)
    count: int


class AnalyticsResponse(BaseModel):
    daily_totals: list[DailyTotal]
    density: list[DensityPoint]


# ── Export ────────────────────────────────────────────────────────────────────

class ExportRequest(BaseModel):
    start_date: str    # "YYYY-MM-DD" (Alaska local)
    end_date: str      # "YYYY-MM-DD" (Alaska local)
    station_ids: list[str] | None = None
    format: str = "csv"   # "csv" | "xlsx"


# ── Alerts Config & History ───────────────────────────────────────────────────

ALERT_TYPES = ("offline_idle", "offline_mid_session", "fault", "suspicious_vid")

class AlertSubscription(BaseModel):
    alert_type: str   # one of ALERT_TYPES
    enabled: bool


class AlertSubscriptionsResponse(BaseModel):
    email: str
    subscriptions: list[AlertSubscription]


class FiredAlert(BaseModel):
    id: str
    fired_at_ak: str
    alert_type: str
    evse_name: str
    message: str


class AlertHistoryResponse(BaseModel):
    alerts: list[FiredAlert]
