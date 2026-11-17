"""Check that a doctor's Google Calendar is shared with the service account.

Creates a 15-minute test event tomorrow on that calendar, then deletes it.

    uv run python scripts/check_calendar.py asha.mehta@gmail.com
    uv run python scripts/check_calendar.py asha.mehta@gmail.com --keep   # leave it, to look at it
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

from clinic_agent.config import settings  # noqa: E402
from clinic_agent.integrations import google_calendar  # noqa: E402


def _service_account_email() -> str:
    if settings.GOOGLE_SERVICE_ACCOUNT_JSON:
        return json.loads(settings.GOOGLE_SERVICE_ACCOUNT_JSON).get("client_email", "?")
    if settings.GOOGLE_SERVICE_ACCOUNT_FILE:
        with open(settings.GOOGLE_SERVICE_ACCOUNT_FILE) as f:
            return json.load(f).get("client_email", "?")
    return "(no service account configured)"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test write access to a doctor's calendar.")
    parser.add_argument("calendar_email", help="the doctor's Google Calendar email / calendar ID")
    parser.add_argument("--keep", action="store_true", help="don't delete the test event")
    args = parser.parse_args()

    print(f"Service account: {_service_account_email()}")
    print(f"Calendar:        {args.calendar_email}")
    tz = ZoneInfo(settings.CLINIC_TIMEZONE)
    start = (datetime.now(tz) + timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    event_id = "check" + uuid.uuid4().hex  # base32hex-safe
    body = {
        "summary": "Clinic AI receptionist: calendar test",
        "description": "Created by scripts/check_calendar.py. Safe to delete.",
        "start": {"dateTime": start.isoformat(), "timeZone": settings.CLINIC_TIMEZONE},
        "end": {
            "dateTime": (start + timedelta(minutes=15)).isoformat(),
            "timeZone": settings.CLINIC_TIMEZONE,
        },
    }
    try:
        await google_calendar.upsert_event(args.calendar_email, event_id, body)
        print(f"OK: created a test event for {start:%a %d %b, %H:%M} ({settings.CLINIC_TIMEZONE}).")
        if args.keep:
            print("Kept it — delete it from the calendar when you've looked.")
        else:
            await google_calendar.delete_event(args.calendar_email, event_id)
            print("OK: deleted it again. Calendar sync will work for this doctor.")
    except google_calendar.CalendarError as exc:
        print(f"FAILED: {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    asyncio.run(main())
