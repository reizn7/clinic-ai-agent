"""Clinic data access over MongoDB (synchronous pymongo).

Collections: clinic, doctors, patients, appointments. The booking tools call these
via ``asyncio.to_thread``. Ported from v1's Mongoose models/services.

Every query is scoped to this deployment's clinic (``settings.CLINIC_ID``) and every
insert stamps it — the console writes doctors/clinic info for many clinics into the
same database (see the clinic data contract). Clinic-configured extra fields live
in each document's ``attributes``; only agent-visible ones are returned
(``catalog.render_attributes``). Patient attributes are never read.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.errors import DuplicateKeyError

from clinic_agent.clinic_time import clinic_now, clinic_today
from clinic_agent.config import settings
from clinic_agent.db import create_index_safe, get_db
from clinic_agent.runtime import outbox
from clinic_agent.tools import catalog
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
    create_index_safe(
        get_db()["appointments"],
        [("doctorId", 1), ("date", 1), ("slot", 1)],
        unique=True,
        partialFilterExpression={"status": {"$in": ACTIVE_STATUSES}},
        name="uniq_active_slot",
    )
    # One patient per phone PER CLINIC (replaces the legacy unique {phone}, which
    # scripts/migrate_clinic_scope.py drops).
    create_index_safe(
        get_db()["patients"],
        [("clinicId", 1), ("phone", 1)],
        unique=True,
        name="uniq_clinic_phone",
    )
    create_index_safe(
        get_db()["doctors"],
        [("clinicId", 1), ("isActive", 1), ("name", 1)],
        name="clinic_active_name",
    )
    create_index_safe(get_db()["clinic"], "clinicId", unique=True, name="uniq_clinic")
    catalog.ensure_indexes()
    outbox.ensure_indexes()


def _followups(appointment_id: ObjectId, event: str) -> dict:
    """Queue calendar sync + patient confirmation; tell the model what to expect."""
    outbox.enqueue_appointment_change(appointment_id, event)
    if settings.WHATSAPP_CONFIRMATIONS_ENABLED:
        return {
            "whatsapp_confirmation": "A written confirmation will be sent to the patient on WhatsApp."
        }
    return {}


def _scoped(query: dict | None = None) -> dict:
    """``query`` restricted to this deployment's clinic."""
    return {**(query or {}), "clinicId": settings.CLINIC_ID}


def _weekday_name(date_key: str) -> str:
    # Python: Monday=0..Sunday=6; our WEEKDAYS list is Sunday-first (Date.getDay()).
    dt = datetime.strptime(date_key, "%Y-%m-%d")
    return WEEKDAYS[(dt.weekday() + 1) % 7]


def _today_key() -> str:
    return clinic_today().isoformat()


def get_clinic() -> dict:
    doc = get_db()["clinic"].find_one(_scoped()) or DEFAULT_CLINIC
    info = {
        "clinicName": doc.get("clinicName", ""),
        "timings": doc.get("timings", ""),
        "address": doc.get("address", ""),
        "phone": doc.get("phone", ""),
    }
    details = catalog.render_attributes(
        catalog.get_definitions().get("clinic"), doc.get("attributes"), where="clinic"
    )
    if details:
        info["details"] = details
    return info


def list_active_doctors() -> list[dict]:
    definition = catalog.get_definitions().get("doctor")
    docs = get_db()["doctors"].find(_scoped({"isActive": True})).sort("name", 1)
    out = []
    for d in docs:
        doctor = {
            "id": str(d["_id"]),
            "name": d["name"],
            "specialization": d.get("specialization", ""),
        }
        details = catalog.render_attributes(
            definition, d.get("attributes"), where=f"doctor:{d['_id']}"
        )
        if details:
            doctor["details"] = details
        out.append(doctor)
    return out


_DR_PREFIX = re.compile(r"^\s*(dr\.?|doctor)\s+", re.IGNORECASE)


