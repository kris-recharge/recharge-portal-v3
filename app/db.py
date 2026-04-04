"""Async Postgres connection pool for FastAPI (asyncpg).

Ported from v2 db.py but switched to asyncpg for non-blocking I/O.
A synchronous helper (get_conn_sync) is kept for the alerts background thread.
"""

from __future__ import annotations

import asyncio
import os
import socket
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

import asyncpg
import psycopg

from .config import DATABASE_URL

# ── Shared pool (initialised in lifespan) ────────────────────────────────────
_pool: Optional[asyncpg.Pool] = None


def _resolve_ipv4(hostname: str) -> Optional[str]:
    try:
        infos = socket.getaddrinfo(hostname, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
        return infos[0][4][0] if infos else None
    except Exception:
        return None


async def create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        DATABASE_URL,
        min_size=2,
        max_size=10,
        command_timeout=30,
        # Keep idle connections alive — Supabase's PgBouncer drops connections
        # that are silent for >5 min. A SELECT 1 every 4 min prevents that.
        max_inactive_connection_lifetime=240,  # recycle after 4 min idle
        server_settings={"application_name": "rca_v3"},
    )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await create_pool()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire() -> AsyncGenerator[asyncpg.Connection, None]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


# ── Sync connection (for alerts background thread) ───────────────────────────
def get_conn_sync():
    """Return a synchronous psycopg v3 connection. Used only by alerts.py."""
    conn = psycopg.connect(
        DATABASE_URL,
        connect_timeout=10,
        autocommit=True,
        application_name="rca_v3_alerts",
    )
    return conn
