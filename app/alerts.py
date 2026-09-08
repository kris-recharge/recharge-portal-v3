"""Alert background service.

Runs in a daemon thread started by the FastAPI lifespan.
Polls Supabase every 60 seconds and checks four alert conditions:

  1. Charger Offline – Idle:        no message from any asset for >= 20 min
  2. Charger Offline – Mid-Session: no message during open transaction for >= 5 min
  3. Fault / Error Code:            StatusNotification with errorCode != 'NoError'
  4. Suspicious VID:                same ID tag, energy < 1 kWh, new session within 5 min
  5. PM Due in 14 Days (pm_due_14d): next PM due date is 13–15 days out (fires once)
  6. PM Overdue (pm_overdue):        PM is due today or overdue; fires on due date then
                                     weekly until a new PM record is logged.

Deduplication:
  - Offline alerts are silenced per asset until a new message arrives (BootNotification
    with new connection_id = confirmed reconnect).
  - Fault alerts deduplicate by (asset_id, errorCode) within a 30-second window.
  - Suspicious VID: fires once per (id_tag, transaction_id) pair.
  - Mid-session offline: fires once per (asset_id, transaction_id) pair.
  - PM due / overdue: deduplicates via fired_alerts table (20-day window for 14d notice;
    7-day window for overdue weekly reminders).

Email is sent via Microsoft 365 SMTP (smtp.office365.com:587).
Browser banner is pushed via SSE (broadcast_alert → /api/alerts/stream).
"""

from __future__ import annotations

import logging
import smtplib
import threading
import time
from datetime import date, datetime, timedelta, timezone
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
from .connector_counts import accumulate as accumulate_connector_counts
from .connector_counts import ensure_tables as ensure_connector_count_tables
from .constants import display_name, get_all_station_ids
from .db import get_conn_sync
from .push import build_payload, push_available, send_to_subscriptions

# How many poll cycles between fired_alerts cleanup runs (60s × 240 = ~4 hours)
_CLEANUP_EVERY_N = 240
_cleanup_counter = 0

# How many poll cycles between PM due-date checks (60s × 60 = ~1 hour)
# Initialized to _PM_CHECK_EVERY_N so the first check runs immediately on startup.
_PM_CHECK_EVERY_N = 60
_pm_check_counter = _PM_CHECK_EVERY_N

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

def _broadcast(
    alert_type: str,
    asset_id: str,
    evse_name: str,
    message: str,
    timestamp_ak: str,
    subscriber_ids: set[str],
) -> None:
    """Push alert to the SSE clients entitled to see it (browser banner).

    asset_id and subscriber_ids are what let the SSE router apply EVSE and
    alert-type scope. Before v3.4 this fanned out to every connected client
    regardless of tenant, which put other sites' charger names in front of
    users scoped to a single unit.
    """
    try:
        from .routers.alerts_sse import broadcast_alert  # noqa: PLC0415
        broadcast_alert(
            {
                "alert_type":   alert_type,
                "asset_id":     asset_id,
                "evse_name":    evse_name,
                "message":      message,
                "timestamp_ak": timestamp_ak,
            },
            subscriber_ids=subscriber_ids,
        )
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


# ── Web Push ──────────────────────────────────────────────────────────────────