def _find_doctor_by_name(name: str) -> dict | None:
    bare = _DR_PREFIX.sub("", name or "").strip()
    if not bare:
        return None
    return get_db()["doctors"].find_one(
        _scoped({"name": {"$regex": f"^{re.escape(bare)}$", "$options": "i"}, "isActive": True})
    )


def _schedule(doctor: dict) -> dict:
    """The doctor's regular week, so the agent can say which days they work
    instead of guessing from a handful of open dates."""
    days = set(doctor.get("availableDays") or [])
    hours = doctor.get("workingHours") or {}
    out: dict = {"workingDays": [d for d in (*WEEKDAYS[1:], WEEKDAYS[0]) if d in days]}
    if hours.get("start") and hours.get("end"):
        out["workingHours"] = f"{hours['start']}-{hours['end']}"
    return out


def get_available_slots(doctor_name: str, date_key: str) -> dict:
    doctor = _find_doctor_by_name(doctor_name)
    if not doctor:
        return {"error": f"No active doctor named '{doctor_name}'."}

    if date_key < _today_key():
        return {
            "doctor": doctor["name"],
            "date": date_key,
            "slots": [],
            "note": "That date is in the past. Please pick today or a future date.",
        }

    day = _weekday_name(date_key)
    if day not in (doctor.get("availableDays") or []):
        return {
            "doctor": doctor["name"],
            "date": date_key,
            "weekday": day,
            "slots": [],
            "note": f"Dr. {doctor['name']} does not work on {day}.",
            **_schedule(doctor),
        }

    all_slots = generate_slots(
        doctor.get("workingHours", {}), doctor.get("consultationDuration", 30)
    )
    booked = [
        a["slot"]
        for a in get_db()["appointments"].find(
            _scoped(
                {"doctorId": doctor["_id"], "date": date_key, "status": {"$in": ACTIVE_STATUSES}}
            ),
            {"slot": 1},
        )
    ]
    if date_key == _today_key():
        # Today: a slot that has already started can't be booked. "HH:mm" strings
        # compare correctly as text.
        now_hhmm = f"{clinic_now():%H:%M}"
        all_slots = [s for s in all_slots if s > now_hhmm]
    return {
        "doctor": doctor["name"],
        "date": date_key,
        "weekday": day,
        "slots": available_slots(all_slots, booked),
        **_schedule(doctor),
    }


def get_upcoming_availability(doctor_name: str, count: int = 3) -> dict:
    """Next `count` upcoming dates (from tomorrow) where the doctor has free slots."""
    doctor = _find_doctor_by_name(doctor_name)
    if not doctor:
        return {"error": f"No active doctor named '{doctor_name}'."}

    results: list[dict] = []
    cursor = clinic_today()
    guard = 0
    while len(results) < count and guard < 60:
        cursor += timedelta(days=1)
        guard += 1
        date_key = cursor.isoformat()
        if _weekday_name(date_key) not in (doctor.get("availableDays") or []):
            continue
        avail = get_available_slots(doctor_name, date_key)
        if avail.get("slots"):
            results.append(
                {
                    "date": date_key,
                    "weekday": avail["weekday"],
                    "slots": avail["slots"][:6],  # first few, to keep replies short
                }
            )
    return {"doctor": doctor["name"], **_schedule(doctor), "dates": results}


def _upsert_patient(
    phone: str, name: str, age: int, gender: str, *, update_profile: bool = True
) -> ObjectId:
    """Find-or-create the patient on ``phone``.

    ``update_profile=False`` (an unverified voice caller) never changes an existing
    patient's name/age/gender; they are only written when the patient is new.
    """
    now = datetime.now(UTC)
    details = {"name": name, "age": age, "gender": gender}
    on_insert = {"phone": phone, "clinicId": settings.CLINIC_ID, "createdAt": now}
    if update_profile:
        update = {"$set": {**details, "updatedAt": now}, "$setOnInsert": on_insert}
    else:
        update = {"$setOnInsert": {**on_insert, **details, "updatedAt": now}}
    res = get_db()["patients"].find_one_and_update(
        _scoped({"phone": phone}),
        update,
        upsert=True,
        return_document=True,
    )
    return res["_id"]


