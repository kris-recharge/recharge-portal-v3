"""GET /api/sessions — charging session list with pagination."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query

from ..auth import CurrentUser, filter_evse_ids
from ..constants import (
    connector_type_for,
    display_name,
    get_all_station_ids,
    is_rfid_tag,
    is_terminal_tag,
    location_label,
)
from ..db import acquire
from ..models import ChargingSession, MeterValuePoint, SessionDetailResponse, SessionsResponse

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

_AK = ZoneInfo("America/Anchorage")


def _fmt_ak(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_AK).strftime("%Y-%m-%d %H:%M")


def _auth_method(start_tag: str, card_matched: bool = False) -> str:
    """Derive Authentication Method for a session. Shared with the export.

    The tag that STARTED the transaction is the only reliable idTag signal —
    Authorize events near session start include rejected AutoCharge probes
    (Autel sends a VID: Authorize on every plug-in) and so mislabel CC sessions.
    Validated 100% (57/57) against Kris's hand-corrected Autel export
    (2026-07-13).

    A matched card-terminal tap outranks the tag entirely (v3.3, 2026-08-04).
    The CCRs on the Alpitronic units don't present a fixed terminal tag the way
    every older reader does — CL-C's first card session started with
    VPCUUT5TWFOLGNY863HC, 20 chars of A-Z/0-9, indistinguishable by shape from a
    LynkWell app token. Whether that tag is fixed per terminal or minted per tap
    is still unknown (n=1 at the time of writing), so classification must not
    depend on the answer: if the matcher linked a committed tap to this session,
    a card was charged and it's CC.

    card_transactions match     → CC (authoritative — a card was actually charged,
                                  Payter or Nayax alike)
    VID:*                       → AutoCharge (vehicle-initiated)
    tag in TERMINAL_TAGS        → CC (a reader's fixed authorisation tag)
    tag in RFID_TAGS            → RFID (a card issued to a driver)
    20-char A-Z/0-9 token       → App (LynkWell remote-start idTag)
    anything else               → Unknown
    blank (no StartTransaction) → unknown, left blank

    v3.5 — the last branch used to return CC, which meant every credential this
    code had never seen was booked as a credit card. RFID cards landed there and
    inflated the bucket that gets tied out against Payter/Nayax settlement, with
    no transaction behind them ($53.57 in August 2026). It now returns "Unknown",
    which is a prompt to come look rather than a silent miscategorisation — the
    same reasoning the card_transactions view applies to unrecognised entry
    modes. When a reader is swapped, its new tag lands here until it is added to
    TERMINAL_TAGS, and the Sessions tab will show it.

    Terminal tags ARE configuration now, in constants.TERMINAL_TAGS — that is
    the v3.5 change. Swapping a reader therefore needs two edits, not one: add
    the new tag there, and update chargers.payter_serial / nayax_serial or the
    card matcher stops linking that charger's taps (see payter_schema.sql).
    ARG-Right has been through three readers this year — 161D77C442099C from
    2026-01-07, 5875910F1313E9 from 2026-08-10, AAE40F780E97C9 from 2026-08-20 —
    and the middle one has zero StartTransactions on record, so it is not in the
    map. If it ever surfaces it will read "Unknown", which is the point.
    """
    if card_matched:
        return "CC"
    if not start_tag:
        return ""
    if start_tag.startswith("VID:"):
        return "AutoCharge"
    if is_terminal_tag(start_tag):
        return "CC"
    if is_rfid_tag(start_tag):
        return "RFID"
    # Shape rule stays BELOW the tag look-ups: at Cooper Landing a Payter Apollo
    # in Cloud mode mints a fresh 20-character tag per tap, shape-identical to an
    # app token, so this branch is only safe once card_matched has had its say.
    if re.fullmatch(r"[0-9A-Z]{20}", start_tag):
        return "App"
    return "Unknown"


def _vid_tag(start_tag: str, authorize_vid: str | None) -> str | None:
    """Resolve the vehicle ID to show for a session. Shared with the export.

    Two sources, most authoritative first:

    1. The StartTransaction idTag, when it is VID:-prefixed. This is the
       credential the charger actually opened the transaction with, and unlike
       an Authorize it carries a connectorId, so it can't be borrowed from the
       other side of a dual-connector unit.
    2. The nearest VID:* Authorize — the AutoCharge probe. Still worth showing
       when the probe was rejected and the driver fell back to a card or the
       app: it names the vehicle that plugged in.

    Both sources existed before v3.4 but only (2) was read, which blanked the
    VID for any session that skipped the probe. The Alpitronic HYC400s at
    Cooper Landing do exactly that once a vehicle's AutoCharge is enrolled:
    they send no Authorize at all and stamp the VID straight onto
    StartTransaction (CL-B connector 2, VID:00182335371B, 2026-08-29 and
    2026-09-01 — Authentication read "AutoCharge" while the VID column sat
    empty). The units still probe-then-fall-back for unenrolled vehicles, so
    both paths stay live on the same charger.
    """
    if start_tag.startswith("VID:"):
        return start_tag
    return authorize_vid or None


def _resolve_soc(
    raw_start:         float | None,
    raw_first_nonzero: float | None,
    raw_end:           float | None,
    raw_last_nonzero:  float | None = None,
) -> tuple[float | None, float | None]:
    """Return (soc_start_pct, soc_end_pct) with three corrections applied.

    1. Scale normalisation — some chargers report SoC as a 0-1 fraction instead
       of 0-100.  The scale is derived from a *genuine* (non-zero) reading:
       a 0% reading carries no scale information, so basing the decision on it
       (e.g. a bogus soc_end=0) would wrongly treat 0 ≤ 1.0 as fraction-scale
       and multiply real readings by 100 (turning a 24% start into 2400%).

    2. Leading bogus-zero filter — chargers often emit one or more soc=0
       readings at session start before the BMS has responded.  Rule: if the
       first reading is 0% AND the first *non-zero* reading is NOT ≈ 1% (i.e.
       the car was not actually near-depleted), discard the leading zeros and
       use the first non-zero reading as the true starting SoC.  If
       soc_first_nonzero ≤ 1.5% we assume the car genuinely started near-empty
       and keep 0%.

    3. Trailing bogus-zero filter — symmetric to (2): some chargers emit a
       soc=0 reading at session teardown after the BMS has disconnected.  A
       charging session can never legitimately *end* at 0%, so if the last
       reading is 0% we fall back to the last non-zero reading for the end.
    """
    # Scale from the most-reliable *non-zero* reference. Prefer the last
    # non-zero reading (highest SoC, end of charge), then first non-zero,
    # then the raw endpoints — skipping any 0 which tells us nothing.
    ref = next(
        (float(v) for v in (raw_last_nonzero, raw_first_nonzero, raw_end, raw_start)
         if v is not None and float(v) > 0.0),
        None,
    )
    scale = 100.0 if (ref is not None and ref <= 1.0) else 1.0

    # Trailing-zero artifact: a 0% end is a teardown reading, not a real SoC.
    raw_end_eff = raw_end
    if raw_end is not None and float(raw_end) == 0.0 and raw_last_nonzero is not None:
        raw_end_eff = raw_last_nonzero

    soc_end_pct = round(float(raw_end_eff) * scale, 1) if raw_end_eff is not None else None

    if raw_start is None:
        return None, soc_end_pct

    start_val = float(raw_start) * scale

    # Leading-zero artifact: skip any number of 0% readings until we find a
    # meaningful SoC, but only if that value is clearly above 1.5%
    # (preserves genuine near-depleted starts like 0 → 1 → 2%).
    if start_val == 0.0 and raw_first_nonzero is not None:
        nonzero_val = float(raw_first_nonzero) * scale
        if nonzero_val > 1.5:
            start_val = nonzero_val

    return round(start_val, 1), soc_end_pct


# ── Shared SQL fragments (v3.5) ───────────────────────────────────────────────
#
# sessions.py and export.py build the same session aggregate, and the handover
# note already flags that duplication as a hazard. These two fragments are
# defined once and interpolated into both, so the energy basis and the
# double-charge rule cannot drift between the table and the spreadsheet.

# Where the true meter registers live, when the charger sends them.
#
# Sessions are aggregated from meter_values_parsed, so their energy is
# MAX(register) - MIN(register) across the periodic samples. That window is
# strictly INSIDE the transaction: whatever was delivered between
# StartTransaction and the first sample, and between the last sample and
# StopTransaction, is never counted. LynkWell bills meterStop - meterStart, so
# we are always the lower number — 6.342 kWh and $3.14 short across 201
# driver-authenticated sessions in August 2026, and never once over.
#
# Two better sources, both already in ocpp_events:
#
#   1. StopTransaction.transactionData, when the charger includes the energy
#      register tagged Transaction.Begin / Transaction.End. This is the
#      charger's own transaction record — the register pair in one message.
#      Glennallen and both Delta units send it on 94-100% of stops (and those
#      sites tie out); the Tritium units at ARG send only the End half; the
#      four Autel HYC400s at Cooper Landing send neither, which is exactly
#      where 90% of the shortfall lives. That is a StopTxnSampledData
#      configuration key on those units, not a code problem — once LynkWell
#      adds Energy.Active.Import.Register to it, branch 1 covers the fleet.
#
#   2. StopTransaction.meterStop paired with StartTransaction.meterStart.
#      NOTE meterStart is NOT in the StopTransaction payload — 300 of 300
#      sampled stops carry meterStop and transactionId only. (app/alerts.py
#      assumed otherwise and has been subtracting NULL, which is why the
#      suspicious-VID alert has never fired; fixed there too.) So meterStart has
#      to be read off the StartTransaction CALL, matched on connector and the
#      charger-stamped payload timestamp — the same join with_auth already does
#      for auth_tag, because a StartTransaction CALL carries no transactionId
#      (that arrives in the CALL_RESULT, which the webhook does not forward).
SESSION_ENERGY_SOURCES_SQL = """
                    (SELECT (sv->>'value')::numeric
                       FROM ocpp_events st
                       CROSS JOIN LATERAL jsonb_array_elements(
                            st.action_payload->'transactionData') td
                       CROSS JOIN LATERAL jsonb_array_elements(td->'sampledValue') sv
                      WHERE st.asset_id       = s.station_id
                        AND st.action         = 'StopTransaction'
                        AND st.transaction_id = s.transaction_id
                        AND sv->>'measurand'  = 'Energy.Active.Import.Register'
                        AND sv->>'context'    = 'Transaction.Begin'
                        AND sv->>'unit'       = 'Wh'
                      LIMIT 1)                                  AS reg_begin_wh,
                    (SELECT (sv->>'value')::numeric
                       FROM ocpp_events st
                       CROSS JOIN LATERAL jsonb_array_elements(
                            st.action_payload->'transactionData') td
                       CROSS JOIN LATERAL jsonb_array_elements(td->'sampledValue') sv
                      WHERE st.asset_id       = s.station_id
                        AND st.action         = 'StopTransaction'
                        AND st.transaction_id = s.transaction_id
                        AND sv->>'measurand'  = 'Energy.Active.Import.Register'
                        AND sv->>'context'    = 'Transaction.End'
                        AND sv->>'unit'       = 'Wh'
                      LIMIT 1)                                  AS reg_end_wh,
                    (SELECT (st.action_payload->>'meterStop')::numeric
                       FROM ocpp_events st
                      WHERE st.asset_id       = s.station_id
                        AND st.action         = 'StopTransaction'
                        AND st.transaction_id = s.transaction_id
                      LIMIT 1)                                  AS meter_stop_wh,
                    (SELECT (o.action_payload->>'meterStart')::numeric
                       FROM ocpp_events o
                      WHERE o.asset_id = s.station_id
                        AND o.action   = 'StartTransaction'
                        AND (o.action_payload->>'connectorId')::int = s.connector_id
                        AND o.received_at BETWEEN s.start_utc - INTERVAL '6 hours'
                                              AND s.start_utc + INTERVAL '24 hours'
                        AND (o.action_payload->>'timestamp')::timestamptz
                              BETWEEN s.start_utc - INTERVAL '6 hours'
                                  AND s.start_utc + INTERVAL '5 minutes'
                      ORDER BY ABS(EXTRACT(EPOCH FROM (
                          (o.action_payload->>'timestamp')::timestamptz - s.start_utc))) ASC
                      LIMIT 1)                                  AS meter_start_wh
"""

# Pick the best available basis. Both candidates are sanity-bounded against the
# MeterValues figure rather than trusted outright:
#
#   >= mv - 100 Wh — clipping can only LOSE energy, so a candidate materially
#              below the sampled window means the wrong event was matched. The
#              100 Wh floor is slack for representation, not for error:
#              meterStart/meterStop are integers while meter_values_parsed.
#              energy_wh is numeric with a decimal, so the two disagree by a
#              watt-hour on perfectly good data. Requiring a strict >= threw the
#              authoritative register away on 1,161 of 2,104 sessions, nearly all
#              of them off by exactly -1 Wh.
#   <= mv + 10 kWh — bounds the excess by what a charger can physically deliver
#              in the missing head+tail. At 200 kW (the fastest unit on the
#              estate) that is three minutes of sampling gap; anything larger is
#              a mis-joined StartTransaction from an adjacent session, a real
#              risk on a dual-connector unit replaying buffered events. Two
#              sessions on 2026-01-07 — the first day of collection, when the
#              sampled window itself was incomplete — trip this and keep the
#              MeterValues figure.
#
# A candidate outside the band is discarded and the next source tried, so the
# worst case is today's behaviour rather than a wrong number.
SESSION_ENERGY_COALESCE_SQL = """
                    CASE
                      WHEN reg_begin_wh IS NOT NULL AND reg_end_wh IS NOT NULL
                       AND reg_end_wh - reg_begin_wh >= energy_wh_delta - 100
                       AND reg_end_wh - reg_begin_wh <= energy_wh_delta + 10000
                        THEN GREATEST(reg_end_wh - reg_begin_wh, 0)
                      WHEN meter_start_wh IS NOT NULL AND meter_stop_wh IS NOT NULL
                       AND meter_stop_wh - meter_start_wh >= energy_wh_delta - 100
                       AND meter_stop_wh - meter_start_wh <= energy_wh_delta + 10000
                        THEN GREATEST(meter_stop_wh - meter_start_wh, 0)
                      ELSE energy_wh_delta
                    END::numeric                        AS energy_wh_delta
"""

# A card paid for this session, but an app credential was presented moments
# before and never started anything. That is the fingerprint of a driver whose
# app start did not take, who then tapped a card — and LynkWell has twice billed
# the app account anyway, on top of the card. Two confirmed in August 2026:
# Glennallen 11 Aug ($33.26 card + $33.25 invoiced) and 12 Aug ($28.49 + $28.48).
#
# Three conditions, each one earning its place against the full history
# (557 card-matched sessions, January-September 2026):
#
#   card_matched          — a settled Payter/Nayax transaction, so money moved.
#   app-shaped Authorize  — 20 chars of A-Z0-9 within 10 minutes before the
#                           start. Every confirmed case sat 35-542 s ahead.
#   not this session's own tag, and never consumed
#                         — without the "consumed" test the rule also fires on a
#                           driver who ran an app session, ended it, and then
#                           started a second session on a card. Requiring that
#                           the app tag started NO transaction on that charger
#                           within +/- 30 min removes exactly those and keeps
#                           every real one: 9 flags become 7 over nine months.
#
# It cannot catch everything. The 11 Aug session produced no Authorize at all on
# our feed — the charger had just rebooted and LynkWell's RemoteStartTransaction
# is CSMS->charger, a direction the webhook does not forward (0 of 168,784
# events). Until that direction arrives, roughly half of these are invisible.
DOUBLE_CHARGE_SQL = """
                    (card_matched AND EXISTS (
                        SELECT 1 FROM ocpp_events az
                         WHERE az.asset_id = with_auth.station_id
                           AND az.action   = 'Authorize'
                           AND az.received_at BETWEEN with_auth.start_utc - INTERVAL '10 minutes'
                                                  AND with_auth.start_utc
                           AND az.action_payload->>'idTag' ~ '^[0-9A-Z]{20}$'
                           AND az.action_payload->>'idTag' IS DISTINCT FROM with_auth.auth_tag
                           AND NOT EXISTS (
                               SELECT 1 FROM ocpp_events sx
                                WHERE sx.asset_id = with_auth.station_id
                                  AND sx.action   = 'StartTransaction'
                                  AND sx.action_payload->>'idTag'
                                        = az.action_payload->>'idTag'
                                  AND sx.received_at
                                        BETWEEN with_auth.start_utc - INTERVAL '30 minutes'
                                            AND with_auth.start_utc + INTERVAL '30 minutes')))
"""


# v3.6 — "the charger opened a transaction that delivered nothing".
#
# Correlated against a StartTransaction CALL aliased `e`; true when that
# transaction is provably zero-energy. Used to stop such a StartTransaction from
# counting as proof that charging began (charge_sig), which is what hid six CL-C
# and CL-D attempts on 2026-09-09: the driver plugged in, the HYC400 accepted the
# AutoCharge VID and opened a transaction, the V2G handshake failed, and it
# stopped 40-65 s later with the register unmoved. No energy flowed, so the unit
# sent no MeterValues, so no row reached meter_values_parsed and no session
# existed — while the StartTransaction alone was enough to disqualify the episode
# from the failed-attempt path too. Invisible on both counts.
#
# A StartTransaction CALL carries no transactionId (that arrives in the
# CALL_RESULT, which the webhook does not forward — see note 2 above), so the
# stop has to be found positionally. The equality test IS the pairing guard: the
# registers are per-connector cumulative Wh in the millions, so a StopTransaction
# belonging to the other connector of a dual-port unit will not coincidentally
# report a meterStop equal to this connector's meterStart. That also makes the
# match order-insensitive, so a concurrent session on the sibling connector
# closing first cannot mask a real zero-energy attempt.
#
# Deliberately NOT keyed on "no MeterValues for this transaction": 8 stops in the
# 180 days to 2026-09-09 delivered real energy with no meter_values_parsed rows
# and no Charging status (all in the 2026-06-28/29 webhook-replay bursts, where
# an ingest outage dropped the samples). Keying on absence would have booked
# those as failed attempts. meterStop = meterStart is the charger's own
# assertion and survives an ingest gap.
#
# The 2 h cap matches the episode cap in `attempts`.
ZERO_ENERGY_TX_SQL = """
                        SELECT 1 FROM ocpp_events sp
                         WHERE sp.asset_id = e.asset_id
                           AND sp.action   = 'StopTransaction'
                           AND sp.received_at >  e.received_at
                           AND sp.received_at <  e.received_at + INTERVAL '2 hours'
                           AND (CASE WHEN sp.action_payload->>'meterStop' ~ '^[0-9]+$'
                                     THEN (sp.action_payload->>'meterStop')::bigint END)
                             = (CASE WHEN e.action_payload->>'meterStart' ~ '^[0-9]+$'
                                     THEN (e.action_payload->>'meterStart')::bigint END)
"""


def _expand(sql: str) -> str:
    """Splice the shared fragments into a query.

    Token replacement rather than an f-string on purpose: DOUBLE_CHARGE_SQL
    contains a POSIX regex with a `{20}` quantifier, and an f-string would try to
    read that as a field. The tokens are SQL comments, so an un-expanded query
    still parses — it just wouldn't have the columns, which fails loudly at the
    first fetch instead of silently returning wrong numbers.
    """
    return (
        sql.replace("/*ENERGY_SOURCES*/",  SESSION_ENERGY_SOURCES_SQL)
           .replace("/*ENERGY_COALESCE*/", SESSION_ENERGY_COALESCE_SQL)
           .replace("/*DOUBLE_CHARGE*/",   DOUBLE_CHARGE_SQL)
           .replace("/*ZERO_ENERGY_TX*/",  ZERO_ENERGY_TX_SQL)
    )


def _parse_dt_param(val: str, *, end: bool = False) -> datetime:
    """Accept YYYY-MM-DD (AK midnight boundary) **or** a full ISO-8601 datetime.

    Live-mode callers send a UTC ISO string (e.g. ``2026-03-08T08:50:00.000Z``).
    Static-mode callers send a bare date  (e.g. ``2026-03-08``).
    """
    if "T" in val or val.endswith("Z") or "+" in val[10:]:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    # Date-only — apply AK midnight / end-of-day boundary
    suffix = "T23:59:59" if end else "T00:00:00"
    return (
        datetime.fromisoformat(f"{val}{suffix}")
        .replace(tzinfo=_AK)
        .astimezone(timezone.utc)
    )


@router.get("", response_model=SessionsResponse)
async def get_sessions(
    user: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    station_id: list[str] | None = Query(None),
    start_date: str | None = Query(None, description="YYYY-MM-DD AK local OR ISO-8601 datetime (live mode)"),
    end_date:   str | None = Query(None, description="YYYY-MM-DD AK local OR ISO-8601 datetime (live mode)"),
):
    # Resolve EVSE filter
    all_ids = get_all_station_ids()
    allowed = filter_evse_ids(all_ids, user.allowed_evse_ids)
    if station_id:
        allowed = [s for s in station_id if s in allowed]

    # Convert date/datetime params → UTC timestamps for the query
    start_utc: datetime | None = _parse_dt_param(start_date)           if start_date else None
    end_utc:   datetime | None = _parse_dt_param(end_date, end=True)   if end_date   else None

    offset = (page - 1) * page_size

    async with acquire() as conn:
        # Build session aggregates from meter_values_parsed
        # One row per (station_id, connector_id, transaction_id)
        rows = await conn.fetch(
            _expand("""
            WITH sessions AS (
                SELECT
                    m.station_id,
                    m.connector_id,
                    m.transaction_id,
                    MIN(m.ts)                                         AS start_utc,
                    MAX(m.ts)                                         AS end_utc,
                    MAX(m.power_w)                                    AS max_power_w,
                    MAX(m.energy_wh) - MIN(m.energy_wh)               AS energy_wh_delta,
                    MIN(m.energy_wh)                                  AS energy_wh_min,
                    MAX(m.energy_wh)                                  AS energy_wh_max,
                    (SELECT mv2.soc FROM meter_values_parsed mv2
                     WHERE mv2.station_id = m.station_id
                       AND mv2.connector_id = m.connector_id
                       AND mv2.transaction_id = m.transaction_id
                       AND mv2.soc IS NOT NULL
                     ORDER BY mv2.ts ASC LIMIT 1)                     AS soc_start,
                    (SELECT mv2.soc FROM meter_values_parsed mv2
                     WHERE mv2.station_id = m.station_id
                       AND mv2.connector_id = m.connector_id
                       AND mv2.transaction_id = m.transaction_id
                       AND mv2.soc IS NOT NULL
                       AND mv2.soc > 0
                     ORDER BY mv2.ts ASC LIMIT 1)                     AS soc_first_nonzero,
                    (SELECT mv2.soc FROM meter_values_parsed mv2
                     WHERE mv2.station_id = m.station_id
                       AND mv2.connector_id = m.connector_id
                       AND mv2.transaction_id = m.transaction_id
                       AND mv2.soc IS NOT NULL
                     ORDER BY mv2.ts DESC LIMIT 1)                    AS soc_end,
                    (SELECT mv2.soc FROM meter_values_parsed mv2
                     WHERE mv2.station_id = m.station_id
                       AND mv2.connector_id = m.connector_id
                       AND mv2.transaction_id = m.transaction_id
                       AND mv2.soc IS NOT NULL
                       AND mv2.soc > 0
                     ORDER BY mv2.ts DESC LIMIT 1)                    AS soc_last_nonzero
                FROM meter_values_parsed m
                WHERE m.station_id = ANY($1::text[])
                  AND m.transaction_id IS NOT NULL
                  AND ($2::timestamptz IS NULL OR m.ts >= $2)
                  -- v3.2: filter by session START time. Widen reading window 1 day
                  -- past end so sessions that START in range aren't truncated; the
                  -- outer WHERE drops sessions that start after the window.
                  AND ($3::timestamptz IS NULL OR m.ts <= $3 + INTERVAL '1 day')
                GROUP BY m.station_id, m.connector_id, m.transaction_id
            ),
            with_auth AS (
                SELECT
                    s.*,
                    -- v3.5: the true meter registers, when the charger sends them.
                    -- See SESSION_ENERGY_SOURCES_SQL for why and which units do.
                    /*ENERGY_SOURCES*/,
                    (SELECT o.action_payload->>'idTag' FROM ocpp_events o
                     WHERE o.asset_id = s.station_id
                       AND o.action = 'Authorize'
                       AND o.action_payload->>'idTag' LIKE 'VID:%'
                       AND o.received_at BETWEEN s.start_utc - INTERVAL '60 minutes'
                                             AND s.start_utc + INTERVAL '5 minutes'
                       -- An Authorize carries no connectorId (OCPP 1.6 has no such
                       -- field — 0 of 4087 stored Authorizes have one), so on a
                       -- dual-connector unit the nearest probe in time can belong to
                       -- the other side. Attribute it by charger state instead: a
                       -- probe fires while its connector sits in Preparing, so
                       -- require THIS connector's last status at that instant to be
                       -- Preparing. Removed 22 borrowed VIDs across all history —
                       -- every one fired while this connector was Unavailable,
                       -- Available or Charging — and cost no correct ones.
                       AND EXISTS (
                           SELECT 1 FROM ocpp_events p
                            WHERE p.asset_id = s.station_id
                              AND p.action = 'StatusNotification'
                              AND p.connector_id = s.connector_id
                              AND p.action_payload->>'status' = 'Preparing'
                              AND p.received_at <= o.received_at
                              AND NOT EXISTS (
                                  SELECT 1 FROM ocpp_events p2
                                   WHERE p2.asset_id = s.station_id
                                     AND p2.action = 'StatusNotification'
                                     AND p2.connector_id = s.connector_id
                                     AND p2.received_at > p.received_at
                                     AND p2.received_at <= o.received_at
                                     AND p2.action_payload->>'status' <> 'Preparing'))
                     ORDER BY ABS(EXTRACT(EPOCH FROM (o.received_at - s.start_utc))) ASC
                     LIMIT 1) AS id_tag,
                    -- v3.3: the idTag that actually STARTED the transaction, for the
                    -- Authentication column. Mirrors export.py's with_auth exactly —
                    -- charger-stamped payload timestamp for the nearest-match (an
                    -- offline unit replays StartTransaction minutes late), with the
                    -- coarse received_at bound left in to keep the index usable.
                    (SELECT o.action_payload->>'idTag' FROM ocpp_events o
                     WHERE o.asset_id = s.station_id
                       AND o.action   = 'StartTransaction'
                       AND (o.action_payload->>'connectorId')::int = s.connector_id
                       AND o.received_at BETWEEN s.start_utc - INTERVAL '6 hours'
                                             AND s.start_utc + INTERVAL '24 hours'
                       AND (o.action_payload->>'timestamp')::timestamptz
                             BETWEEN s.start_utc - INTERVAL '6 hours'
                                 AND s.start_utc + INTERVAL '5 minutes'
                     ORDER BY ABS(EXTRACT(EPOCH FROM (
                         (o.action_payload->>'timestamp')::timestamptz - s.start_utc))) ASC
                     LIMIT 1) AS auth_tag,
                    (SELECT ep.price_per_kwh FROM evse_pricing ep
                     WHERE ep.station_id = s.station_id
                       AND ep.effective_start <= s.start_utc
                       AND (ep.effective_end IS NULL OR ep.effective_end > s.start_utc)
                     ORDER BY ep.effective_start DESC LIMIT 1) AS price_per_kwh,
                    (SELECT ep.connection_fee FROM evse_pricing ep
                     WHERE ep.station_id = s.station_id
                       AND ep.effective_start <= s.start_utc
                       AND (ep.effective_end IS NULL OR ep.effective_end > s.start_utc)
                     ORDER BY ep.effective_start DESC LIMIT 1) AS connection_fee,
                    -- v3.3: committed CCR amount + normalised card entry mode
                    -- for card-initiated sessions. Reads the cross-vendor
                    -- card_transactions view, so Payter (ARG, Cooper Landing)
                    -- and Nayax (Delta, Glennallen) arrive in one shape and the
                    -- dashboard never learns which terminal brand a charger has.
                    card.committed_cents                       AS card_amount_cents,
                    card.entry_mode                            AS card_entry_mode,
                    -- Proof of a card start, independent of idTag shape (see
                    -- _auth_method for why the shape rules aren't enough). Kept
                    -- separate from the amount: a committed tap can settle at
                    -- NULL/0 (a session that drew no energy) and must still
                    -- classify as CC.
                    (card.vendor IS NOT NULL)                  AS card_matched
                FROM sessions s
                LEFT JOIN LATERAL (
                    SELECT ct.vendor, ct.committed_cents, ct.entry_mode
                    FROM card_transactions ct
                    WHERE ct.station_id = s.station_id
                      AND ct.connector_id IS NOT DISTINCT FROM s.connector_id
                      AND ct.transaction_id = s.transaction_id::text
                    LIMIT 1
                ) card ON true
            ),
            real_rows AS (
                SELECT
                    'session'::text                     AS kind,
                    station_id,
                    connector_id,
                    transaction_id::text                AS transaction_id,
                    start_utc,
                    end_utc,
                    max_power_w::numeric                AS max_power_w,
                    /*ENERGY_COALESCE*/,
                    soc_start::numeric                  AS soc_start,
                    soc_first_nonzero::numeric          AS soc_first_nonzero,
                    soc_end::numeric                    AS soc_end,
                    soc_last_nonzero::numeric           AS soc_last_nonzero,
                    id_tag,
                    auth_tag,
                    price_per_kwh::numeric              AS price_per_kwh,
                    connection_fee::numeric             AS connection_fee,
                    card_amount_cents::numeric          AS card_amount_cents,
                    card_entry_mode,
                    card_matched,
                    -- v3.5: a card paid, but an app credential was stranded
                    -- moments earlier. See DOUBLE_CHARGE_SQL.
                    /*DOUBLE_CHARGE*/                   AS double_charge_suspect
                FROM with_auth
                WHERE ($3::timestamptz IS NULL OR start_utc <= $3)  -- v3.2: START in range
            ),
            -- ── Failed start attempts ──────────────────────────────────────────
            -- A plug-in (StatusNotification "Preparing") where the driver
            -- presented a real credential (a non-VID token Authorize via CC reader
            -- or app) but the session never reached "Charging" before the connector
            -- cleared (capped at 2 h).  These never mint a transaction_id, so they
            -- never appear in meter_values_parsed and are otherwise invisible.
            -- Excluded by design: AutoCharge VID:* probes (Blocked when AutoCharge
            -- isn't configured) and plug-and-unplug blips with no Authorize at all.
            -- EXCEPTION: an episode that ends in the charger's own auth-timeout
            -- fault (Tritium vendor code 824, Alpitronic 23) counts even when the
            -- only Authorize was a rejected AutoCharge VID — a real driver plugged
            -- in and never got authorized, which is exactly the failed attempt the
            -- operator wants to see.
            -- EXCEPTION 2 (v3.6): an episode where the charger opened a transaction
            -- that then metered nothing at all.  See the zero-energy guard in
            -- charge_sig and branch (2d) below.
            sn AS (
                SELECT
                    asset_id,
                    connector_id,
                    received_at,
                    action_payload->>'status'                        AS status,
                    LAG(action_payload->>'status') OVER (
                        PARTITION BY asset_id, connector_id ORDER BY received_at
                    )                                                AS prev_status
                FROM ocpp_events
                WHERE asset_id = ANY($1::text[])
                  AND action = 'StatusNotification'
                  AND action_payload->>'status' IS NOT NULL
            ),
            charge_sig AS (   -- any signal that a transaction actually began
                                -- AND delivered something
                SELECT e.asset_id, e.connector_id, e.received_at
                FROM ocpp_events e
                WHERE e.asset_id = ANY($1::text[])
                  AND ( (e.action = 'StatusNotification'
                         AND e.action_payload->>'status' = 'Charging')
                        OR ( e.action = 'StartTransaction'
                             AND NOT EXISTS (/*ZERO_ENERGY_TX*/) ) )
            ),
            avail AS (        -- connector cleared / unplugged
                SELECT asset_id, connector_id, received_at
                FROM ocpp_events
                WHERE asset_id = ANY($1::text[])
                  AND action = 'StatusNotification'
                  AND action_payload->>'status' = 'Available'
            ),
            attempts AS (
                SELECT
                    s.asset_id    AS station_id,
                    s.connector_id,
                    s.received_at AS attempt_at,
                    -- End of this plug-in episode: the next time the connector
                    -- clears to Available, capped at 2 h if that event is missing.
                    LEAST(
                        s.received_at + INTERVAL '2 hours',
                        COALESCE((SELECT MIN(a.received_at) FROM avail a
                                  WHERE a.asset_id = s.asset_id
                                    AND a.connector_id IS NOT DISTINCT FROM s.connector_id
                                    AND a.received_at > s.received_at),
                                 'infinity'::timestamptz)
                    )                                            AS episode_end
                FROM sn s
                WHERE s.status = 'Preparing'
                  -- First Preparing of an episode.  A Preparing directly after
                  -- Faulted (no Available/Finishing between) is the same plug-in
                  -- continuing — Tritium flaps Preparing↔Faulted during a fault —
                  -- so it must not start a new attempt row.
                  AND s.prev_status IS DISTINCT FROM 'Preparing'
                  AND s.prev_status IS DISTINCT FROM 'Faulted'
                  AND ($2::timestamptz IS NULL OR s.received_at >= $2)
                  AND ($3::timestamptz IS NULL OR s.received_at <= $3)
            ),
            attempts_filtered AS (
                SELECT a.station_id, a.connector_id, a.attempt_at, a.episode_end
                FROM attempts a
                WHERE
                    -- (1) never reached Charging within the episode
                    NOT EXISTS (
                        SELECT 1 FROM charge_sig c
                        WHERE c.asset_id = a.station_id
                          AND c.connector_id IS NOT DISTINCT FROM a.connector_id
                          AND c.received_at > a.attempt_at
                          AND c.received_at < a.episode_end
                    )
                    -- (2) but a real user attempt is evidenced by either:
                    AND (
                        -- (2a) a real, non-AutoCharge credential was presented:
                        -- a token (non-VID) Authorize — the CC/app-reader stall the
                        -- operator cares about.  VID:* Authorizes are handled by (2e)
                        -- below; a plug-in that produced no Authorize at all
                        -- (plug-and-unplug) is still ignored.  Authorize messages
                        -- carry no connector_id, so match on station + time window.
                        EXISTS (
                            SELECT 1 FROM ocpp_events az
                            WHERE az.asset_id = a.station_id
                              AND az.action = 'Authorize'
                              AND az.action_payload->>'idTag' NOT LIKE 'VID:%'
                              AND az.received_at BETWEEN a.attempt_at - INTERVAL '30 seconds'
                                                     AND a.episode_end
                        )
                        -- (2b) or the charger raised an auth-timeout fault: no accepted
                        -- authorization arrived within its timeout after plug-in.
                        -- Tritium vendor code 824; Alpitronic 23 "Authorization Timeout"
                        -- (Tritium's own 23 is "Not used", so no cross-vendor collision).
                        -- These stand alone — the charger itself asserts a driver
                        -- plugged in and never got authorized, even with no Authorize.
                        OR EXISTS (
                            SELECT 1 FROM ocpp_events ft
                            WHERE ft.asset_id = a.station_id
                              AND ft.action = 'StatusNotification'
                              AND ft.action_payload->>'vendorErrorCode' IN ('824', '23')
                              AND ft.connector_id IS NOT DISTINCT FROM a.connector_id
                              AND ft.received_at BETWEEN a.attempt_at AND a.episode_end
                        )
                        -- (2c) or an authorization exchange began (ANY Authorize,
                        -- VID probes included) and the connector Faulted before
                        -- charging.  Tritium has a whole family of pre-charge
                        -- failure codes (62/63/74/823/869/873/875...) so no code
                        -- allowlist: any fault after a handshake started marks a
                        -- real driver who failed to start.  The Authorize
                        -- requirement keeps out driverless hardware faults and
                        -- plug/unplug blips.
                        OR (
                            EXISTS (
                                SELECT 1 FROM ocpp_events ft
                                WHERE ft.asset_id = a.station_id
                                  AND ft.action = 'StatusNotification'
                                  AND ft.action_payload->>'status' = 'Faulted'
                                  AND ft.connector_id IS NOT DISTINCT FROM a.connector_id
                                  AND ft.received_at BETWEEN a.attempt_at AND a.episode_end
                            )
                            AND EXISTS (
                                SELECT 1 FROM ocpp_events az2
                                WHERE az2.asset_id = a.station_id
                                  AND az2.action = 'Authorize'
                                  AND az2.received_at BETWEEN a.attempt_at - INTERVAL '30 seconds'
                                                          AND a.episode_end
                            )
                        )
                        -- (2d) v3.6: or the charger opened a transaction during
                        -- this episode.  Condition (1) has already established
                        -- that nothing in the episode counted as charging, and
                        -- charge_sig now discounts a StartTransaction whose
                        -- register never moved (ZERO_ENERGY_TX_SQL) — so reaching
                        -- here means the unit accepted a credential, opened a
                        -- transaction and metered nothing.  That is the charger's
                        -- own account of a driver who tried and got no energy, so
                        -- it stands alone: no Authorize shape test, no fault code
                        -- allowlist.  Covers the Alpitronic V2G/EVCommunicationError
                        -- family, which never raises status 'Faulted' (it stays
                        -- 'Preparing') and so slips past (2b) and (2c).
                        OR EXISTS (
                            SELECT 1 FROM ocpp_events sx
                            WHERE sx.asset_id = a.station_id
                              AND sx.action = 'StartTransaction'
                              AND sx.connector_id IS NOT DISTINCT FROM a.connector_id
                              AND sx.received_at BETWEEN a.attempt_at AND a.episode_end
                        )
                        -- (2e) or an AutoCharge VID was presented and no
                        -- transaction was ever opened.  (2d) covers the vehicle
                        -- that authorised and drew nothing; this covers the one
                        -- that never got that far, which is what an unenrolled
                        -- car looks like.  Condition (1) has already excluded
                        -- every episode that reached charging, so a VID Authorize
                        -- still standing here is a driver who asked and was
                        -- refused.
                        --
                        -- Autel is why this cannot lean on a fault the way (2b)
                        -- and (2c) do.  On 11 Sep 2026 a Cybertruck was rejected
                        -- twice at Glennallen (Authorize -> Invalid, 12:47 and
                        -- 12:50 AK); the MaxiChargerDC simply went Preparing ->
                        -- Available with no Faulted status and no vendorErrorCode,
                        -- so both attempts fell through every branch and the
                        -- operator saw nothing until the VID was enrolled and the
                        -- third try took.  Treating a VID Authorize as a mere
                        -- "probe" only ever held for chargers that announce a
                        -- refusal by faulting.
                        --
                        -- LIMIT OF THE EVIDENCE: ocpp_events stores CALL requests
                        -- only, never CALLRESULTs, so the Invalid/Accepted verdict
                        -- on an Authorize is not in this database at all (it lives
                        -- only in LynkWell's log export).  A refused credential and
                        -- a driver who plugged in and thought better of it are
                        -- therefore indistinguishable here; both surface as a
                        -- failed start, which is the safer of the two errors.
                        OR EXISTS (
                            SELECT 1 FROM ocpp_events az
                            WHERE az.asset_id = a.station_id
                              AND az.action = 'Authorize'
                              AND az.action_payload->>'idTag' LIKE 'VID:%'
                              AND az.received_at BETWEEN a.attempt_at - INTERVAL '30 seconds'
                                                     AND a.episode_end
                        )
                    )
            ),
            failed_rows AS (
                SELECT
                    'failed'::text                      AS kind,
                    f.station_id,
                    f.connector_id,
                    'attempt:' || f.station_id || ':' || COALESCE(f.connector_id, 0)
                               || ':' || EXTRACT(EPOCH FROM f.attempt_at)::bigint::text AS transaction_id,
                    f.attempt_at                        AS start_utc,
                    NULL::timestamptz                   AS end_utc,
                    NULL::numeric                       AS max_power_w,
                    NULL::numeric                       AS energy_wh_delta,
                    NULL::numeric                       AS soc_start,
                    NULL::numeric                       AS soc_first_nonzero,
                    NULL::numeric                       AS soc_end,
                    NULL::numeric                       AS soc_last_nonzero,
                    -- Surface the credential that failed (e.g. the rejected
                    -- AutoCharge VID) so repeat offenders are visible in the table.
                    -- v3.6: fall back to the StartTransaction idTag. An Alpitronic
                    -- with AutoCharge enrolled sends no Authorize at all and stamps
                    -- the VID straight onto StartTransaction (see _vid_tag), so a
                    -- (2d) zero-energy attempt would otherwise name no credential.
                    COALESCE(
                        (SELECT az.action_payload->>'idTag' FROM ocpp_events az
                          WHERE az.asset_id = f.station_id
                            AND az.action = 'Authorize'
                            AND az.received_at BETWEEN f.attempt_at - INTERVAL '30 seconds'
                                                   AND f.episode_end
                          ORDER BY az.received_at ASC LIMIT 1),
                        (SELECT sx.action_payload->>'idTag' FROM ocpp_events sx
                          WHERE sx.asset_id = f.station_id
                            AND sx.action = 'StartTransaction'
                            AND sx.connector_id IS NOT DISTINCT FROM f.connector_id
                            AND sx.received_at BETWEEN f.attempt_at AND f.episode_end
                          ORDER BY sx.received_at ASC LIMIT 1)
                    )                                   AS id_tag,
                    NULL::text                          AS auth_tag,
                    NULL::numeric                       AS price_per_kwh,
                    NULL::numeric                       AS connection_fee,
                    NULL::numeric                       AS card_amount_cents,
                    NULL::text                          AS card_entry_mode,
                    FALSE                               AS card_matched,
                    FALSE                               AS double_charge_suspect
                FROM attempts_filtered f
            ),
            unioned AS (
                SELECT * FROM real_rows
                UNION ALL
                SELECT * FROM failed_rows
            )
            SELECT *,
                   COUNT(*) OVER()                                              AS total_count,
                   COUNT(*) FILTER (WHERE kind = 'session') OVER()              AS completed_count,
                   SUM(energy_wh_delta) FILTER (WHERE kind = 'session') OVER()  AS agg_energy_wh,
                   -- v3.3: prefer the terminal-committed amount per session,
                   -- falling back to the price-sheet estimate.
                   SUM(
                       CASE WHEN kind = 'session'
                            THEN COALESCE(
                                card_amount_cents / 100.0,
                                CASE WHEN price_per_kwh IS NOT NULL OR connection_fee IS NOT NULL
                                     THEN COALESCE(connection_fee, 0)
                                          + (energy_wh_delta / 1000.0) * COALESCE(price_per_kwh, 0)
                                     ELSE 0 END)
                            ELSE 0 END
                   ) OVER()                                                     AS agg_revenue,
                   AVG(EXTRACT(EPOCH FROM (end_utc - start_utc)) / 60.0)
                       FILTER (WHERE kind = 'session') OVER()                   AS agg_avg_duration_min
            FROM unioned
            ORDER BY start_utc DESC NULLS LAST                  -- v3.2: newest start on top
            LIMIT $4 OFFSET $5
            """),
            allowed,
            start_utc,
            end_utc,
            page_size,
            offset,
        )

    total           = int(rows[0]["total_count"])                     if rows else 0
    completed_count = int(rows[0]["completed_count"])                 if rows else 0
    failed_count    = total - completed_count
    review_count    = sum(1 for r in rows if r["double_charge_suspect"])
    total_energy  = float(rows[0]["agg_energy_wh"] or 0) / 1000.0    if rows else 0.0
    total_revenue = float(rows[0]["agg_revenue"]   or 0)              if rows else 0.0
    avg_dur_raw   = rows[0]["agg_avg_duration_min"]                    if rows else None
    avg_duration  = float(avg_dur_raw) if avg_dur_raw is not None else None

    sessions: list[ChargingSession] = []
    for r in rows:
        sid        = r["station_id"]
        conn_id    = r["connector_id"]
        tx_id      = str(r["transaction_id"])
        start_dt   = r["start_utc"]
        end_dt     = r["end_utc"]
        dur_min    = (end_dt - start_dt).total_seconds() / 60.0 if start_dt and end_dt else None
        energy_wh  = r["energy_wh_delta"]
        energy_kwh = round(float(energy_wh) / 1000.0, 3) if energy_wh else None
        max_kw     = round(float(r["max_power_w"]) / 1000.0, 2) if r["max_power_w"] else None

        p_kwh   = float(r["price_per_kwh"] or 0)
        c_fee   = float(r["connection_fee"] or 0)
        est_rev = math.floor((c_fee + (energy_kwh or 0) * p_kwh) * 100) / 100 if (p_kwh or c_fee) else None

        card_cents = r["card_amount_cents"]
        actual_rev = round(float(card_cents) / 100.0, 2) if card_cents is not None else None

        soc_start_pct, soc_end_pct = _resolve_soc(
            r["soc_start"], r["soc_first_nonzero"], r["soc_end"], r["soc_last_nonzero"]
        )

        status = "completed" if r["kind"] == "session" else "failed_start"

        # Failed starts get no method (matches the export, which blanks column N
        # for them). v3.6: a (2d) zero-energy attempt DID authenticate — the
        # charger opened a transaction — so this is now a display choice rather
        # than a statement of fact. The credential still shows in id_tag, which
        # is the part an operator chasing a repeat failure actually needs; the
        # method column stays blank so it keeps meaning "this driver paid by X".
        auth_method = (
            None if status == "failed_start"
            else (_auth_method(r["auth_tag"] or "", bool(r["card_matched"])) or None)
        )

        sessions.append(
            ChargingSession(
                status          = status,
                transaction_id  = tx_id,
                station_id      = sid,
                evse_name       = display_name(sid),
                location        = location_label(sid),
                connector_id    = conn_id,
                connector_type  = connector_type_for(sid, conn_id or 0, start_dt),
                start_dt        = _fmt_ak(start_dt),
                end_dt          = _fmt_ak(end_dt),
                duration_min    = round(dur_min, 1) if dur_min is not None else None,
                max_power_kw    = max_kw,
                energy_kwh      = energy_kwh,
                soc_start       = soc_start_pct,
                soc_end         = soc_end_pct,
                id_tag          = _vid_tag(r["auth_tag"] or "", r["id_tag"]),
                est_revenue_usd = est_rev,
                actual_revenue_usd = actual_rev,
                card_entry_mode = r["card_entry_mode"],
                auth_method     = auth_method,
                double_charge_suspect = bool(r["double_charge_suspect"]),
            )
        )

    return SessionsResponse(
        sessions=sessions,
        total=total,
        completed_count=completed_count,
        failed_count=failed_count,
        review_count=review_count,
        page=page,
        page_size=page_size,
        total_energy_kwh=round(total_energy, 3),
        total_revenue_usd=math.floor(total_revenue * 100) / 100,
        avg_duration_min=round(avg_duration, 1) if avg_duration is not None else None,
    )


# ── Session Detail — time-series meter values for a single transaction ─────────

@router.get("/detail", response_model=SessionDetailResponse)
async def get_session_detail(
    user: CurrentUser,
    station_id:     str = Query(...),
    transaction_id: str = Query(...),
    connector_id:   int | None = Query(None),
):
    # Auth: verify this station is allowed for the user
    all_ids  = get_all_station_ids()
    allowed  = filter_evse_ids(all_ids, user.allowed_evse_ids)
    if station_id not in allowed:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="EVSE not permitted")

    async with acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                ts,
                power_w,
                power_offered_w,
                current_offered_a,
                energy_wh,
                soc,
                voltage_v
            FROM meter_values_parsed
            WHERE station_id = $1
              AND transaction_id::text = $2
              AND ($3::int IS NULL OR connector_id = $3)
            ORDER BY ts ASC
            """,
            station_id,
            transaction_id,
            connector_id,
        )

    if not rows:
        return SessionDetailResponse(
            station_id=station_id,
            evse_name=display_name(station_id),
            transaction_id=transaction_id,
            start_dt="—",
            end_dt=None,
            points=[],
        )

    # Baseline energy to session start
    first_energy_wh = next(
        (float(r["energy_wh"]) for r in rows if r["energy_wh"] is not None), None
    )

    # SoC normalisation: some chargers send 0–1 fraction instead of 0–100.
    # Use the max value across ALL rows to distinguish: if max ≤ 1.0 it's fractional.
    soc_max = max((float(r["soc"]) for r in rows if r["soc"] is not None), default=None)
    soc_scale = 100.0 if (soc_max is not None and soc_max <= 1.0) else 1.0

    points: list[MeterValuePoint] = []
    for r in rows:
        ts = r["ts"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ts_ak_str = ts.astimezone(_AK).strftime("%Y-%m-%d %H:%M")

        raw_soc = r["soc"]
        soc_pct = round(float(raw_soc) * soc_scale, 1) if raw_soc is not None else None

        ew = r["energy_wh"]
        e_delta = (
            round((float(ew) - first_energy_wh) / 1000.0, 3)
            if ew is not None and first_energy_wh is not None
            else None
        )

        pw = r["power_w"]
        poffered = r["power_offered_w"]

        points.append(
            MeterValuePoint(
                ts_ak=ts_ak_str,
                power_kw=round(float(pw) / 1000.0, 2) if pw is not None else None,
                power_offered_kw=round(float(poffered) / 1000.0, 2) if poffered is not None else None,
                current_offered_a=round(float(r["current_offered_a"]), 1) if r["current_offered_a"] is not None else None,
                soc=soc_pct,
                energy_kwh_delta=e_delta,
                voltage_v=round(float(r["voltage_v"]), 0) if r["voltage_v"] is not None else None,
            )
        )

    start_dt = _fmt_ak(rows[0]["ts"])
    end_dt   = _fmt_ak(rows[-1]["ts"])

    return SessionDetailResponse(
        station_id=station_id,
        evse_name=display_name(station_id),
        transaction_id=transaction_id,
        start_dt=start_dt,
        end_dt=end_dt,
        points=points,
    )
