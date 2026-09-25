"""Seed the clinic DB with one clinic + a couple of doctors, and create indexes.

Run with:  uv run python scripts/seed.py
Idempotent: upserts by natural key; does not touch patients or appointments.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

from clinic_agent.db import get_db  # noqa: E402
from clinic_agent.runtime import conversations, debounce  # noqa: E402
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


def main() -> None:
    db = get_db()
    db["clinic"].update_one(
        {"clinicName": CLINIC["clinicName"]}, {"$set": CLINIC}, upsert=True
    )
    for doc in DOCTORS:
        db["doctors"].update_one(
            {"name": doc["name"], "specialization": doc["specialization"]},
            {"$set": doc},
            upsert=True,
        )
    ensure_indexes()
    conversations.ensure_indexes()
    debounce.ensure_indexes()
    print(
        f"Seed complete: 1 clinic, {len(DOCTORS)} doctors, indexes ensured "
        f"(db='{db.name}')."
    )


if __name__ == "__main__":
    main()
