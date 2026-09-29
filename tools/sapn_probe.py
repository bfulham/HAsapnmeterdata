"""Probe the SAPN portal to check the assumptions the integration relies on.

Uses the integration's own portal client and NEM12 parser, without Home
Assistant. Requires Python 3.12+ and aiohttp:

    python -m pip install aiohttp
    python tools/sapn_probe.py [--nmi NMI] [--max-days 1100]

Credentials come from the SAPN_EMAIL and SAPN_PASSWORD environment variables,
or are prompted for. They are sent only to SAPN and are not printed.

The probe reports:

- the account's meters and their types;
- whether a request's end date is inclusive;
- the newest day SAPN has published right now;
- the largest date range the portal returns in one request;
- how far back the portal's history goes;
- each channel's unit and how many days are not final (estimated etc.).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.util
import os
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

PORTAL_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "sapnmeterdata"
    / "portal"
)
ADELAIDE = ZoneInfo("Australia/Adelaide")
PAUSE_SECONDS = 2.0


def load_portal():
    """Import the portal package without importing Home Assistant."""
    spec = importlib.util.spec_from_file_location(
        "sapn_portal",
        PORTAL_DIR / "__init__.py",
        submodule_search_locations=[str(PORTAL_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["sapn_portal"] = module
    spec.loader.exec_module(module)
    return module


portal = load_portal()


class Probe:
    """Run portal requests with a pause between them."""

    def __init__(self, client, nmi: str) -> None:
        self.client = client
        self.nmi = nmi

    async def days(self, first: date, last: date):
        """Return ``(readings, seconds, error)`` for a date range."""
        await asyncio.sleep(PAUSE_SECONDS)
        started = time.monotonic()
        try:
            text = await self.client.download_nem12(self.nmi, first, last)
        except portal.SAPNNoDataError:
            return [], time.monotonic() - started, None
        except portal.SAPNError as err:
            return None, time.monotonic() - started, f"{type(err).__name__}: {err}"
        readings = [
            reading
            for reading in portal.parse_nem12(text)
            if portal.nmi_matches(self.nmi, reading.nmi)
        ]
        return readings, time.monotonic() - started, None


def day_span(readings) -> str:
    dates = sorted({reading.day for reading in readings})
    if not dates:
        return "no days"
    return f"{len(dates)} days, {dates[0]} to {dates[-1]}"


async def run(email: str, password: str, nmi: str | None, max_days: int) -> None:
    now = datetime.now(ADELAIDE)
    today = now.date()
    print(f"Probe run at {now:%Y-%m-%d %H:%M %Z} (Adelaide)\n")

    async with aiohttp.ClientSession() as session:
        client = portal.SAPNClient(session, email, password)
        await client.login()
        print("Signed in.\n\nMeters:")
        meters = await client.get_assignments()
        for meter in meters:
            print(f"  {meter.nmi}  {meter.name!r}  type={meter.meter_type_label!r}")
        if nmi is None:
            candidates = [m for m in meters if m.supports_interval_data is not False]
            if not candidates:
                print("No interval meters on this account.")
                return
            nmi = candidates[0].nmi
        print(f"\nProbing {nmi}\n")
        probe = Probe(client, nmi)

        # 1. Is the end date inclusive? Request one day, two days ago.
        target = today - timedelta(days=3)
        readings, _, error = await probe.days(target, target)
        print(f"Single-day request for {target}: {error or day_span(readings)}")

        # 2. Newest published day right now.
        readings, _, error = await probe.days(today - timedelta(days=6), today)
        newest = max((r.day for r in readings or []), default=None)
        print(f"Request ending today: {error or day_span(readings)}")
        print(f"Newest published day: {newest}\n")

        # 3. Largest range returned in one request.
        end = newest or today - timedelta(days=1)
        print("Range sizes (days requested -> result, seconds):")
        for size in (30, 60, 90, 180, 365):
            readings, seconds, error = await probe.days(
                end - timedelta(days=size - 1), end
            )
            print(f"  {size:>4} -> {error or day_span(readings)} ({seconds:.1f}s)")
            if error:
                break

        # 4. How far back history goes, walking back 30 days at a time.
        print("\nHistory (30-day steps back until 60 empty days):")
        earliest = None
        empty = 0
        channel_units: dict[str, str] = {}
        qualities: Counter[str] = Counter()
        intervals: Counter[int] = Counter()
        step_end = end
        while empty < 60 and (end - step_end).days < max_days:
            step_start = step_end - timedelta(days=29)
            readings, _, error = await probe.days(step_start, step_end)
            if error:
                print(f"  {step_start} to {step_end}: {error}")
                readings = []
            if readings:
                empty = 0
                earliest = min(r.day for r in readings)
                for reading in readings:
                    channel_units[reading.suffix] = reading.uom
                    intervals[reading.interval_minutes] += 1
                    if not reading.is_final:
                        qualities[
                            f"{reading.suffix}:{''.join(sorted(set(reading.qualities)))}"
                        ] += 1
            else:
                empty += 30
            print(f"  {step_start} to {step_end}: {error or day_span(readings)}")
            step_end = step_start - timedelta(days=1)

    print("\nSummary")
    print(f"  Newest published day: {newest}")
    print(f"  Earliest day found:   {earliest}")
    print(f"  Channels and units:   {channel_units}")
    print(f"  Interval lengths:     {dict(intervals)} (minutes: day-records)")
    print(f"  Non-final day-records by channel:quality: {dict(qualities) or 'none'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nmi", help="Meter to probe (default: first interval meter)")
    parser.add_argument(
        "--max-days",
        type=int,
        default=1100,
        help="Stop walking back after this many days (default: 1100)",
    )
    args = parser.parse_args()
    email = os.environ.get("SAPN_EMAIL") or input("SAPN email: ")
    password = os.environ.get("SAPN_PASSWORD") or getpass.getpass("SAPN password: ")
    try:
        asyncio.run(run(email, password, args.nmi, args.max_days))
    except portal.SAPNAuthError as err:
        sys.exit(f"SAPN rejected the login: {err}")
    except portal.SAPNError as err:
        sys.exit(f"SAPN portal error: {type(err).__name__}: {err}")


if __name__ == "__main__":
    main()