def create_appointment(
    phone: str,
    name: str,
    age: int,
    gender: str,
    doctor_name: str,
    date_key: str,
    slot: str,
    *,
    update_profile: bool = True,
) -> dict:
    doctor = _find_doctor_by_name(doctor_name)
    if not doctor:
        return {"success": False, "error": f"No active doctor named '{doctor_name}'."}

    # Idempotent: a repeat call for a slot this patient already holds returns
    # that appointment instead of "slot just taken" (their own booking).
    mine = _own_booking(phone, doctor["_id"], date_key, slot)
    if mine:
        return {
            "success": True,
            "alreadyBooked": True,
            "appointmentId": str(mine["_id"]),
            "doctor": doctor["name"],
            "date": date_key,
            "weekday": _weekday_name(date_key),
            "slot": slot,
            "status": mine.get("status", "Pending"),
        }

    # Validate the slot is genuinely bookable (future, a working day, a real free
    # slot) before writing — clearer than relying on the unique index alone.
    avail = get_available_slots(doctor_name, date_key)
    if slot not in avail.get("slots", []):
        return {
            "success": False,
            "error": avail.get("note")
            or f"{slot} isn't an available slot for Dr. {doctor['name']} on {date_key}.",
            "available_slots": avail.get("slots", []),
        }

    patient_id = _upsert_patient(phone, name, age, gender, update_profile=update_profile)
    try:
        res = get_db()["appointments"].insert_one(
            {
                "clinicId": settings.CLINIC_ID,
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
        **_followups(res.inserted_id, "booked"),
    }


def _own_booking(phone: str, doctor_id, date_key: str, slot: str) -> dict | None:
    patient = get_db()["patients"].find_one(_scoped({"phone": phone}), {"_id": 1})
    if not patient:
        return None
    return get_db()["appointments"].find_one(
        _scoped(
            {
                "patientId": patient["_id"],
                "doctorId": doctor_id,
                "date": date_key,
                "slot": slot,
                "status": {"$in": ACTIVE_STATUSES},
            }
        )
    )


# --- Booking confirmation gate (WhatsApp) -----------------------------------
# The prompt says "read back, wait for yes, then book", but a small model can
# book and ask in the same turn. So the first book_appointment for a slot only
# records a proposal; it books when called again, for the same doctor/date/slot,
# in a LATER turn (i.e. after the patient has replied).

PROPOSAL_TTL = timedelta(minutes=30)


def confirmation_gate(
    phone: str, doctor_name: str, date_key: str, slot: str, turn_id: str
) -> dict | None:
    """``None`` → go ahead and book. A dict → NOT booked; ask the patient first."""
    doctor = _find_doctor_by_name(doctor_name)
    if not doctor or slot not in get_available_slots(doctor_name, date_key).get("slots", []):
        # Unknown doctor / unavailable or already-own slot: create_appointment
        # reports it (or returns the existing booking).
        return None

    col = get_db()["booking_proposals"]
    query = _scoped({"phone": phone})
    now = datetime.now(UTC)
    prop = col.find_one(query)
    if prop:
        created = prop["createdAt"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if (
            (prop["doctorId"], prop["date"], prop["slot"]) == (doctor["_id"], date_key, slot)
            and prop["turnId"] != turn_id
            and created > now - PROPOSAL_TTL
        ):
            col.delete_one({"_id": prop["_id"]})
            return None

    col.update_one(
        query,
        {
            "$set": {
                "doctorId": doctor["_id"],
                "date": date_key,
                "slot": slot,
                "turnId": turn_id,
                "createdAt": now,
            }
        },
        upsert=True,
    )
    return {
        "success": False,
        "booked": False,
        "needs_confirmation": True,
        "doctor": doctor["name"],
        "date": date_key,
        "weekday": _weekday_name(date_key),
        "slot": slot,
        "message": (
            "NOT booked yet — the slot is free. Read the details back to the patient "
            "and ask them to confirm. Do not say it is booked. When they say yes in "
            "their next message, call book_appointment again with the same doctor, "
            "date and slot."
        ),
    }


def list_patient_appointments(phone: str) -> list[dict]:
    patient = get_db()["patients"].find_one(_scoped({"phone": phone}))
    if not patient:
        return []
    doctors = {d["_id"]: d["name"] for d in get_db()["doctors"].find(_scoped(), {"name": 1})}
    appts = (
        get_db()["appointments"]
        .find(_scoped({"patientId": patient["_id"], "status": {"$in": ACTIVE_STATUSES}}))
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


def _owned(oid: ObjectId, phone: str | None) -> dict:
    """Query for appointment ``oid``, restricted to the patient on ``phone`` when given.

    An appointment on someone else's number then simply reads as "not found", so
    a caller cannot act on (or learn about) another patient's appointment.
    """
    query = {"_id": oid}
    if phone is not None:
        patient = get_db()["patients"].find_one(_scoped({"phone": phone}), {"_id": 1})
        query["patientId"] = patient["_id"] if patient else None
    return _scoped(query)


def cancel_appointment(appointment_id: str, phone: str | None = None) -> dict:
    """Cancel an appointment. With ``phone``, only one belonging to that patient."""
    try:
        oid = ObjectId(appointment_id)
    except InvalidId, TypeError:
        return {"success": False, "error": "Invalid appointment id."}
    owned = _owned(oid, phone)
    now = datetime.now(UTC)
    res = get_db()["appointments"].update_one(
        {**owned, "status": {"$ne": "Cancelled"}},
        {"$set": {"status": "Cancelled", "cancelledAt": now, "updatedAt": now}},
    )
    if res.matched_count == 0:
        if get_db()["appointments"].find_one(owned):
            # Already cancelled: idempotent success, no second notification.
            return {"success": True, "appointmentId": appointment_id, "status": "Cancelled"}
        return {"success": False, "error": "Appointment not found."}
    return {
        "success": True,
        "appointmentId": appointment_id,
        "status": "Cancelled",
        **_followups(oid, "cancelled"),
    }


def reschedule_appointment(
    appointment_id: str, new_date: str, new_slot: str, phone: str | None = None
) -> dict:
    """Move an existing (non-cancelled) appointment to a new date + slot. With
    ``phone``, only one belonging to that patient."""
    try:
        oid = ObjectId(appointment_id)
    except InvalidId, TypeError:
        return {"success": False, "error": "Invalid appointment id."}

    appt = get_db()["appointments"].find_one(_owned(oid, phone))
    if not appt or appt["status"] == "Cancelled":
        return {"success": False, "error": "Appointment not found."}
    doctor = get_db()["doctors"].find_one(_scoped({"_id": appt["doctorId"]}))
    if not doctor:
        return {"success": False, "error": "That doctor is no longer available."}

    avail = get_available_slots(doctor["name"], new_date)
    if new_slot not in avail.get("slots", []):
        return {
            "success": False,
            "error": avail.get("note")
            or f"{new_slot} isn't available for Dr. {doctor['name']} on {new_date}.",
            "available_slots": avail.get("slots", []),
        }

    try:
        get_db()["appointments"].update_one(
            _scoped({"_id": oid}),
            {"$set": {"date": new_date, "slot": new_slot, "updatedAt": datetime.now(UTC)}},
        )
    except DuplicateKeyError:
        return {"success": False, "conflict": True, "error": "That slot has just been taken."}
    return {
        "success": True,
        "appointmentId": appointment_id,
        "doctor": doctor["name"],
        "date": new_date,
        "weekday": _weekday_name(new_date),
        "slot": new_slot,
        "status": appt["status"],
        **_followups(oid, "rescheduled"),
    }
