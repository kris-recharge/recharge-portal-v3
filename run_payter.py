#!/usr/bin/env python
"""Run the Payter CCR transaction import manually from this machine.

Usage:
    ./venv/bin/python run_payter.py           # incremental run (first run backfills from backfill_floor)
    ./venv/bin/python run_payter.py --probe   # auth + fetch ONE page, print a sample doc, write nothing

--probe is for validating credentials and field assumptions (ifd values,
amount units, ingestTime filter format) before committing data to Supabase.

Reads DATABASE_URL from .env. Credentials come from the payter_credentials
table in Supabase — insert/update your row there first.

In production this same import runs inside the app daily at 04:15 AK
(see app/collectors/scheduler.py); this script exists for testing and
one-off backfills.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import asyncpg
import httpx
from dotenv import load_dotenv

from app.collectors.payter import PayterClient, collect_payter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


async def probe(database_url: str) -> None:
    conn = await asyncpg.connect(database_url)
    creds = await conn.fetch(
        "SELECT username, password, domain, backfill_floor"
        " FROM payter_credentials WHERE enabled ORDER BY id"
    )
    await conn.close()
    if not creds:
        print("No enabled rows in payter_credentials — add your login there first.")
        return

    async with httpx.AsyncClient(timeout=60) as http:
        for cred in creds:
            print(f"\n=== domain {cred['domain']} ({cred['username']}) ===")
            client = PayterClient(http, cred["username"], cred["password"], cred["domain"])
            await client.login()
            print("auth OK, token acquired")
            try:
                docs = await client.fetch_transactions_page(since=cred["backfill_floor"])
                print(f"first page: {len(docs)} documents")
                if docs:
                    print("sample document:")
                    print(json.dumps(docs[0], indent=2, sort_keys=True))
                    ifds = sorted({str(d.get("ifd")) for d in docs})
                    serials = sorted({str(d.get("serialNumber")) for d in docs})
                    print(f"ifd values on this page:    {ifds}")
                    print(f"serials on this page:       {serials}")
            finally:
                await client.logout()


async def full_run(database_url: str) -> None:
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    try:
        n = await collect_payter(pool)
        print(f"Done — {n} transactions upserted into payter_transactions.")
    finally:
        await pool.close()


if __name__ == "__main__":
    load_dotenv(".env")
    url = os.environ["DATABASE_URL"]
    if "--probe" in sys.argv:
        asyncio.run(probe(url))
    else:
        asyncio.run(full_run(url))