def _send_push(
    conn,
    user_ids: list[str],
    alert_type: str,
    subject: str,
    evse_name: str,
    message: str,
    timestamp_ak: str,
) -> None:
    """Deliver one alert to every registered device of the given users.

    A user may have several rows here (phone + iPad + laptop); each is its own
    push subscription and gets its own notification. Dead endpoints are pruned
    inside send_to_subscriptions.
    """
    if not push_available():
        logger.debug("Push subscribers exist but push is not configured — skipping.")
        return

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id::text, endpoint, p256dh, auth
                FROM push_subscriptions
                WHERE user_id = ANY(%s::uuid[])
                """,
                (user_ids,),
            )
            subs = [
                {"user_id": r[0], "endpoint": r[1], "p256dh": r[2], "auth": r[3]}
                for r in cur.fetchall()
            ]
    except Exception as exc:
        logger.error("Failed to load push subscriptions: %s", exc)
        return

    if not subs:
        return

    # The email subject already reads as a headline ("⚠ Charger Offline — ARG - Left"),
    # so it doubles as the notification title.
    payload = build_payload(alert_type, subject, evse_name, message, timestamp_ak)
    delivered = send_to_subscriptions(conn, subs, payload)
    logger.info("Push: %d/%d device(s) notified for %s on %s",
                delivered, len(subs), alert_type, evse_name)


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
    2. Deliver on each channel that user enabled: email, Web Push, or both.
    3. Insert one row into fired_alerts (logged once per firing).
    4. Push an SSE broadcast, scoped to the entitled subscribers only.

    `enabled` is the master subscription switch — it decides who is in scope at
    all, and therefore who sees the alert in History and in the banner.
    email_enabled / push_enabled then choose the delivery channels, so a user
    can move to push-only without losing the alert from the rest of the UI.
    """
    # ── Find subscribed recipients ────────────────────────────────────────────
    email_targets:  list[str] = []
    push_user_ids:  list[str] = []
    subscriber_ids: set[str]  = set()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT asub.user_id::text, pu.email,
                       asub.email_enabled, asub.push_enabled
                FROM alert_subscriptions asub
                -- alert_subscriptions.user_id is the SUPABASE AUTH UID, which is
                -- NOT portal_users.id. This join used to be
                -- `portal_users pu ON pu.id = asub.user_id`, comparing the two
                -- unrelated keys — so every subscription saved through the UI
                -- matched nothing and delivered nothing (pm_due_14d/pm_overdue
                -- never fired at all). Resolve auth uid → email → portal_users,
                -- case-insensitively, the same way auth.py does its lookup.
                JOIN auth.users  au ON au.id = asub.user_id
                JOIN portal_users pu ON lower(pu.email) = lower(au.email)
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
            for user_id, email, email_on, push_on in cur.fetchall():
                subscriber_ids.add(user_id)
                if email_on:
                    email_targets.append(email)
                if push_on:
                    push_user_ids.append(user_id)
    except Exception as exc:
        logger.error("Failed to query alert subscriptions: %s", exc)

    # ── Send per-user emails ──────────────────────────────────────────────────
    for email in email_targets:
        _send_email_to(email, subject, body_html)

    # ── Send Web Push to every registered device of the push subscribers ──────
    if push_user_ids:
        _send_push(conn, push_user_ids, alert_type, subject, evse_name, message, timestamp_ak)

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

    # ── SSE browser banner (scoped to entitled subscribers) ───────────────────
    _broadcast(alert_type, asset_id, evse_name, message, timestamp_ak, subscriber_ids)


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
            r"""
            SELECT
                e.asset_id,
                e.received_at,
                e.connector_id,
                e.action_payload->>'status'           AS status,
                e.action_payload->>'errorCode'        AS error_code,
                e.action_payload->>'vendorErrorCode'  AS vendor_error_code,
                -- Vendor error description, scoped to the station's manufacturer
                -- (same lookup the Status History tab uses in routers/status.py).
                CASE
                    WHEN (e.action_payload->>'vendorErrorCode') !~ '^\d+$' THEN NULL
                    WHEN ut.manufacturer = 'Tritium'
                        THEN (SELECT t.description FROM tritium_error_codes t
                              WHERE t.code = (e.action_payload->>'vendorErrorCode')::integer
                              LIMIT 1)
                    WHEN ut.manufacturer = 'Alpitronic'
                        THEN (SELECT a.description FROM alpitronic_error_codes a
                              WHERE a.error_code = (e.action_payload->>'vendorErrorCode')::integer
                              LIMIT 1)
                    ELSE NULL
                END                                    AS vendor_error_description
            FROM ocpp_events e
            LEFT JOIN chargers c    ON c.external_id = e.asset_id
            LEFT JOIN unit_types ut ON ut.id = c.unit_type_id
            WHERE e.action = 'StatusNotification'
              AND e.action_payload->>'errorCode' != 'NoError'
              AND e.received_at >= %s
            ORDER BY e.received_at ASC
            """,
            (lookback,),
        )
        rows = cur.fetchall()

    for row in rows:
        sid, recv_at, conn_id, status, error_code, vendor_code, vendor_desc = row
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
        if vendor_desc:
            detail += f" — {vendor_desc[:200]}"
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
                    ("Vendor Error", vendor_desc or "—"),
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
                    -- v3.5 BUGFIX: this read meterStart off the StopTransaction
                    -- payload, which OCPP 1.6 does not put there — 300 of 300
                    -- sampled stops carry meterStop and transactionId only. The
                    -- subtraction was therefore NULL on every row, the
                    -- `energy_kwh IS NOT NULL` filter below dropped all of them,
                    -- and this alert had never fired once since it was written.
                    -- meterStart lives on the StartTransaction CALL, which
                    -- carries no transactionId (that comes back in the
                    -- CALL_RESULT, which the webhook does not forward), so it is
                    -- matched on connector + charger-stamped timestamp the same
                    -- way sessions.py does. The sampled window is the fallback:
                    -- slightly short, but this is a "< 1 kWh" threshold test, so
                    -- a few watt-hours cannot change the answer.
                    COALESCE(
                        ((e.action_payload->>'meterStop')::numeric - (
                            SELECT (o.action_payload->>'meterStart')::numeric
                            FROM ocpp_events o
                            WHERE o.asset_id = e.asset_id
                              AND o.action   = 'StartTransaction'
                              AND o.received_at BETWEEN e.received_at - INTERVAL '24 hours'
                                                    AND e.received_at
                            ORDER BY o.received_at DESC
                            LIMIT 1)) / 1000.0,
                        (SELECT (MAX(mv.energy_wh) - MIN(mv.energy_wh)) / 1000.0
                         FROM meter_values_parsed mv
                         WHERE mv.station_id     = e.asset_id
                           AND mv.transaction_id = (e.action_payload->>'transactionId')::bigint)
                    )                                                  AS energy_kwh
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


