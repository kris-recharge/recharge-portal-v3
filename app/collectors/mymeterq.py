"""CEA mymeterQ collector (myaccount.chugachelectric.com).

Auth flow
---------
mymeterQ uses server-side session cookies.  There is no bearer token —
the session is established via a standard form POST to /Account/Login and
maintained by the .AspNet.ApplicationCookie header that the server sets.

httpx.AsyncClient with follow_redirects=True handles this automatically
when we create it with cookies persisted (default for AsyncClient).

Multi-account
-------------
CEA groups accounts under one login.  Switching between accounts is done
with GET /Dashboard/SetMeterGroup?meterGroupId=<id> which updates the
server-side session.  Each utility_accounts row must supply meter_group_id.

Data endpoint
-------------
GET /Dashboard/ChartData?unixTimeStart=<ms>&unixTimeEnd=<ms>

Response shape (relevant part):
{
  "Data": {
    "series": [
      …,
      {                                  ← series with actual kWh data
        "name": "Meter #354273415 (…)",
        "type": "column",
        "visible": true,
        "data": [
          {
            "x":  1773702000000,   // interval start (unix ms)
            "y":  0.24,            // kWh
            "xs": 1773702000000,   // same as x
            "xe": 1773705599999,   // interval end
            "v":  false            // is_estimated flag
          },
          …
        ],
        "tooltip": { "valueSuffix": " kWh" }
      }
    ],
    "usageType": "Consumption"
  }
}

Granularity is 60 minutes (one row per hour) for a 1-day query window.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx

from .base import AbstractCollector

logger = logging.getLogger("rca.collectors.mymeterq")

_BASE      = "https://myaccount.chugachelectric.com"
_LOGIN_URL = f"{_BASE}/User/LogIn"
_SET_GROUP = f"{_BASE}/Dashboard/SetMeterGroup"
_DATA_URL  = f"{_BASE}/Dashboard/ChartData"

# One-hour query windows; CEA returns hourly data for a 1-day range.
_QUERY_HOURS = 23           # 23-hour window matches CEA's "1d" quick-pick
_GRAN_MIN    = 60


class MyMeterQCollector(AbstractCollector):
    """Collects hourly kWh data for one CEA account from mymeterQ."""

    utility = "cea"

    # ── Public interface ──────────────────────────────────────────────────────

    async def collect(self, pool: asyncpg.Pool, days_back: int = 2) -> int:
        acct = self.account
        self.log.info(
            "mymeterQ collect start: account=%s site='%s'",
            acct["account_number"], acct.get("display_name", ""),
        )

        try:
            async with httpx.AsyncClient(
                base_url=_BASE,
                follow_redirects=True,
                timeout=30,
            ) as client:
                await self._login(client)
                await self._set_account(client)
                rows = await self._fetch_usage(client, days_back)

            n = await self.upsert_usage(pool, rows)
            await self.mark_collected(pool, error=None)
            self.log.info("mymeterQ collect done: %d rows upserted", n)
            return n

        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            self.log.error("mymeterQ collect failed: %s", msg)
            await self.mark_collected(pool, error=msg)
            return 0

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _login(self, client: httpx.AsyncClient) -> None:
        """Obtain a session cookie via the mymeterQ login form."""
        # GET the login page first to capture the CSRF __RequestVerificationToken
        resp = await client.get("/User/LogIn")
        resp.raise_for_status()

        token = self._extract_csrf(resp.text)
        if not token:
            self.log.warning(
                "mymeterQ: could not find CSRF token on login page — "
                "proceeding without it (may fail)"
            )

        payload = {
            "UserName": self.credentials["username"],
            "Password": self.credentials["password"],
        }
        if token:
            payload["__RequestVerificationToken"] = token

        resp = await client.post("/User/LogIn", data=payload)
        resp.raise_for_status()

        # A successful login redirects to /Dashboard; check we're not still on /LogIn
        if "/User/LogIn" in str(resp.url):
            raise RuntimeError(
                "mymeterQ login failed — still on login page. "
                "Check credentials for CEA account."
            )
        self.log.debug("mymeterQ login OK")

    async def _set_account(self, client: httpx.AsyncClient) -> None:
        """Switch the server-side session to the correct meter group."""
        meter_group_id = self.account.get("meter_group_id")
        if not meter_group_id:
            self.log.debug("mymeterQ: no meter_group_id — using default session account")
            return

        resp = await client.get(
            "/Dashboard/SetMeterGroup",
            params={"meterGroupId": meter_group_id},
        )
        resp.raise_for_status()
        self.log.debug("mymeterQ: switched to meterGroupId=%s", meter_group_id)

    async def _fetch_usage(
        self, client: httpx.AsyncClient, days_back: int
    ) -> list[dict]:
        """Request ChartData for each day window and aggregate results."""
        all_rows: list[dict] = []

        # Build list of (start_ms, end_ms) windows — one per day going back
        now_utc   = datetime.now(tz=timezone.utc)
        # Align to midnight UTC so windows don't drift
        today_midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

        for day_offset in range(days_back + 1):
            window_end   = today_midnight - timedelta(days=day_offset)
            window_start = window_end - timedelta(hours=_QUERY_HOURS)

            start_ms = int(window_start.timestamp() * 1000)
            end_ms   = int(window_end.timestamp() * 1000)

            resp = await client.get(
                "/Dashboard/ChartData",
                params={"unixTimeStart": start_ms, "unixTimeEnd": end_ms},
            )
            resp.raise_for_status()

            raw_points = self._parse_response(resp.json())
            self.log.debug(
                "mymeterQ ChartData window %s–%s: %d points",
                window_start.date(), window_end.date(), len(raw_points),
            )
            all_rows.extend(self._normalise(raw_points))

        # Deduplicate on interval_start (upsert handles it in DB too)
        seen: set[datetime] = set()
        deduped = []
        for r in all_rows:
            if r["interval_start"] not in seen:
                seen.add(r["interval_start"])
                deduped.append(r)

        return deduped

    # ── Response parsing ──────────────────────────────────────────────────────

    def _parse_response(self, data: dict) -> list[dict]:
        """Extract data points from the CEA ChartData JSON.

        Returns list of raw point dicts: {x, y, xs, xe, v}.
        Only returns points from the first visible column series with kWh data.
        """
        payload = data.get("Data") or data
        series  = payload.get("series") or []

        for s in series:
            # Skip non-column or non-visible series
            if s.get("type") != "column":
                continue
            if not s.get("visible", True):
                continue
            pts = s.get("data") or []
            if not pts:
                continue
            # Make sure points look like kWh data (dicts with x/y keys)
            if isinstance(pts[0], dict) and "y" in pts[0]:
                return [p for p in pts if p.get("y") is not None]
            # Fallback: [ms, kwh] array format
            if isinstance(pts[0], (list, tuple)):
                return [{"x": p[0], "y": p[1], "xs": p[0], "xe": None, "v": False}
                        for p in pts if len(p) >= 2 and p[1] is not None]

        self.log.warning("mymeterQ: no column series with kWh data found")
        return []

    def _normalise(self, raw: list[dict]) -> list[dict]:
        """Convert raw point dicts to utility_usage row dicts."""
        acct     = self.account
        meter_id = None
        rows     = []

        for p in raw:
            kwh = p.get("y")
            if kwh is None or kwh < 0:
                continue

            xs = p.get("xs") or p.get("x")
            xe = p.get("xe")

            start = datetime.fromtimestamp(xs / 1000, tz=timezone.utc)
            end   = (
                datetime.fromtimestamp(xe / 1000, tz=timezone.utc)
                if xe
                else start + timedelta(minutes=_GRAN_MIN)
            )

            rows.append({
                "utility":         self.utility,
                "account_number":  acct["account_number"],
                "meter_id":        meter_id,
                "interval_start":  start,
                "interval_end":    end,
                "kwh":             float(kwh),
                "is_estimated":    bool(p.get("v", False)),
                "granularity_min": _GRAN_MIN,
            })
        return rows

    # ── Utility ───────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_csrf(html: str) -> str | None:
        """Extract the ASP.NET request verification token from HTML."""
        match = re.search(
            r'<input[^>]+name="__RequestVerificationToken"[^>]+value="([^"]+)"',
            html,
            re.IGNORECASE,
        )
        return match.group(1) if match else None
