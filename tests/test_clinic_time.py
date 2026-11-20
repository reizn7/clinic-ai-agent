from datetime import UTC, datetime

from clinic_agent import clinic_time
from clinic_agent.runtime import conversations, turn


def test_clinic_today_ignores_server_timezone(monkeypatch):
    # 23:00 UTC on Nov 20 is already 04:30 on Nov 21 in Asia/Kolkata.
    utc_late = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return utc_late.astimezone(tz) if tz else utc_late.replace(tzinfo=None)

    monkeypatch.setattr(clinic_time, "datetime", _Clock)
    assert clinic_time.clinic_today().isoformat() == "2026-11-21"
    monkeypatch.setattr(turn, "clinic_now", clinic_time.clinic_now)
    assert "November 21, 2026" in turn.contextualize("hi", [], "")


def test_slot_start_is_clinic_local():
    start = clinic_time.slot_start("2026-11-23", "10:00")
    assert start.isoformat() == "2026-11-23T10:00:00+05:30"
    assert start.astimezone(UTC).isoformat() == "2026-11-23T04:30:00+00:00"


def test_appointment_at_is_utc():
    calls = [
        {
            "name": "book_appointment",
            "status": "success",
            "args": {"date": "2026-11-23", "slot": "10:00"},
        }
    ]
    assert conversations._appointment_at(calls) == "2026-11-23T04:30:00+00:00"
