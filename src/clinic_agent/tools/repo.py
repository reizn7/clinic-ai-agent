"""Clinic data access over MongoDB (synchronous pymongo).

Collections: clinic, doctors, patients, appointments. The booking tools call these
via ``asyncio.to_thread``. Ported from v1's Mongoose models/services.
"""

from __future__ import annotations

from datetime import UTC, datetime

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.errors import DuplicateKeyError

from clinic_agent.db import get_db
from clinic_agent.tools.slots import available_slots, generate_slots

WEEKDAYS = [
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
]

ACTIVE_STATUSES = ["Pending", "Confirmed", "Arrived", "Completed"]

DEFAULT_CLINIC = {
    "clinicName": "ABC Clinic",
    "timings": "Mon-Sat, 9:00 AM - 5:00 PM",
    "address": "Address not configured yet.",
    "phone": "Reception number not configured yet.",
}


def ensure_indexes() -> None:
    """Create the double-booking guard: unique (doctorId, date, slot) for
    non-cancelled appointments. Safe to call repeatedly."""
    # Partial indexes only allow a limited operator set — $nin/$not is rejected,
    # so express "non-cancelled" as $in over the active statuses (equivalent).
    get_db()["appointments"].create_index(
        [("doctorId", 1), ("date", 1), ("slot", 1)],
        unique=True,
        partialFilterExpression={"status": {"$in": ACTIVE_STATUSES}},
        name="uniq_active_slot",
    )
    get_db()["patients"].create_index("phone", unique=True, name="uniq_phone")


def _weekday_name(date_key: str) -> str:
    # Python: Monday=0..Sunday=6; our WEEKDAYS list is Sunday-first (Date.getDay()).
    dt = datetime.strptime(date_key, "%Y-%m-%d")
    return WEEKDAYS[(dt.weekday() + 1) % 7]


def get_clinic() -> dict:
    doc = get_db()["clinic"].find_one() or DEFAULT_CLINIC
    return {
        "clinicName": doc.get("clinicName", ""),
        "timings": doc.get("timings", ""),
        "address": doc.get("address", ""),
        "phone": doc.get("phone", ""),
    }


def list_active_doctors() -> list[dict]:
    docs = get_db()["doctors"].find({"isActive": True}).sort("name", 1)
    return [
        {
            "id": str(d["_id"]),
            "name": d["name"],
            "specialization": d.get("specialization", ""),
        }
        for d in docs
    ]


def _find_doctor_by_name(name: str) -> dict | None:
    return get_db()["doctors"].find_one(
        {"name": {"$regex": f"^{name}$", "$options": "i"}, "isActive": True}
    )


def get_available_slots(doctor_name: str, date_key: str) -> dict:
    doctor = _find_doctor_by_name(doctor_name)
    if not doctor:
        return {"error": f"No active doctor named '{doctor_name}'."}

    day = _weekday_name(date_key)
    if day not in (doctor.get("availableDays") or []):
        return {
            "doctor": doctor["name"],
            "date": date_key,
            "weekday": day,
            "slots": [],
            "note": f"Dr. {doctor['name']} does not work on {day}.",
        }

    all_slots = generate_slots(
        doctor.get("workingHours", {}), doctor.get("consultationDuration", 30)
    )
    booked = [
        a["slot"]
        for a in get_db()["appointments"].find(
            {"doctorId": doctor["_id"], "date": date_key, "status": {"$in": ACTIVE_STATUSES}},
            {"slot": 1},
        )
    ]
    return {
        "doctor": doctor["name"],
        "date": date_key,
        "weekday": day,
        "slots": available_slots(all_slots, booked),
    }


def _upsert_patient(phone: str, name: str, age: int, gender: str) -> ObjectId:
    now = datetime.now(UTC)
    res = get_db()["patients"].find_one_and_update(
        {"phone": phone},
        {
            "$set": {"name": name, "age": age, "gender": gender, "updatedAt": now},
            "$setOnInsert": {"phone": phone, "createdAt": now},
        },
        upsert=True,
        return_document=True,
    )
    return res["_id"]


def create_appointment(
    phone: str, name: str, age: int, gender: str, doctor_name: str, date_key: str, slot: str
) -> dict:
    doctor = _find_doctor_by_name(doctor_name)
    if not doctor:
        return {"success": False, "error": f"No active doctor named '{doctor_name}'."}

    patient_id = _upsert_patient(phone, name, age, gender)
    try:
        res = get_db()["appointments"].insert_one(
            {
                "patientId": patient_id,
                "doctorId": doctor["_id"],
                "date": date_key,
                "slot": slot,
                "status": "Pending",
                "createdAt": datetime.now(UTC),
                "updatedAt": datetime.now(UTC),
            }
        )
    except DuplicateKeyError:
        return {
            "success": False,
            "conflict": True,
            "error": "That slot has just been booked. Please pick another.",
        }
    return {
        "success": True,
        "appointmentId": str(res.inserted_id),
        "doctor": doctor["name"],
        "date": date_key,
        "weekday": _weekday_name(date_key),
        "slot": slot,
        "status": "Pending",
    }


def list_patient_appointments(phone: str) -> list[dict]:
    patient = get_db()["patients"].find_one({"phone": phone})
    if not patient:
        return []
    doctors = {d["_id"]: d["name"] for d in get_db()["doctors"].find({}, {"name": 1})}
    appts = (
        get_db()["appointments"]
        .find({"patientId": patient["_id"], "status": {"$in": ACTIVE_STATUSES}})
        .sort([("date", 1), ("slot", 1)])
    )
    return [
        {
            "appointmentId": str(a["_id"]),
            "doctor": doctors.get(a["doctorId"], "Unknown"),
            "date": a["date"],
            "slot": a["slot"],
            "status": a["status"],
        }
        for a in appts
    ]


def cancel_appointment(appointment_id: str) -> dict:
    try:
        oid = ObjectId(appointment_id)
    except (InvalidId, TypeError):
        return {"success": False, "error": "Invalid appointment id."}
    now = datetime.now(UTC)
    res = get_db()["appointments"].update_one(
        {"_id": oid},
        {"$set": {"status": "Cancelled", "cancelledAt": now, "updatedAt": now}},
    )
    if res.matched_count == 0:
        return {"success": False, "error": "Appointment not found."}
    return {"success": True, "appointmentId": appointment_id, "status": "Cancelled"}