# ── PM due-date alerts ────────────────────────────────────────────────────────

def _check_double_charge(conn) -> None:
    """Alert when a card settled for a session an app credential tried to start.

    The fingerprint, confirmed twice at Glennallen in August 2026: a driver's app
    start does not take, they tap a card, the charger opens the transaction on
    the reader's tag — and LynkWell invoices the app account anyway, on top of
    the $33.26 / $28.49 the card already paid. Neither driver told us; both were
    found by hand at month-end, four weeks late.

    Same three conditions as DOUBLE_CHARGE_SQL in routers/sessions.py, which is
    what paints the badge in the Sessions tab; this is the push/email half so it
    does not wait for someone to open the tab. Measured against the full history
    (557 card-matched sessions, Jan-Sep 2026) it fires 7 times — under one a
    month, and both real cases are in it.

    Dedupe is on fired_alerts rather than an in-memory set: this alert is about
    money and must survive a restart. fired_alerts is pruned at 15 days and the
    scan window is 72 h, so the row is always still there when it matters.

    The 72 h window is deliberately much wider than the poll interval. The flag
    only becomes true once the Payter/Nayax collector has fetched the settlement
    and matched it, which can be hours after the session ended.
    """
    with conn.cursor() as cur:
        cur.execute(
            r"""
            WITH recent AS (
                SELECT mv.station_id, mv.connector_id, mv.transaction_id,
                       MIN(mv.ts) AS start_utc
                FROM meter_values_parsed mv
                WHERE mv.transaction_id IS NOT NULL
                  AND mv.ts >= NOW() - INTERVAL '72 hours'
                GROUP BY 1, 2, 3
            ),
            carded AS (
                SELECT r.station_id, r.connector_id, r.transaction_id, r.start_utc,
                       ct.committed_cents, ct.entry_mode, ct.masked_pan, ct.vendor,
                       (SELECT o.action_payload->>'idTag'
                          FROM ocpp_events o
                         WHERE o.asset_id = r.station_id
                           AND o.action   = 'StartTransaction'
                           AND (o.action_payload->>'connectorId')::int = r.connector_id
                           AND o.received_at BETWEEN r.start_utc - INTERVAL '6 hours'
                                                 AND r.start_utc + INTERVAL '24 hours'
                           AND (o.action_payload->>'timestamp')::timestamptz
                                 BETWEEN r.start_utc - INTERVAL '6 hours'
                                     AND r.start_utc + INTERVAL '5 minutes'
                         ORDER BY ABS(EXTRACT(EPOCH FROM (
                             (o.action_payload->>'timestamp')::timestamptz - r.start_utc))) ASC
                         LIMIT 1) AS auth_tag
                FROM recent r
                JOIN card_transactions ct
                  ON ct.station_id     = r.station_id
                 AND ct.connector_id  IS NOT DISTINCT FROM r.connector_id
                 AND ct.transaction_id = r.transaction_id::text
            )
            SELECT c.station_id, c.connector_id, c.transaction_id, c.start_utc,
                   c.committed_cents, c.entry_mode, c.masked_pan, c.vendor,
                   (SELECT az.action_payload->>'idTag'
                      FROM ocpp_events az
                     WHERE az.asset_id = c.station_id
                       AND az.action   = 'Authorize'
                       AND az.received_at BETWEEN c.start_utc - INTERVAL '10 minutes'
                                              AND c.start_utc
                       AND az.action_payload->>'idTag' ~ '^[0-9A-Z]{20}$'
                       AND az.action_payload->>'idTag' IS DISTINCT FROM c.auth_tag
                       AND NOT EXISTS (
                           SELECT 1 FROM ocpp_events sx
                            WHERE sx.asset_id = c.station_id
                              AND sx.action   = 'StartTransaction'
                              AND sx.action_payload->>'idTag' = az.action_payload->>'idTag'
                              AND sx.received_at BETWEEN c.start_utc - INTERVAL '30 minutes'
                                                     AND c.start_utc + INTERVAL '30 minutes')
                     ORDER BY az.received_at DESC
                     LIMIT 1) AS stranded_tag
            FROM carded c
            -- strpos, not LIKE: this statement takes no parameters, so psycopg
            -- does no %-interpolation and a '%%' would survive into the SQL
            -- literally. Substring search sidesteps the escaping question
            -- entirely. The trailing space keeps tx 110830 from matching
            -- tx 1108305.
            WHERE NOT EXISTS (
                SELECT 1 FROM fired_alerts fa
                 WHERE fa.alert_type = 'double_charge'
                   AND strpos(fa.message, 'tx ' || c.transaction_id || ' ') > 0)
            ORDER BY c.start_utc ASC
            """
        )
        rows = cur.fetchall()

    for (sid, conn_id, tx_id, start_utc, cents, entry_mode, pan, vendor, stranded) in rows:
        if not stranded:
            continue                      # no stranded app credential — ordinary card session
        if start_utc.tzinfo is None:
            start_utc = start_utc.replace(tzinfo=timezone.utc)

        name    = display_name(sid)
        ts      = _fmt_ak(start_utc)
        amount  = f"${(cents or 0) / 100:.2f}"
        msg     = (f"Card charged {amount} on tx {tx_id} — app credential "
                   f"{stranded} was presented first and never started a session. "
                   f"Check LynkWell for a second charge.")
        _fire_alert(
            conn,
            alert_type = "double_charge",
            asset_id   = sid,
            evse_name  = name,
            message    = msg,
            subject    = f"💳 Possible double charge: {name} — {amount}",
            body_html  = _alert_body(
                "Possible Double Charge — Review Against LynkWell",
                [
                    ("Charger",           name),
                    ("Connector",         str(conn_id) if conn_id else "—"),
                    ("Session started",   ts),
                    ("Transaction ID",    str(tx_id)),
                    ("Card charged",      f"{amount} ({entry_mode or '—'}, {vendor or '—'})"),
                    ("Card",              pan or "—"),
                    ("Stranded app tag",  stranded),
                    ("What to check",     "Open this session in LynkWell. If the app "
                                          "account was also invoiced, the driver paid "
                                          "twice and the app charge needs refunding."),
                ],
            ),
            timestamp_ak = ts,
        )


