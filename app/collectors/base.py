"""Abstract base class shared by all utility collectors."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import asyncpg

logger = logging.getLogger("rca.collectors")


class AbstractCollector(ABC):
    """Subclass one of these per utility platform (SmartHub, mymeterQ, …).

    Each instance is scoped to a single *account* row from utility_accounts.
    Credentials come from the matching utility_credentials row.
    """

    # Subclasses must set this to match the utility column value.
    utility: str

    def __init__(self, account: dict, credentials: dict) -> None:
        self.account     = account       # row from utility_accounts
        self.credentials = credentials   # row from utility_credentials
        self.log         = logging.getLogger(f"rca.collectors.{self.utility}")

    # ── Abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    async def collect(self, pool: asyncpg.Pool, days_back: int = 2) -> int:
        """Fetch usage data and upsert to utility_usage.

        Args:
            pool:      asyncpg connection pool.
            days_back: how many calendar days to (re-)fetch.  Default 2 gives
                       one full day of overlap so we never miss a late-arriving read.

        Returns:
            Number of rows upserted.
        """

    # ── Shared helpers ────────────────────────────────────────────────────────

    async def upsert_usage(self, pool: asyncpg.Pool, rows: list[dict]) -> int:
        """Bulk-upsert a list of usage dicts to the utility_usage table.

        Each dict must contain:
            utility, account_number, interval_start (datetime), interval_end (datetime),
            kwh (float), granularity_min (int)

        Optional keys:
            meter_id (str), is_estimated (bool)
        """
        if not rows:
            return 0

        records = [
            (
                r["utility"],
                r["account_number"],
                r.get("meter_id"),
                r["interval_start"],
                r["interval_end"],
                float(r["kwh"]) if r["kwh"] is not None else None,
                bool(r.get("is_estimated", False)),
                int(r["granularity_min"]),
            )
            for r in rows
        ]

        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO utility_usage
                    (utility, account_number, meter_id,
                     interval_start, interval_end,
                     kwh, is_estimated, granularity_min)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (utility, account_number, interval_start)
                DO UPDATE SET
                    kwh          = EXCLUDED.kwh,
                    is_estimated = EXCLUDED.is_estimated,
                    collected_at = NOW()
                """,
                records,
            )

        return len(records)

    async def mark_collected(
        self, pool: asyncpg.Pool, error: str | None = None
    ) -> None:
        """Update last_collected / last_error on the utility_accounts row."""
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE utility_accounts
                SET last_collected = NOW(),
                    last_error     = $1
                WHERE utility        = $2
                  AND account_number = $3
                """,
                error,
                self.utility,
                self.account["account_number"],
            )
