"""Alert background service.

Runs in a daemon thread started by the FastAPI lifespan.
Polls Supabase every 60 seconds and checks four alert conditions:

  1. Charger Offline – Idle:        no message from any asset for >= 20 min
  2. Charger Offline – Mid-Session: no message during open transaction for >= 5 min
  3. Fault / Error Code:            StatusNotification with errorCode != 'NoError'
  4. Suspicious VID:                same ID tag, energy < 1 kWh, new session within 5 min

Deduplication:
  - Offline alerts are silenced per asset until a new message arrives (BootNotification
    with new connection_id = confirmed reconnect).
  - Fault alerts deduplicate by (asset_id, errorCode) within a 30-second window.
  - Suspicious VID: fires once per (id_tag, transaction_id) pair.
  - Mid-session offline: fires once per (asset_id, transaction_id) pair.

Email is sent via Microsoft 365 SMTP (smtp.office365.com:587).
Browser banner is pushed via SSE (broadcast_alert → /api/alerts/stream).
"""

from __future__ import annotations

import logging
import smtplib
import threading
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from .config import (
    ALERT_EMAIL_FROM,
    ALERT_EMAIL_TO,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USER,
)
from .constants import display_name, get_all_station_ids
from .db import get_conn_sync

# How many poll cycles between fired_alerts cleanup runs (60s × 240 = ~4 hours)
_CLEANUP_EVERY_N = 240
_cleanup_counter = 0

logger = logging.getLogger("rca.alerts")

_AK = ZoneInfo("America/Anchorage")

# ── Thresholds ────────────────────────────────────────────────────────────────
IDLE_OFFLINE_MIN        = 20
MID_SESSION_OFFLINE_MIN = 5
FAULT_DEDUP_SEC         = 30
SUSPICIOUS_VID_MIN      = 5
SUSPICIOUS_VID_KWH      = 1.0
POLL_INTERVAL_SEC       = 60

# ── In-memory dedup state ─────────────────────────────────────────────────────
# offline_state[station_id] = {"alerted": bool, "connection_id": str | None}
_offline_state: dict[str, dict] = {}
# fault_seen[(station_id, error_code)] = last_alert_utc
_fault_seen: dict[tuple[str, str], datetime] = {}
# vid_seen[(id_tag, transaction_id)] — suspicious VID dedup
_vid_seen: set[tuple[str, str]] = set()
# mid_session_seen[(asset_id, transaction_id)] — mid-session offline dedup
_mid_session_seen: set[tuple[str, str]] = set()


# ── SSE broadcast (lazy import to avoid circular import at module load) ────────

def _broadcast(alert_type: str, evse_name: str, message: str, timestamp_ak: str) -> None:
    """Push alert to all connected SSE clients (browser banner)."""
    try:
        from .routers.alerts_sse import broadcast_alert  # noqa: PLC0415
        broadcast_alert({
            "alert_type":   alert_type,
            "evse_name":    evse_name,
            "message":      message,
            "timestamp_ak": timestamp_ak,
        })
    except Exception as exc:
        logger.warning("SSE broadcast failed: %s", exc)


# ── Email ─────────────────────────────────────────────────────────────────────

def _send_email_to(to_addr: str, subject: str, body_html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = ALERT_EMAIL_FROM
    msg["To"]      = to_addr
    msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
        logger.info("Alert email sent to %s: %s", to_addr, subject)
    except Exception as exc:
        logger.error("Failed to send alert email to %s: %s", to_addr, exc)


# ── Unified fire-alert helper ─────────────────────────────────────────────────

def _fire_alert(
    conn,
    alert_type: str,
    asset_id: str,
    evse_name: str,
    message: str,
    subject: str,
    body_html: str,
    timestamp_ak: str,
) -> None:
    """
    1. Find all users subscribed to alert_type who have asset_id in their allowed EVSEs.
    2. Send each an individual email.
    3. Insert one row into fired_alerts (logged once per firing).
    4. Push SSE broadcast to all open browser connections.
    """
    # ── Find subscribed recipients ────────────────────────────────────────────
    recipients: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pu.email
                FROM alert_subscriptions asub
                JOIN portal_users pu ON pu.id = asub.user_id
                WHERE asub.alert_type = %s
                  AND asub.enabled    = true
                  AND pu.active       = true
                  AND (
                      pu.allowed_evse_ids IS NULL
                      OR %s = ANY(pu.allowed_evse_ids)
                  )
                """,
                (alert_type, asset_id),
            )
            recipients = [row[0] for row in cur.fetchall()]
    except Exception as exc:
        logger.error("Failed to query alert subscriptions: %s", exc)

    # ── Send per-user emails ──────────────────────────────────────────────────
    for email in recipients:
        _send_email_to(email, subject, body_html)

    # ── Log to fired_alerts (once per firing, regardless of recipient count) ──
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fired_alerts (alert_type, asset_id, evse_name, message)
                VALUES (%s, %s, %s, %s)
                """,
                (alert_type, asset_id, evse_name, message),
            )
        conn.commit()
    except Exception as exc:
        logger.error("Failed to log fired_alert: %s", exc)

    # ── SSE browser banner ────────────────────────────────────────────────────
    _broadcast(alert_type, evse_name, message, timestamp_ak)


