from datetime import timedelta

import mongomock
import pytest
from bson import ObjectId

from clinic_agent.clinic_time import clinic_today
from clinic_agent.config import settings
from clinic_agent.tools import catalog, repo

CLINIC = settings.CLINIC_ID


@pytest.fixture
def db(monkeypatch):
    client = mongomock.MongoClient()
    fake = client["clinic-test"]
    monkeypatch.setattr(repo, "get_db", lambda: fake)
    monkeypatch.setattr(catalog, "get_db", lambda: fake)
    catalog.clear_cache()
    # One active doctor: works Mon-Fri 09:00-11:00, 30-min slots.
    fake["doctors"].insert_one(
        {
            "clinicId": CLINIC,
            "name": "Asha Mehta",
            "specialization": "General Physician",
            "consultationDuration": 30,
            "availableDays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
            "workingHours": {"start": "09:00", "end": "11:00"},
            "isActive": True,
        }
    )
    return fake


def _next_weekday(py_weekday: int) -> str:
    """The next FUTURE date (from tomorrow) matching a Python weekday (Mon=0)."""
    d = clinic_today() + timedelta(days=1)
    for _ in range(14):
        if d.weekday() == py_weekday:
            return d.isoformat()
        d += timedelta(days=1)
    raise AssertionError("unreachable")


MON = _next_weekday(0)  # a future Monday (doctor works)
SUN = _next_weekday(6)  # a future Sunday (doctor off)
PAST = (clinic_today() - timedelta(days=1)).isoformat()


def test_list_active_doctors(db):
    docs = repo.list_active_doctors()
    assert [d["name"] for d in docs] == ["Asha Mehta"]


def test_availability_on_working_day(db):
    res = repo.get_available_slots("Asha Mehta", MON)
    assert res["weekday"] == "Monday"
    assert res["slots"] == ["09:00", "09:30", "10:00", "10:30"]


def test_availability_on_off_day_is_empty(db):
    res = repo.get_available_slots("Asha Mehta", SUN)
    assert res["slots"] == []
    assert "does not work" in res["note"]


def test_unknown_doctor(db):
    assert "error" in repo.get_available_slots("Nobody", MON)


def test_book_then_slot_disappears_and_appears_in_list(db):
    booked = repo.create_appointment(
        "919999999999", "Ravi", 30, "Male", "Asha Mehta", MON, "09:30"
    )
    assert booked["success"] is True
    assert booked["slot"] == "09:30"

    # That slot is no longer offered.
    res = repo.get_available_slots("Asha Mehta", MON)
    assert "09:30" not in res["slots"]

    # It shows up in the patient's appointments.
    appts = repo.list_patient_appointments("919999999999")
    assert len(appts) == 1
    assert appts[0]["doctor"] == "Asha Mehta"
    assert appts[0]["slot"] == "09:30"


def test_cancel_appointment_frees_slot(db):
    booked = repo.create_appointment(
        "919999999999", "Ravi", 30, "Male", "Asha Mehta", MON, "09:30"
    )
    res = repo.cancel_appointment(booked["appointmentId"])
    assert res["success"] is True

    # Cancelled -> slot free again, and not listed as upcoming.
    assert "09:30" in repo.get_available_slots("Asha Mehta", MON)["slots"]
    assert repo.list_patient_appointments("919999999999") == []


def test_cancel_invalid_id(db):
    assert repo.cancel_appointment("not-an-id")["success"] is False


def test_booking_writes_patient_and_appointment_timestamps(db):
    booked = repo.create_appointment(
        "919999999999", "Ravi", 30, "Male", "Asha Mehta", MON, "09:30"
    )
    patient = db["patients"].find_one({"phone": "919999999999"})
    assert "createdAt" in patient and "updatedAt" in patient

    appt = db["appointments"].find_one({"_id": ObjectId(booked["appointmentId"])})
    assert "createdAt" in appt and "updatedAt" in appt


def test_booking_and_lookups_are_clinic_scoped(db):
    booked = repo.create_appointment(
        "919999999999", "Ravi", 30, "Male", "Asha Mehta", MON, "09:30"
    )
    assert db["patients"].find_one({"phone": "919999999999"})["clinicId"] == CLINIC
    appt = db["appointments"].find_one({"_id": ObjectId(booked["appointmentId"])})
    assert appt["clinicId"] == CLINIC

    # Another clinic's appointment can't be listed, cancelled or rescheduled here.
    other = db["appointments"].insert_one(
        {"clinicId": "other-clinic", "patientId": appt["patientId"], "doctorId": appt["doctorId"],
         "date": MON, "slot": "10:00", "status": "Pending"}
    ).inserted_id
    assert [a["slot"] for a in repo.list_patient_appointments("919999999999")] == ["09:30"]
    assert repo.cancel_appointment(str(other))["success"] is False
    assert repo.reschedule_appointment(str(other), MON, "10:30")["success"] is False


