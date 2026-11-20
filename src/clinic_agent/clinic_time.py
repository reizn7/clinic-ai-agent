"""The clinic's wall clock.

Appointment dates and slots are local to the clinic (``settings.CLINIC_TIMEZONE``),
while the server may run in any timezone (containers default to UTC). Anything
that reasons about "today" or turns a date+slot into an instant goes through here,
never through ``datetime.now()`` / ``date.today()``.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from clinic_agent.config import settings


def clinic_tz() -> ZoneInfo:
    return ZoneInfo(settings.CLINIC_TIMEZONE)


def clinic_now() -> datetime:
    """The current time in the clinic's timezone (tz-aware)."""
    return datetime.now(clinic_tz())


def clinic_today() -> date:
    return clinic_now().date()


def slot_start(date_key: str, slot: str) -> datetime:
    """A booked ``YYYY-MM-DD`` + ``HH:mm`` as a tz-aware clinic-local datetime."""
    return datetime.strptime(f"{date_key} {slot}", "%Y-%m-%d %H:%M").replace(tzinfo=clinic_tz())