# ── Cleanup old fired_alerts ──────────────────────────────────────────────────

def _cleanup_fired_alerts(conn) -> None:
    """Delete fired_alerts rows older than 15 days."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM fired_alerts WHERE fired_at < NOW() - INTERVAL '15 days'"
            )
        conn.commit()
        logger.debug("fired_alerts cleanup complete")
    except Exception as exc:
        logger.error("fired_alerts cleanup failed: %s", exc)


def _fmt_ak(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_AK).strftime("%Y-%m-%d %H:%M:%S AKST")


def _alert_body(title: str, rows: list[tuple[str, str]]) -> str:
    """Minimal HTML email body."""
    table_rows = "".join(f"<tr><td><b>{k}</b></td><td>{v}</td></tr>" for k, v in rows)
    return f"""
    <html><body>
    <h2 style="color:#c0392b;">⚠ ReCharge Alaska Alert</h2>
    <h3>{title}</h3>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;font-family:sans-serif;">
      {table_rows}
    </table>
    <p style="color:#888;font-size:12px;">
      Sent by ReCharge Alaska Portal v3 — <a href="https://www.rechargealaska.net/dashboard">Dashboard</a>
    </p>
    </body></html>
    """


# ── Alert checks ──────────────────────────────────────────────────────────────

def _check_offline_idle(conn) -> None:
    """Alert when no message from a configured asset for >= 20 min.

    Uses MAX of ocpp_events.received_at and meter_values_parsed.received_at so
    Tritium RTM chargers (ARG) that don't send periodic heartbeats aren't
    falsely flagged as offline while actively delivering power.
    """
    now    = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(minutes=IDLE_OFFLINE_MIN)
    allowed = get_all_station_ids()

    with conn.cursor() as cur:
        # Latest signal from OCPP events per asset
        cur.execute(
            """
            SELECT DISTINCT ON (asset_id)
                asset_id,
                received_at,
                action,
                connection_id
            FROM ocpp_events
            WHERE asset_id = ANY(%s)
            ORDER BY asset_id, received_at DESC
            """,
            (allowed,),
        )
        ocpp_rows = {r[0]: r for r in cur.fetchall()}

        # Latest meter value timestamp per asset (active sessions only)
        cur.execute(
            """
            SELECT station_id, MAX(received_at) AS last_mv
            FROM meter_values_parsed
            WHERE station_id = ANY(%s)
            GROUP BY station_id
            """,
            (allowed,),
        )
        mv_rows = {r[0]: r[1] for r in cur.fetchall()}

    for sid in allowed:
        ocpp_row = ocpp_rows.get(sid)
        if not ocpp_row:
            continue  # charger has never sent anything — skip

        last_ocpp, last_action, conn_id = ocpp_row[1], ocpp_row[2], ocpp_row[3]
        if last_ocpp.tzinfo is None:
            last_ocpp = last_ocpp.replace(tzinfo=timezone.utc)

        last_mv = mv_rows.get(sid)
        if last_mv and last_mv.tzinfo is None:
            last_mv = last_mv.replace(tzinfo=timezone.utc)

        # Use the most recent signal from either source
        last_seen = max(t for t in (last_ocpp, last_mv) if t is not None)

        state = _offline_state.setdefault(sid, {"alerted": False, "connection_id": conn_id})

        # Reconnect detected: BootNotification with a new connection_id
        if last_action == "BootNotification" and conn_id != state["connection_id"]:
            state["alerted"] = False
            state["connection_id"] = conn_id

        if last_seen < cutoff and not state["alerted"]:
            mins_offline = (now - last_seen).total_seconds() / 60.0
            state["alerted"] = True
            name = display_name(sid)
            ts   = _fmt_ak(last_seen)
            msg  = f"No messages for {mins_offline:.0f} min (last seen {ts})"
            _fire_alert(
                conn,
                alert_type = "offline_idle",
                asset_id   = sid,
                evse_name  = name,
                message    = msg,
                subject    = f"⚠ Charger Offline (Idle): {name}",
                body_html  = _alert_body(
                    f"Charger Offline – No messages for {mins_offline:.0f} minutes",
                    [
                        ("Charger",         name),
                        ("Asset ID",        sid),
                        ("Last Seen",       ts),
                        ("Minutes Offline", f"{mins_offline:.0f}"),
                        ("Last Action",     last_action or "—"),
                    ],
                ),
                timestamp_ak = ts,
            )


def _check_offline_mid_session(conn) -> None:
    """Alert when MeterValues stop during an open transaction for >= 5 min."""
    now    = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(minutes=MID_SESSION_OFFLINE_MIN)

    with conn.cursor() as cur:
        # Open transactions: StartTransaction without a matching StopTransaction
        cur.execute(
            """
            SELECT DISTINCT ON (st.asset_id, st.transaction_id)
                st.asset_id,
                st.transaction_id,
                st.received_at AS start_time
            FROM ocpp_events st
            WHERE st.action = 'StartTransaction'
              AND NOT EXISTS (
                  SELECT 1 FROM ocpp_events sp
                  WHERE sp.action = 'StopTransaction'
                    AND sp.asset_id = st.asset_id
                    AND (sp.action_payload->>'transactionId')::text =
                        (st.action_payload->>'transactionId')::text
              )
            ORDER BY st.asset_id, st.transaction_id, st.received_at DESC
            """
        )
        open_txns = cur.fetchall()

    for sid, tx_id, tx_start in open_txns:
        dedup_key = (sid, str(tx_id))
        if dedup_key in _mid_session_seen:
            continue  # already alerted for this transaction

        with conn.cursor() as cur:
            # Last OCPP message during this transaction
            cur.execute(
                """
                SELECT MAX(received_at) FROM ocpp_events
                WHERE asset_id = %s AND received_at >= %s
                """,
                (sid, tx_start),
            )
            last_ocpp = (cur.fetchone() or [None])[0]

            # Last meter value during this transaction
            cur.execute(
                """
                SELECT MAX(received_at) FROM meter_values_parsed
                WHERE station_id = %s AND received_at >= %s
                """,
                (sid, tx_start),
            )
            last_mv = (cur.fetchone() or [None])[0]

        candidates = [t for t in (last_ocpp, last_mv) if t is not None]
        if not candidates:
            continue
        last_msg = max(
            c if c.tzinfo else c.replace(tzinfo=timezone.utc) for c in candidates
        )

        if last_msg < cutoff:
            mins = (now - last_msg).total_seconds() / 60.0
            _mid_session_seen.add(dedup_key)
            name = display_name(sid)
            ts   = _fmt_ak(last_msg)
            msg  = f"No messages for {mins:.0f} min during active transaction"
            _fire_alert(
                conn,
                alert_type = "offline_mid_session",
                asset_id   = sid,
                evse_name  = name,
                message    = msg,
                subject    = f"⚠ Charger Offline Mid-Session: {name}",
                body_html  = _alert_body(
                    "Charger Offline – No messages during active transaction",
                    [
                        ("Charger",        name),
                        ("Asset ID",       sid),
                        ("Transaction ID", str(tx_id)),
                        ("Last Message",   ts),
                        ("Silent for",     f"{mins:.0f} minutes"),
                    ],
                ),
                timestamp_ak = ts,
            )


def _check_faults(conn) -> None:
    """Alert on StatusNotification with errorCode != 'NoError', dedup within 30s."""
    now      = datetime.now(tz=timezone.utc)
    lookback = now - timedelta(minutes=5)  # only check recent events each poll

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                asset_id,
                received_at,
                connector_id,
                action_payload->>'status'           AS status,
                action_payload->>'errorCode'        AS error_code,
                action_payload->>'vendorErrorCode'  AS vendor_error_code
            FROM ocpp_events
            WHERE action = 'StatusNotification'
              AND action_payload->>'errorCode' != 'NoError'
              AND received_at >= %s
            ORDER BY received_at ASC
            """,
            (lookback,),
        )
        rows = cur.fetchall()

    for row in rows:
        sid, recv_at, conn_id, status, error_code, vendor_code = row
        if recv_at.tzinfo is None:
            recv_at = recv_at.replace(tzinfo=timezone.utc)

        key = (sid, error_code or "")
        last = _fault_seen.get(key)
        if last and (recv_at - last).total_seconds() < FAULT_DEDUP_SEC:
            continue

        _fault_seen[key] = recv_at
        name   = display_name(sid)
        ts     = _fmt_ak(recv_at)
        detail = f"{error_code}" + (f" / {vendor_code}" if vendor_code else "")
        msg    = f"{status or 'Fault'} — {detail}"
        _fire_alert(
            conn,
            alert_type = "fault",
            asset_id   = sid,
            evse_name  = name,
            message    = msg,
            subject    = f"⚠ Charger Fault: {name} — {error_code}",
            body_html  = _alert_body(
                "Charger Fault / Error Code Detected",
                [
                    ("Charger",      name),
                    ("Asset ID",     sid),
                    ("Connector",    str(conn_id) if conn_id else "—"),
                    ("Status",       status or "—"),
                    ("Error Code",   error_code or "—"),
                    ("Vendor Code",  vendor_code or "—"),
                    ("Time",         ts),
                ],
            ),
            timestamp_ak = ts,
        )


