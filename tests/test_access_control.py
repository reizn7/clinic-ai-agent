"""Caller identity, voice verification (security codes), and appointment ownership."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from clinic_agent.clinic_time import clinic_today
from clinic_agent.config import settings
from clinic_agent.runtime.tool_capture import ToolCallRecorder
from clinic_agent.tools import access_code, booking_tools, repo
from clinic_agent.tools.booking_tools import CALLER_PHONE, CHANNEL

ME = "919999999999"
OTHER = "918888888888"


def _next(py_weekday: int) -> str:
    d = clinic_today() + timedelta(days=1)
    while d.weekday() != py_weekday:
        d += timedelta(days=1)
    return d.isoformat()


MON, SUN = _next(0), _next(6)


@pytest.fixture
def db(_no_real_mongo, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_CONFIRMATIONS_ENABLED", False)
    monkeypatch.setattr(settings, "GOOGLE_CALENDAR_ENABLED", False)
    _no_real_mongo["doctors"].insert_one(
        {
            "clinicId": settings.CLINIC_ID,
            "name": "Rohit Sharma",
            "specialization": "Dentist",
            "consultationDuration": 30,
            # Stored out of order on purpose: the tools return them Monday-first.
            "availableDays": ["Saturday", "Monday", "Friday", "Wednesday"],
            "workingHours": {"start": "14:00", "end": "17:00"},
            "isActive": True,
        }
    )
    return _no_real_mongo


def ctx(phone: str = ME, channel: str = "voice", turn: str = "turn-1") -> SimpleNamespace:
    """Stand-in for ADK's ToolContext: the tools use ``.state`` (and, for the
    WhatsApp booking confirmation gate, ``.invocation_id`` = one turn)."""
    return SimpleNamespace(state={CALLER_PHONE: phone, CHANNEL: channel}, invocation_id=turn)


def _book_for(phone: str, name: str = "Ravi Kumar", slot: str = "14:00") -> dict:
    res = repo.create_appointment(phone, name, 34, "Male", "Rohit Sharma", MON, slot)
    assert res["success"], res
    return res


def _code_for(db, phone: str) -> str:
    patient = db["patients"].find_one({"phone": phone})
    return access_code.ensure_access_code(patient["_id"])


# ---- availability carries the doctor's week -----------------------------------------


def test_availability_includes_working_days_and_hours(db):
    expected = ["Monday", "Wednesday", "Friday", "Saturday"]
    on = repo.get_available_slots("Rohit Sharma", MON)
    off = repo.get_available_slots("Rohit Sharma", SUN)
    upcoming = repo.get_upcoming_availability("Rohit Sharma", 3)
    for res in (on, off, upcoming):
        assert res["workingDays"] == expected
        assert res["workingHours"] == "14:00-17:00"
    assert off["slots"] == [] and "does not work on Sunday" in off["note"]


# ---- caller identity comes from the session ------------------------------------------


async def test_tools_use_the_session_number_not_a_model_argument(db):
    _book_for(OTHER, "Someone Else")
    res = await booking_tools.list_my_appointments(ctx(ME, "whatsapp"))
    assert res == {"appointments": []}  # OTHER's appointment is invisible to ME


async def test_missing_caller_number_is_refused(db):
    res = await booking_tools.list_my_appointments(SimpleNamespace(state={}))
    assert res["success"] is False


# ---- WhatsApp: the sender is already proven -------------------------------------------


async def test_whatsapp_needs_no_code(db):
    _book_for(ME)
    res = await booking_tools.list_my_appointments(ctx(ME, "whatsapp"))
    assert len(res["appointments"]) == 1


# ---- voice: verify before touching existing appointments ----------------------------


async def test_voice_lookup_and_changes_need_verification(db):
    appt = _book_for(ME)["appointmentId"]
    c = ctx()
    assert (await booking_tools.list_my_appointments(c))["verification_required"] is True
    assert (await booking_tools.cancel_appointment(appt, c))["verification_required"] is True
    res = await booking_tools.reschedule_appointment(appt, MON, "15:00", c)
    assert res["verification_required"] is True
    assert db["appointments"].find_one()["status"] == "Pending"  # untouched


async def test_voice_correct_code_unlocks_the_call(db):
    _book_for(ME)
    code = _code_for(db, ME)
    c = ctx()
    wrong = await booking_tools.verify_caller("000000" if code != "000000" else "111111", c)
    assert wrong["verified"] is False and wrong["attempts_left"] == 2
    assert (await booking_tools.verify_caller(f"{code[:3]} {code[3:]}", c))["verified"] is True
    assert len((await booking_tools.list_my_appointments(c))["appointments"]) == 1


async def test_one_numbers_code_does_not_unlock_another(db):
    _book_for(ME)
    _book_for(OTHER, "Someone Else", slot="14:30")
    other_code = _code_for(db, OTHER)
    _code_for(db, ME)
    c = ctx(ME)
    assert (await booking_tools.verify_caller(other_code, c))["verified"] is False


async def test_unknown_number_reads_like_a_wrong_code(db):
    res = await booking_tools.verify_caller("123456", ctx("917777777777"))
    assert res["verified"] is False and res["error"] == "That code doesn't match this number."


async def test_per_call_attempt_limit(db):
    _book_for(ME)
    code = _code_for(db, ME)
    bad = "000000" if code != "000000" else "111111"
    c = ctx()
    for _ in range(booking_tools.MAX_ATTEMPTS_PER_CALL):
        await booking_tools.verify_caller(bad, c)
    # Even the right code is refused once this call is out of attempts.
    res = await booking_tools.verify_caller(code, c)
    assert res["verified"] is False and "Too many attempts" in res["error"]


async def test_number_locks_after_repeated_failures_across_calls(db):
    _book_for(ME)
    code = _code_for(db, ME)
    bad = "000000" if code != "000000" else "111111"
    for _ in range(access_code.MAX_FAILURES):
        await booking_tools.verify_caller(bad, ctx())  # a fresh call each time
    res = await booking_tools.verify_caller(code, ctx())
    assert res["verified"] is False and "locked" in res["error"]


async def test_success_resets_the_failure_count(db):
    _book_for(ME)
    code = _code_for(db, ME)
    bad = "000000" if code != "000000" else "111111"
    for _ in range(access_code.MAX_FAILURES - 1):
        await booking_tools.verify_caller(bad, ctx())
    assert (await booking_tools.verify_caller(code, ctx()))["verified"] is True
    assert db["patients"].find_one({"phone": ME})["accessCodeFailures"] == 0


async def test_unverified_caller_can_manage_what_they_booked_this_call(db):
    c = ctx()
    booked = await booking_tools.book_appointment(
        "Yashi Goyal", 26, "Female", "Rohit Sharma", MON, "14:00", c
    )
    assert booked["success"]
    res = await booking_tools.reschedule_appointment(booked["appointmentId"], MON, "15:00", c)
    assert res["success"] is True
    # ...but still can't list (or touch) anything older.
    assert (await booking_tools.list_my_appointments(c))["verification_required"] is True


async def test_unverified_voice_booking_keeps_existing_patient_details(db):
    _book_for(ME, "Ravi Kumar")
    res = await booking_tools.book_appointment(
        "Impostor", 50, "Male", "Rohit Sharma", MON, "15:00", ctx()
    )
    assert res["success"]
    assert db["patients"].find_one({"phone": ME})["name"] == "Ravi Kumar"


async def test_whatsapp_booking_still_updates_patient_details(db):
    _book_for(ME, "Ravi Kumar")
    args = ("Ravi K. Sharma", 34, "Male", "Rohit Sharma", MON, "15:00")
    # Turn 1 only proposes (read-back); turn 2 — the patient's yes — books.
    first = await booking_tools.book_appointment(*args, ctx(ME, "whatsapp", "turn-1"))
    assert first["needs_confirmation"] is True
    assert db["patients"].find_one({"phone": ME})["name"] == "Ravi Kumar"
    booked = await booking_tools.book_appointment(*args, ctx(ME, "whatsapp", "turn-2"))
    assert booked["success"] is True
    assert db["patients"].find_one({"phone": ME})["name"] == "Ravi K. Sharma"


# ---- ownership: an appointment id alone is not enough ------------------------------


async def test_cannot_cancel_or_reschedule_someone_elses_appointment(db):
    theirs = _book_for(OTHER, "Someone Else")["appointmentId"]
    c = ctx(ME, "whatsapp")
    assert (await booking_tools.cancel_appointment(theirs, c))["success"] is False
    res = await booking_tools.reschedule_appointment(theirs, MON, "15:00", c)
    assert res["success"] is False
    appt = db["appointments"].find_one()
    assert (appt["status"], appt["slot"]) == ("Pending", "14:00")


# ---- codes ---------------------------------------------------------------------------


def test_access_code_is_six_digits_and_stable(db):
    _book_for(ME)
    first = _code_for(db, ME)
    assert len(first) == 6 and first.isdigit()
    assert _code_for(db, ME) == first


def test_recorded_tool_calls_hide_the_code():
    call = SimpleNamespace(id="1", name="verify_caller", args={"code": "482913"})
    resp = SimpleNamespace(id="1", name="verify_caller", response={"verified": True})
    event = SimpleNamespace(get_function_calls=lambda: [call], get_function_responses=lambda: [])
    done = SimpleNamespace(get_function_calls=lambda: [], get_function_responses=lambda: [resp])
    rec = ToolCallRecorder()
    rec.observe(event)
    (record,) = rec.observe(done)
    assert record["args"] == {"code": "[redacted]"}
