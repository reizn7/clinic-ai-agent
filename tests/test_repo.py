import mongomock
import pytest
from bson import ObjectId

from clinic_agent.tools import repo


@pytest.fixture
def db(monkeypatch):
    client = mongomock.MongoClient()
    fake = client["clinic-test"]
    monkeypatch.setattr(repo, "get_db", lambda: fake)
    # One active doctor: works Mon-Fri 09:00-11:00, 30-min slots.
    fake["doctors"].insert_one(
        {
            "name": "Asha Mehta",
            "specialization": "General Physician",
            "consultationDuration": 30,
            "availableDays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
            "workingHours": {"start": "09:00", "end": "11:00"},
            "isActive": True,
        }
    )
    return fake


# 2026-08-10 is a Monday; 2026-08-09 is a Sunday.
MON = "2026-08-10"
SUN = "2026-08-09"


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


def test_cancel_stamps_cancelled_at(db):
    booked = repo.create_appointment(
        "919999999999", "Ravi", 30, "Male", "Asha Mehta", MON, "09:30"
    )
    repo.cancel_appointment(booked["appointmentId"])
    appt = db["appointments"].find_one({"_id": ObjectId(booked["appointmentId"])})
    assert "cancelledAt" in appt