def _check_suspicious_vid(conn) -> None:
    """Alert on same VID: energy < 1 kWh + new session within 5 min of end."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH recent_stops AS (
                SELECT
                    e.asset_id,
                    e.received_at                                      AS stop_time,
                    (e.action_payload->>'idTag')                       AS id_tag,
                    (e.action_payload->>'transactionId')::text         AS transaction_id,
                    ((e.action_payload->>'meterStop')::float
                      - (e.action_payload->>'meterStart')::float) / 1000.0 AS energy_kwh
                FROM ocpp_events e
                WHERE e.action = 'StopTransaction'
                  AND e.received_at >= NOW() - INTERVAL '30 minutes'
            ),
            next_starts AS (
                SELECT
                    s.id_tag,
                    s.transaction_id   AS stop_tx,
                    s.energy_kwh,
                    s.stop_time,
                    (SELECT e2.received_at FROM ocpp_events e2
                     WHERE e2.action = 'StartTransaction'
                       AND (e2.action_payload->>'idTag') = s.id_tag
                       AND e2.received_at > s.stop_time
                       AND e2.received_at < s.stop_time + INTERVAL '5 minutes'
                     ORDER BY e2.received_at ASC LIMIT 1) AS next_start_time
                FROM recent_stops s
                WHERE s.energy_kwh IS NOT NULL
                  AND s.energy_kwh < %s
            )
            SELECT *
            FROM next_starts
            WHERE next_start_time IS NOT NULL
            """,
            (SUSPICIOUS_VID_KWH,),
        )
        rows = cur.fetchall()

    for row in rows:
        id_tag, stop_tx, energy_kwh, stop_time, next_start_time = row
        key = (id_tag or "", str(stop_tx or ""))
        if key in _vid_seen:
            continue
        _vid_seen.add(key)

        if stop_time and stop_time.tzinfo is None:
            stop_time = stop_time.replace(tzinfo=timezone.utc)

        ts  = _fmt_ak(stop_time) if stop_time else "—"
        msg = f"{energy_kwh:.3f} kWh session — new attempt within {SUSPICIOUS_VID_MIN} min"
        # Suspicious VID isn't tied to one specific asset_id — use the stop asset
        # We query all allowed assets; use a dummy that matches all (logged per VID)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT asset_id FROM ocpp_events WHERE action='StopTransaction' "
                "AND action_payload->>'idTag' = %s ORDER BY received_at DESC LIMIT 1",
                (id_tag,),
            )
            row = cur.fetchone()
        asset = row[0] if row else "unknown"
        _fire_alert(
            conn,
            alert_type = "suspicious_vid",
            asset_id   = asset,
            evse_name  = id_tag or "Unknown VID",
            message    = msg,
            subject    = f"⚠ Suspicious VID Activity: {id_tag}",
            body_html  = _alert_body(
                "Suspicious ID Tag (VID) Session Pattern",
                [
                    ("ID Tag",                   id_tag or "—"),
                    ("Completed Session Energy", f"{energy_kwh:.3f} kWh"),
                    ("Session End",              ts),
                    ("New Session Started",      _fmt_ak(next_start_time) if next_start_time else "—"),
                    ("Pattern",                  f"< {SUSPICIOUS_VID_KWH} kWh followed by new session within {SUSPICIOUS_VID_MIN} min"),
                ],
            ),
            timestamp_ak = ts,
        )


# ── Main poll loop ────────────────────────────────────────────────────────────

def _run_poll_loop() -> None:
    global _cleanup_counter
    logger.info("Alert poll loop started (interval=%ds)", POLL_INTERVAL_SEC)
    while True:
        try:
            conn = get_conn_sync()
            try:
                _check_offline_idle(conn)
                _check_offline_mid_session(conn)
                _check_faults(conn)
                _check_suspicious_vid(conn)
                _cleanup_counter += 1
                if _cleanup_counter >= _CLEANUP_EVERY_N:
                    _cleanup_fired_alerts(conn)
                    _cleanup_counter = 0
            finally:
                conn.close()
        except Exception as exc:
            logger.error("Alert poll error: %s", exc, exc_info=True)
        time.sleep(POLL_INTERVAL_SEC)


def start_alert_thread() -> threading.Thread:
    t = threading.Thread(target=_run_poll_loop, daemon=True, name="alert-poller")
    t.start()
    logger.info("Alert thread started")
    return t
