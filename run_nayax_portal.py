#!/usr/bin/env python
"""Run the Nayax Core portal collector by hand.

    ./venv/bin/python run_nayax_portal.py --login --headful   # watch it log in
    ./venv/bin/python run_nayax_portal.py --login             # headless (VPS)
    ./venv/bin/python run_nayax_portal.py --probe      # read last 30 days, write NOTHING
    ./venv/bin/python run_nayax_portal.py              # incremental collect + match
    ./venv/bin/python run_nayax_portal.py --backfill   # from backfill_floor to now

Credentials come from nayax_credentials in Supabase; DATABASE_URL and
TWOCAPTCHA_API_KEY come from .env. Nothing is ever printed that would expose
the password or the TOTP secret.

Run --login once on a machine to seed NAYAX_STATE_PATH. After that the session
cookie is reused and the browser only launches when it expires.

This is the portal path. run_nayax.py is the older Lynx API probe, kept for the
day Nayax finally enables API permissions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta

import asyncpg
from dotenv import load_dotenv

from app.collectors import nayax


async def do_login(headful: bool = False) -> None:
    """Log in and persist the session.

    Headless by default so this works on the VPS, which has no display. Pass
    --headful on a desktop to watch it drive the login, which is the fastest
    way to see where a selector has drifted.
    """
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    row = await conn.fetchrow(
        "SELECT username, password, totp_secret FROM nayax_credentials"
        " WHERE enabled AND username IS NOT NULL ORDER BY id LIMIT 1"
    )
    await conn.close()
    if not row:
        sys.exit("No usable row in nayax_credentials — set username/password/totp_secret.")

    cookies = await nayax._login(dict(row), headless=not headful)
    nayax._save_state(cookies)
    print(f"Session saved to {nayax.STATE_PATH} ({len(cookies)} cookies)")


async def do_probe() -> None:
    """Read the last 30 days and summarise. Touches no tables."""
    import httpx

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    row = await conn.fetchrow(
        "SELECT username, password, totp_secret FROM nayax_credentials"
        " WHERE enabled AND username IS NOT NULL ORDER BY id LIMIT 1"
    )
    await conn.close()
    if not row:
        sys.exit("No usable row in nayax_credentials.")

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as http:
        client = nayax.NayaxClient(http)
        cookies = nayax._load_state()
        for cookie in cookies or []:
            http.cookies.set(cookie["name"], cookie["value"],
                             domain=cookie.get("domain", "my.nayax.com"))

        if not await client.refresh_token():
            print("No usable session — logging in headless…")
            cookies = await nayax._login(dict(row))
            nayax._save_state(cookies)
            for cookie in cookies:
                http.cookies.set(cookie["name"], cookie["value"],
                                 domain=cookie.get("domain", "my.nayax.com"))
            if not await client.refresh_token():
                sys.exit("Logged in but no CSRF token on the report page.")

        end = datetime.now(nayax.AK_TZ)
        rows = await client.fetch_range(end - timedelta(days=30), end)

    print(f"\n{len(rows)} transactions in the last 30 days\n")
    settled = [r for r in rows if r.get("tran_status_id") == nayax.STATUS_SETTLED]
    revenue = sum(float(r.get("seValue") or 0) for r in settled)
    print(f"  settled (status 12): {len(settled)}")
    print(f"  revenue            : ${revenue:,.2f}")

    by_status: dict = {}
    for r in rows:
        by_status.setdefault(r.get("tran_status_id"), 0)
        by_status[r.get("tran_status_id")] += 1
    print(f"  status breakdown   : {by_status}")

    by_device: dict = {}
    for r in settled:
        key = f"{r.get('ex_device_number')} ({r.get('machine_name')})"
        by_device.setdefault(key, 0)
        by_device[key] += 1
    print("\n  settled per device:")
    for key, count in sorted(by_device.items()):
        print(f"    {key}: {count}")

    if settled:
        sample = settled[0]
        print("\n  newest settled transaction, as it would be stored:")
        record = nayax._row_to_record(sample)
        fields = ("nayax_id", "device", "machine", "site", "tap_utc", "settle_utc",
                  "updated_utc", "auth_cents", "committed_cents", "currency",
                  "entry_mode", "brand", "pan", "service", "status", "type", "secs")
        for name, value in zip(fields, record[:17]):
            print(f"    {name:<16} {value}")


async def do_collect(backfill: bool) -> None:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=4)
    try:
        stored = await nayax.collect_nayax(pool, backfill=backfill)
        print(f"\nUpserted {stored} transactions.")
    finally:
        await pool.close()


async def do_daemon(interval: int) -> None:
    """Poll forever. This is the container's entrypoint.

    A failed cycle is logged and slept through rather than crashing the
    container — an expired session or a 2captcha hiccup should cost one cycle,
    not restart-loop the service.
    """
    log = logging.getLogger("rca.collectors.nayax")
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=4)
    try:
        while True:
            try:
                stored = await nayax.collect_nayax(pool)
                log.info("Cycle complete — %d transactions upserted", stored)
            except Exception:
                log.error("Nayax collection cycle failed", exc_info=True)
            await asyncio.sleep(interval)
    finally:
        await pool.close()


def main() -> None:
    load_dotenv(".env")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    # Default the session next to the repo for local runs; the container
    # overrides this to a mounted volume.
    os.environ.setdefault("NAYAX_STATE_PATH", ".nayax_session.json")
    nayax.STATE_PATH = __import__("pathlib").Path(os.environ["NAYAX_STATE_PATH"])

    if "--login" in sys.argv:
        asyncio.run(do_login(headful="--headful" in sys.argv))
    elif "--probe" in sys.argv:
        asyncio.run(do_probe())
    elif "--daemon" in sys.argv:
        asyncio.run(do_daemon(int(os.environ.get("NAYAX_POLL_SECONDS", "900"))))
    else:
        asyncio.run(do_collect(backfill="--backfill" in sys.argv))


if __name__ == "__main__":
    main()
