"""Pydantic response models for all API endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


# ── Charging Sessions ─────────────────────────────────────────────────────────

class ChargingSession(BaseModel):
    status: str = "completed"   # "completed" | "failed_start"
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
    # v3.3: the card terminal's committed amount for this session (None when the
    # session wasn't card-initiated or no match exists) and the entry mode the
    # driver used — "Contactless" | "Contact" | "Magstripe", already normalised
    # across vendors by the card_transactions view, so this is display-ready.
    actual_revenue_usd: float | None = None
    card_entry_mode: str | None = None
    # v3.3: how the driver authenticated — "CC" | "App" | "AutoCharge", or None
    # when no StartTransaction was found (and always None for failed starts,
    # which never authenticated at all). Derived by sessions._auth_method.
    auth_method: str | None = None


class SessionsResponse(BaseModel):
    sessions: list[ChargingSession]
    total: int                       # all rows (completed + failed attempts)
    completed_count: int = 0         # rows with a real transaction
    failed_count: int = 0            # Preparing-but-never-Charging attempts
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


# ── Connector Count (cord-life plug-in odometer) ──────────────────────────────

class ConnectorCount(BaseModel):
    connector_id: int
    connector_type: str
    attempts: int                          # current cord: baseline + (odometer − cord start)
    previous_cord_attempts: int | None = None   # how long the last cord lasted (if ever replaced)
    last_reset_ak: str | None = None            # AK timestamp of last cord change


class ConnectorCountCard(BaseModel):
    station_id: str
    evse_name: str
    manufacturer: str
    location: str
    connectors: list[ConnectorCount]   # connector 1 (left) then 2 (right)


class ConnectorCountResponse(BaseModel):
    chargers: list[ConnectorCountCard]
    as_of_utc: datetime


class CordResetRequest(BaseModel):
    note: str


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
    # Year-to-date peak: the single AK-local calendar day (since 1-Jan of the
    # current AK year) with the most energy dispensed across the allowed EVSEs.
    # NOT affected by the date-range filter — only by the EVSE filter. Both
    # fields are None until the first session of the year lands.
    max_daily_energy_kwh: float | None = None
    max_daily_energy_date: str | None = None   # "YYYY-MM-DD" (Alaska local)


# ── Export ────────────────────────────────────────────────────────────────────

class ExportRequest(BaseModel):
    start_date: str    # "YYYY-MM-DD" (Alaska local)
    end_date: str      # "YYYY-MM-DD" (Alaska local)
    station_ids: list[str] | None = None
    format: str = "csv"   # "csv" | "xlsx"


# ── Alerts Config & History ───────────────────────────────────────────────────

ALERT_TYPES = ("offline_idle", "offline_mid_session", "fault", "suspicious_vid",
               "pm_due_14d", "pm_overdue")

class AlertSubscription(BaseModel):
    alert_type: str   # one of ALERT_TYPES
    enabled: bool                  # master switch: subscribed at all?
    email_enabled: bool = True     # deliver by email
    push_enabled: bool = False     # deliver by Web Push


class PushDevice(BaseModel):
    id: str
    user_agent: str
    label: str = ""          # browser-supplied ("iPad", "iPhone", "Mac", …)
    created_at_ak: str
    last_seen_at_ak: str
    # sha256(endpoint)[:16]. The browser hashes its own endpoint the same way to
    # find itself in the list. User agents cannot do this: an iPad and a Mac send
    # the same UA string, so UA matching flagged the wrong row.
    endpoint_hash: str = ""
    is_current: bool = False


class AlertSubscriptionsResponse(BaseModel):
    email: str
    subscriptions: list[AlertSubscription]
    # Server-side push readiness (VAPID keys present). When false the frontend
    # hides the push controls entirely rather than offering a toggle that
    # silently does nothing.
    push_supported: bool = False
    vapid_public_key: str = ""
    push_devices: list[PushDevice] = []
    banner_all_alert_types: bool = True


class PushSubscribeRequest(BaseModel):
    endpoint: str
    p256dh: str
    auth: str
    user_agent: str = ""
    device_label: str = ""


class BannerScopeRequest(BaseModel):
    banner_all_alert_types: bool


class FiredAlert(BaseModel):
    id: str
    fired_at_ak: str
    alert_type: str
    evse_name: str
    message: str


class AlertHistoryResponse(BaseModel):
    alerts: list[FiredAlert]
