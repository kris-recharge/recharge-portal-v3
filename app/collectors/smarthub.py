"""NISC SmartHub collector — covers GVEA, CVEA, and any future SmartHub utility.

Auth flow
---------
1. POST /services/oauth/auth  →  { authorizationToken, expiration, … }
2. Every subsequent call:     →  header  authorizationToken: <token>
   (token TTL is ~5 min; since we only collect once/day we just re-login each run)

Data endpoint
-------------
POST /services/secured/utility-usage/poll
Body: { accountNumber, serviceLocationNumber, customerNumber,
        systemOfRecord, industry, usageType, graphType,
        timeFrame, startDate, endDate }

timeFrame options observed:
  "HOURLY"  — returns 5-minute intervals (GVEA, AMI meters)
  "DAILY"   — returns daily totals       (CVEA, conventional meters)
  "MONTHLY" — returns monthly totals

Response shape (relevant part):
{
  "usageData": [
      { "reads": [ [unix_ms, kwh], … ] }   ← older SmartHub
  ]
  OR
  "series": [
      { "data": [ [unix_ms, kwh], … ] }    ← newer SmartHub
  ]
}

The collector tries both shapes and normalises to (interval_start_ms, kwh).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx

from .base import AbstractCollector

logger = logging.getLogger("rca.collectors.smarthub")

# Per-utility base URLs — add new SmartHub utilities here, no code changes needed.
SMARTHUB_URLS: dict[str, str] = {
    "gvea": "https://gvea.smarthub.coop",
    "cvea": "https://cvea.smarthub.coop",
    # Future: "mea": "https://mea.smarthub.coop",
}

# Best timeFrame to request per utility (determined by meter capability).
# If a utility isn't listed, we fall back to "DAILY".
PREFERRED_TIMEFRAME: dict[str, str] = {
    "gvea": "HOURLY",   # AMI meter — 5-min intervals
    "cvea": "DAILY",    # Conventional meter — daily reads only
}

# Granularity in minutes per timeFrame
GRANULARITY: dict[str, int] = {
    "HOURLY":  5,    # SmartHub "HOURLY" actually delivers 5-min intervals
    "DAILY":   1440,
    "MONTHLY": 43200,
}


class SmartHubCollector(AbstractCollector):
    """Collects kWh data for one account from a NISC SmartHub portal."""

    utility: str  # set dynamically in __init__

    def __init__(self, account: dict, credentials: dict) -> None:
        self.utility = account["utility"]   # e.g. "gvea" or "cvea"
        super().__init__(account, credentials)

        base = SMARTHUB_URLS.get(self.utility)
        if not base:
            raise ValueError(
                f"No SmartHub URL configured for utility '{self.utility}'. "
                f"Add it to SMARTHUB_URLS in smarthub.py."
            )
        self._base = base.rstrip("/")

    # ── Public interface ──────────────────────────────────────────────────────

    async def collect(self, pool: asyncpg.Pool, days_back: int = 2) -> int:
        acct = self.account
        self.log.info(
            "SmartHub collect start: utility=%s account=%s site='%s'",
            self.utility, acct["account_number"], acct.get("display_name", ""),
        )

        try:
            token = await self._login()
            rows  = await self._fetch_usage(token, days_back)
            n     = await self.upsert_usage(pool, rows)
            await self.mark_collected(pool, error=None)
            self.log.info("SmartHub collect done: %d rows upserted", n)
            return n

        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            self.log.error("SmartHub collect failed: %s", msg)
            await self.mark_collected(pool, error=msg)
            return 0

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _login(self) -> str:
        """POST to /services/oauth/auth/v2 and return the authorizationToken.

        SmartHub upgraded from a JSON endpoint (/v1) to a form-encoded endpoint (/v2).
        Field name changed from 'username' to 'userId'.
        Content-Type must be application/x-www-form-urlencoded.
        Response still contains authorizationToken.
        """
        url = f"{self._base}/services/oauth/auth/v2"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                data={
                    "userId":   self.credentials["username"],
                    "password": self.credentials["password"],
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept":       "application/json, text/plain, */*",
                },
            )

        if resp.status_code != 200:
            raise RuntimeError(
                f"SmartHub login failed for {self.utility}: "
                f"HTTP {resp.status_code} — {resp.text[:200]}"
            )

        data = resp.json()
        token = data.get("authorizationToken") or data.get("token")
        if not token:
            raise RuntimeError(
                f"SmartHub login response missing authorizationToken: {data}"
            )
        self.log.debug("SmartHub login OK for %s", self.utility)
        return token

    async def _fetch_usage(self, token: str, days_back: int) -> list[dict]:
        """Call the poll endpoint and return normalised usage rows."""
        acct       = self.account
        time_frame = PREFERRED_TIMEFRAME.get(self.utility, "DAILY")
        gran_min   = GRANULARITY[time_frame]

        # Build date window
        today     = datetime.now(tz=timezone.utc).date()
        end_date  = today
        start_date = today - timedelta(days=days_back)

        body: dict[str, Any] = {
            "accountNumber":    acct["account_number"],
            "systemOfRecord":   acct.get("system_of_record") or "UTILITY",
            "industry":         "ELECTRIC",
            "usageType":        "KWH",
            "graphType":        "USAGE",
            "timeFrame":        time_frame,
            "startDate":        str(start_date),
            "endDate":          str(end_date),
            "includeInactive":  False,
        }

        # Optional SmartHub-specific fields — only include if purely numeric
        sln = acct.get("service_location_number") or ""
        if sln.strip().isdigit():
            body["serviceLocationNumber"] = sln.strip()
        cust = acct.get("customer_number") or ""
        if cust.strip():
            body["customerNumber"] = cust.strip()

        url     = f"{self._base}/services/secured/utility-usage/poll"
        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {token}",
        }

        self.log.info("SmartHub poll body: %s", body)
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=body, headers=headers)

        if resp.status_code != 200:
            raise RuntimeError(
                f"SmartHub poll failed: HTTP {resp.status_code} — {resp.text[:300]}"
            )

        raw_points = self._parse_response(resp.json())
        self.log.debug(
            "SmartHub poll: %d raw points for %s/%s (%s)",
            len(raw_points), self.utility, acct["account_number"], time_frame,
        )

        return self._normalise(raw_points, gran_min)

    def _parse_response(self, data: dict) -> list[tuple[int, float]]:
        """Extract (unix_ms, kwh) pairs from either SmartHub response shape."""

        # Shape 1: older — data["usageData"][0]["reads"] = [[ms, kwh], …]
        usage_data = data.get("usageData") or data.get("UsageData")
        if usage_data and isinstance(usage_data, list):
            reads = usage_data[0].get("reads") or usage_data[0].get("Reads") or []
            return [(int(r[0]), float(r[1])) for r in reads if len(r) >= 2 and r[1] is not None]

        # Shape 2: newer — data["series"][0]["data"] = [[ms, kwh], …]
        series = data.get("series") or data.get("Series")
        if series and isinstance(series, list):
            pts = series[0].get("data") or []
            result = []
            for p in pts:
                if isinstance(p, (list, tuple)) and len(p) >= 2 and p[1] is not None:
                    result.append((int(p[0]), float(p[1])))
                elif isinstance(p, dict) and p.get("y") is not None:
                    result.append((int(p["x"]), float(p["y"])))
            return result

        self.log.warning("SmartHub: unrecognised response shape — keys: %s", list(data.keys()))
        return []

    def _normalise(
        self, raw: list[tuple[int, float]], gran_min: int
    ) -> list[dict]:
        """Convert (unix_ms, kwh) pairs into utility_usage row dicts."""
        acct       = self.account
        meter_id   = acct.get("service_location_number") or acct.get("account_number")
        gran_ms    = gran_min * 60 * 1000

        rows = []
        for ms, kwh in raw:
            if kwh is None or kwh < 0:
                continue
            start = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            end   = datetime.fromtimestamp((ms + gran_ms) / 1000, tz=timezone.utc)
            rows.append({
                "utility":        self.utility,
                "account_number": acct["account_number"],
                "meter_id":       meter_id,
                "interval_start": start,
                "interval_end":   end,
                "kwh":            kwh,
                "is_estimated":   False,
                "granularity_min": gran_min,
            })
        return rows
