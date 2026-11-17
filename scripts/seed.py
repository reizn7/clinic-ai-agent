"""Seed the clinic DB with one clinic + a couple of doctors, and create indexes.

Everything is stamped with this deployment's CLINIC_ID. It also adds an example
of clinic-configured data (clinic data contract): extra doctor fields and a "Lab
tests" catalog. Those are inserted only if missing, so edits made in the console
are never overwritten.

Run with:  uv run python scripts/seed.py
Idempotent: upserts by natural key; does not touch patients or appointments.
"""

from __future__ import annotations

from datetime import UTC, datetime

from dotenv import load_dotenv

load_dotenv()

from clinic_agent.config import settings  # noqa: E402
from clinic_agent.db import get_db  # noqa: E402
from clinic_agent.runtime import conversations, debounce  # noqa: E402
from clinic_agent.tools import catalog  # noqa: E402
from clinic_agent.tools.repo import ensure_indexes  # noqa: E402

CLINIC = {
    "clinicName": "ABC Clinic",
    "timings": "Mon-Sat, 9:00 AM - 5:00 PM",
    "address": "123 Health Street, Wellness City",
    "phone": "+91-00000-00000",
    "slotDuration": 30,
}

DOCTORS = [
    {
        "name": "Asha Mehta",
        "specialization": "General Physician",
        "consultationDuration": 30,
        "availableDays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        "workingHours": {"start": "09:00", "end": "13:00"},
        "isActive": True,
    },
    {
        "name": "Rohit Sharma",
        "specialization": "Dentist",
        "consultationDuration": 20,
        "availableDays": ["Monday", "Wednesday", "Friday", "Saturday"],
        "workingHours": {"start": "14:00", "end": "17:00"},
        "isActive": True,
    },
]


def _field(key: str, label: str, type_: str, order: int, **kw) -> dict:
    return {
        "key": key,
        "label": label,
        "type": type_,
        "required": kw.pop("required", False),
        "options": kw.pop("options", []),
        "description": kw.pop("description", ""),
        "agentVisible": kw.pop("agentVisible", True),
        "filterable": kw.pop("filterable", False),
        "pii": kw.pop("pii", False),
        "archived": False,
        "order": order,
    }


DEFINITIONS = [
    {
        "entityType": "doctor",
        "kind": "system",
        "label": "Doctors",
        "singularLabel": "Doctor",
        "description": "Doctors patients can book.",
        "fields": [
            _field("degrees", "Degrees", "tags", 0),
            _field("experience", "Experience (years)", "number", 1),
            _field(
                "languages",
                "Languages",
                "multiselect",
                2,
                options=["English", "Hindi", "Marathi"],
                filterable=True,
            ),
            _field("email", "Email", "email", 3, agentVisible=False, pii=True),
        ],
    },
    {
        "entityType": "lab_test",
        "kind": "custom",
        "label": "Lab tests",
        "singularLabel": "Lab test",
        "agentEnabled": True,
        "titleField": "name",
        "description": "Pathology tests done at the clinic, with price, fasting and report time.",
        "fields": [
            _field("name", "Name", "text", 0, required=True),
            _field("price", "Price (INR)", "number", 1),
            _field("fastingRequired", "Fasting required", "boolean", 2),
            _field("reportTime", "Report time", "text", 3),
        ],
    },
]

DOCTOR_ATTRIBUTES = {
    "Asha Mehta": {
        "degrees": ["MBBS", "MD (Medicine)"],
        "experience": 12,
        "languages": ["English", "Hindi"],
    },
    "Rohit Sharma": {"degrees": ["BDS", "MDS"], "experience": 8, "languages": ["English"]},
}

LAB_TESTS = [
    {
        "name": "Complete blood count (CBC)",
        "price": 350,
        "fastingRequired": False,
        "reportTime": "Same day",
    },
    {
        "name": "Blood sugar fasting",
        "price": 120,
        "fastingRequired": True,
        "reportTime": "Same day",
    },
    {
        "name": "Thyroid profile (T3, T4, TSH)",
        "price": 650,
        "fastingRequired": False,
        "reportTime": "Next day",
    },
    {"name": "Lipid profile", "price": 600, "fastingRequired": True, "reportTime": "Next day"},
]


def main() -> None:
    db = get_db()
    clinic_id = settings.CLINIC_ID
    now = datetime.now(UTC)
    db["clinic"].update_one(
        {"clinicId": clinic_id}, {"$set": {**CLINIC, "clinicId": clinic_id}}, upsert=True
    )
    for doc in DOCTORS:
        db["doctors"].update_one(
            {"clinicId": clinic_id, "name": doc["name"], "specialization": doc["specialization"]},
            {
                "$set": {**doc, "clinicId": clinic_id},
                "$setOnInsert": {
                    "attributes": DOCTOR_ATTRIBUTES.get(doc["name"], {}),
                    "createdAt": now,
                },
            },
            upsert=True,
        )

    # Example clinic-configured data — only if missing (never clobber console edits).
    iso = now.isoformat()
    for d in DEFINITIONS:
        db[catalog.DEFINITIONS].update_one(
            {"clinicId": clinic_id, "entityType": d["entityType"]},
            {
                "$setOnInsert": {
                    **d,
                    "clinicId": clinic_id,
                    "version": 1,
                    "createdAt": iso,
                    "updatedAt": iso,
                }
            },
            upsert=True,
        )
    for test in LAB_TESTS:
        db[catalog.RECORDS].update_one(
            {"clinicId": clinic_id, "entityType": "lab_test", "attributes.name": test["name"]},
            {
                "$setOnInsert": {
                    "clinicId": clinic_id,
                    "entityType": "lab_test",
                    "isActive": True,
                    "attributes": test,
                    "createdAt": iso,
                    "updatedAt": iso,
                }
            },
            upsert=True,
        )
    ensure_indexes()
    conversations.ensure_indexes()
    debounce.ensure_indexes()
    print(
        f"Seed complete (db='{db.name}', clinicId='{clinic_id}'): 1 clinic, "
        f"{len(DOCTORS)} doctors, {len(DEFINITIONS)} definitions, {len(LAB_TESTS)} lab tests "
        "(examples inserted only if missing), indexes ensured."
    )


if __name__ == "__main__":
    main()