def _fire_pm_alert(
    conn,
    alert_type: str,
    charger_id: str,
    evse_name: str,
    message: str,
    subject: str,
    body_html: str,
    timestamp_ak: str,
) -> None:
    """PM variant of _fire_alert — skips EVSE access filter.

    Fleet units like the Terra184 have no external_id, so the standard
    allowed_evse_ids check would exclude them.  PM subscriptions are sent
    to every active user who has opted in to the PM alert type.
    """
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
                """,
                (alert_type,),
            )
            recipients = [row[0] for row in cur.fetchall()]
    except Exception as exc:
        logger.error("Failed to query PM alert subscriptions: %s", exc)

    for email in recipients:
        _send_email_to(email, subject, body_html)

    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fired_alerts (alert_type, asset_id, evse_name, message) "
                "VALUES (%s, %s, %s, %s)",
                (alert_type, charger_id, evse_name, message),
            )
        conn.commit()
    except Exception as exc:
        logger.error("Failed to log fired PM alert: %s", exc)

    _broadcast(alert_type, evse_name, message, timestamp_ak)


def _check_pm_due(conn) -> None:
    """Check all active fleet units for upcoming or overdue PMs.

    Cadence:
      - pm_due_14d  : fires once when next_pm_due_date is 13–15 days out.
                      Deduplicated via fired_alerts (20-day window per unit).
      - pm_overdue  : fires when due today (days_until == 0) or overdue
                      (days_until < 0).  Fires again weekly while still overdue.
                      Deduplicated via fired_alerts (7-day window per unit).
    """
    today = date.today()
    now_ak = _fmt_ak(datetime.now(tz=timezone.utc))

    # Fetch all active chargers with unit_type PM intervals and last PM timestamps
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH last_pm AS (
                SELECT
                    charger_id,
                    MAX(CASE WHEN record_type = 'pm_quarterly'
                             THEN record_timestamp END)                    AS last_q,
                    MAX(CASE WHEN record_type = 'pm_semi_annual'
                             THEN record_timestamp END)                    AS last_sa,
                    MAX(CASE WHEN record_type IN ('pm_annual', 'pm_general')
                             THEN record_timestamp END)                    AS last_a
                FROM maintenance_records
                WHERE record_type IN
                      ('pm_quarterly','pm_semi_annual','pm_annual','pm_general')
                GROUP BY charger_id
            )
            SELECT
                c.id::text                        AS charger_id,
                c.name,
                c.serial_number,
                ut.interval_quarterly_months,
                ut.interval_semiannual_months,
                ut.interval_annual_months,
                lp.last_q,
                lp.last_sa,
                lp.last_a
            FROM chargers c
            LEFT JOIN unit_types ut ON ut.id = c.unit_type_id
            LEFT JOIN last_pm lp    ON lp.charger_id = c.id
            WHERE c.status = 'active'
              AND c.unit_type_id IS NOT NULL
            """
        )
        rows = cur.fetchall()

    def _next_due_date(last_ts, months) -> date | None:
        if not months or not last_ts:
            return None
        last_d = last_ts.date() if hasattr(last_ts, "date") else last_ts
        return date.fromordinal(last_d.toordinal() + months * 30)

    for row in rows:
        (charger_id, name, serial,
         q_months, sa_months, a_months,
         last_q, last_sa, last_a) = row
        a_months = a_months or 12

        candidates = [
            (d, label)
            for d, label in [
                (_next_due_date(last_q,  q_months),  "Quarterly"),
                (_next_due_date(last_sa, sa_months), "Semi-Annual"),
                (_next_due_date(last_a,  a_months),  "Annual"),
            ]
            if d is not None
        ]
        if not candidates:
            continue  # no PM history yet — no calculated due date

        candidates.sort(key=lambda x: x[0])
        next_due, pm_label = candidates[0]
        days_until = (next_due - today).days
        unit_label = f"{name}{f' ({serial})' if serial else ''}"

        if 13 <= days_until <= 15:
            # ── 14-day advance notice ─────────────────────────────────────
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM fired_alerts "
                    "WHERE alert_type = 'pm_due_14d' AND asset_id = %s "
                    "  AND fired_at >= NOW() - INTERVAL '20 days'",
                    (charger_id,),
                )
                already = cur.fetchone()
            if already:
                continue

            logger.info("Firing pm_due_14d for %s (due %s)", unit_label, next_due)
            _fire_pm_alert(
                conn,
                alert_type   = "pm_due_14d",
                charger_id   = charger_id,
                evse_name    = unit_label,
                message      = (f"{pm_label} PM due in {days_until} days "
                                f"({next_due.isoformat()})"),
                subject      = f"🔔 PM Due in 14 Days: {unit_label}",
                body_html    = _alert_body(
                    f"Preventive Maintenance Due in {days_until} Days",
                    [
                        ("Charger",       unit_label),
                        ("PM Type",       pm_label),
                        ("Due Date",      next_due.isoformat()),
                        ("Days Until Due", str(days_until)),
                        ("Serial Number", serial or "—"),
                        ("Action",        "Schedule PM visit before due date"),
                    ],
                ),
                timestamp_ak = now_ak,
            )

        elif days_until <= 0:
            # ── Due today or overdue — weekly reminder ────────────────────
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM fired_alerts "
                    "WHERE alert_type = 'pm_overdue' AND asset_id = %s "
                    "  AND fired_at >= NOW() - INTERVAL '7 days'",
                    (charger_id,),
                )
                already = cur.fetchone()
            if already:
                continue

            days_overdue = abs(days_until)
            if days_overdue == 0:
                msg     = (f"{pm_label} PM due today ({next_due.isoformat()})")
                subject = f"🔔 PM Due Today: {unit_label}"
                title   = "Preventive Maintenance Due Today"
                detail  = "Due today"
            else:
                msg     = (f"{pm_label} PM overdue by {days_overdue} day"
                           f"{'s' if days_overdue != 1 else ''} "
                           f"(was due {next_due.isoformat()})")
                subject = f"⚠ PM Overdue ({days_overdue}d): {unit_label}"
                title   = (f"Preventive Maintenance Overdue by {days_overdue} "
                           f"Day{'s' if days_overdue != 1 else ''}")
                detail  = f"{days_overdue} day{'s' if days_overdue != 1 else ''} overdue"

            logger.info("Firing pm_overdue for %s (%s)", unit_label, detail)
            _fire_pm_alert(
                conn,
                alert_type   = "pm_overdue",
                charger_id   = charger_id,
                evse_name    = unit_label,
                message      = msg,
                subject      = subject,
                body_html    = _alert_body(
                    title,
                    [
                        ("Charger",       unit_label),
                        ("PM Type",       pm_label),
                        ("Due Date",      next_due.isoformat()),
                        ("Status",        detail),
                        ("Serial Number", serial or "—"),
                        ("Action",        "Complete PM and log record in the Maintenance Tracker"),
                    ],
                ),
                timestamp_ak = now_ak,
            )


# ── Main poll loop ────────────────────────────────────────────────────────────

def _run_poll_loop() -> None:
    global _cleanup_counter, _pm_check_counter
    logger.info("Alert poll loop started (interval=%ds)", POLL_INTERVAL_SEC)

    # Ensure the connector-count odometer tables exist before the first tick.
    try:
        conn = get_conn_sync()
        try:
            ensure_connector_count_tables(conn)
        finally:
            conn.close()
    except Exception as exc:
        logger.error("connector_counts table init failed: %s", exc, exc_info=True)

    while True:
        try:
            conn = get_conn_sync()
            try:
                _check_offline_idle(conn)
                _check_offline_mid_session(conn)
                _check_faults(conn)
                _check_suspicious_vid(conn)
                _check_double_charge(conn)

                # Roll the connector-count odometer forward (plug-in counter).
                accumulate_connector_counts(conn)

                # PM due-date check — runs every ~1 hour
                _pm_check_counter += 1
                if _pm_check_counter >= _PM_CHECK_EVERY_N:
                    _check_pm_due(conn)
                    _pm_check_counter = 0

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