def test_cancel_stamps_cancelled_at(db):
    booked = repo.create_appointment(
        "919999999999", "Ravi", 30, "Male", "Asha Mehta", MON, "09:30"
    )
    repo.cancel_appointment(booked["appointmentId"])
    appt = db["appointments"].find_one({"_id": ObjectId(booked["appointmentId"])})
    assert "cancelledAt" in appt


def test_availability_rejects_past_date(db):
    res = repo.get_available_slots("Asha Mehta", PAST)
    assert res["slots"] == []
    assert "past" in res["note"].lower()


def test_book_rejects_past_date(db):
    res = repo.create_appointment("919", "Ravi", 30, "Male", "Asha Mehta", PAST, "09:00")
    assert res["success"] is False


def test_book_rejects_invalid_slot(db):
    # 08:00 is outside the doctor's 09:00-11:00 window.
    res = repo.create_appointment("919", "Ravi", 30, "Male", "Asha Mehta", MON, "08:00")
    assert res["success"] is False
    assert "available_slots" in res


def test_upcoming_availability_returns_working_dates(db):
    res = repo.get_upcoming_availability("Asha Mehta", 2)
    assert len(res["dates"]) == 2
    for d in res["dates"]:
        assert d["weekday"] in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        assert d["slots"]


def test_reschedule_moves_appointment(db):
    booked = repo.create_appointment("919", "Ravi", 30, "Male", "Asha Mehta", MON, "09:30")
    res = repo.reschedule_appointment(booked["appointmentId"], MON, "10:30")
    assert res["success"] is True
    assert res["slot"] == "10:30"
    # Old slot freed, new slot taken.
    slots = repo.get_available_slots("Asha Mehta", MON)["slots"]
    assert "09:30" in slots and "10:30" not in slots


def test_reschedule_rejects_unavailable_slot(db):
    booked = repo.create_appointment("919", "Ravi", 30, "Male", "Asha Mehta", MON, "09:30")
    res = repo.reschedule_appointment(booked["appointmentId"], SUN, "09:00")  # off day
    assert res["success"] is False


def test_confirmation_gate_needs_a_later_turn(db):
    phone = "919999999999"
    # First call: not booked, asks for confirmation.
    first = repo.confirmation_gate(phone, "Asha Mehta", MON, "09:30", "turn-1")
    assert first["needs_confirmation"] is True and first["booked"] is False
    # Same turn again (model calling twice before the patient replied): still gated.
    assert repo.confirmation_gate(phone, "Asha Mehta", MON, "09:30", "turn-1")
    # Different details in a later turn: re-proposed, not booked.
    assert repo.confirmation_gate(phone, "Asha Mehta", MON, "10:00", "turn-2")
    # Same details as the latest proposal, in a later turn → go ahead (once).
    assert repo.confirmation_gate(phone, "dr. asha mehta", MON, "10:00", "turn-3") is None
    assert repo.confirmation_gate(phone, "Asha Mehta", MON, "10:00", "turn-4")


def test_confirmation_gate_passes_through_unbookable_slots(db):
    # create_appointment reports these, so the gate stays out of the way.
    assert repo.confirmation_gate("919999999999", "Nobody", MON, "09:30", "t") is None
    assert repo.confirmation_gate("919999999999", "Asha Mehta", SUN, "09:30", "t") is None


def test_rebooking_own_slot_is_idempotent(db):
    phone = "919999999999"
    first = repo.create_appointment(phone, "Ravi", 30, "Male", "Asha Mehta", MON, "09:30")
    again = repo.create_appointment(phone, "Ravi", 30, "Male", "Asha Mehta", MON, "09:30")
    assert again["success"] is True and again["alreadyBooked"] is True
    assert again["appointmentId"] == first["appointmentId"]
    assert len(repo.list_patient_appointments(phone)) == 1
    # Someone else still can't take it.
    other = repo.create_appointment("918888888888", "Asha", 40, "Female", "Asha Mehta", MON, "09:30")
    assert other["success"] is False


def test_today_hides_slots_that_already_started(db, monkeypatch):
    from datetime import datetime

    from clinic_agent.clinic_time import clinic_tz

    monday = datetime.fromisoformat(f"{MON}T09:45").replace(tzinfo=clinic_tz())
    monkeypatch.setattr(repo, "clinic_now", lambda: monday)
    monkeypatch.setattr(repo, "clinic_today", lambda: monday.date())
    res = repo.get_available_slots("Asha Mehta", MON)
    assert res["slots"] == ["10:00", "10:30"]
    booked = repo.create_appointment("919999999999", "Ravi", 30, "Male", "Asha Mehta", MON, "09:30")
    assert booked["success"] is False
