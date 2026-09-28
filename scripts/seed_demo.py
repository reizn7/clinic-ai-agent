"""Rich demo dataset for evaluations/demos.

Seeds a realistic multi-specialty clinic, doctors with varied schedules, a few
sample patients, and existing appointments on upcoming working days (so
availability shows real occupancy and "my appointments" has data).

Run:  uv run python scripts/seed_demo.py
It RESETS the runtime collections (conversations, transcripts, patients,
appointments, pending_messages) and re-seeds clinic + doctors + sample data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from clinic_agent.db import get_db  # noqa: E402
from clinic_agent.runtime import conversations, debounce  # noqa: E402
from clinic_agent.tools.repo import ensure_indexes  # noqa: E402
from clinic_agent.tools.slots import generate_slots  # noqa: E402

# Change this to your own WhatsApp number to demo "list my appointments".
DEMO_PATIENT_PHONE = "919625561517"

WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

CLINIC = {
    "clinicName": "CityCare Multi-Speciality Clinic",
    "timings": "Mon-Sat, 9:00 AM - 8:00 PM (Sunday closed)",
    "address": "2nd Floor, Trade Centre, MG Road, Bengaluru 560001",
    "phone": "+91-80-4567-8900",
    "slotDuration": 30,
}

DOCTORS = [
    {
        "name": "Asha Mehta",
        "specialization": "General Physician",
        "consultationDuration": 20,
        "availableDays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        "workingHours": {"start": "09:00", "end": "13:00"},
    },
    {
        "name": "Rohit Sharma",
        "specialization": "Dentist",
        "consultationDuration": 30,
        "availableDays": ["Monday", "Wednesday", "Friday", "Saturday"],
        "workingHours": {"start": "14:00", "end": "18:00"},
    },
    {
        "name": "Priya Nair",
        "specialization": "Pediatrician",
        "consultationDuration": 15,
        "availableDays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
        "workingHours": {"start": "10:00", "end": "14:00"},
    },
    {
        "name": "Vikram Rao",
        "specialization": "Cardiologist",
        "consultationDuration": 30,
        "availableDays": ["Tuesday", "Thursday", "Saturday"],
        "workingHours": {"start": "11:00", "end": "15:00"},
    },
    {
        "name": "Sneha Kulkarni",
        "specialization": "Dermatologist",
        "consultationDuration": 20,
        "availableDays": ["Monday", "Wednesday", "Friday"],
        "workingHours": {"start": "15:00", "end": "19:00"},
    },
    {
        "name": "Arjun Menon",
        "specialization": "Orthopedic Surgeon",
        "consultationDuration": 20,
        "availableDays": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        "workingHours": {"start": "16:00", "end": "20:00"},
    },
    {
        "name": "Fatima Sheikh",
        "specialization": "Gynecologist",
        "consultationDuration": 30,
        "availableDays": ["Tuesday", "Thursday", "Saturday"],
        "workingHours": {"start": "09:00", "end": "13:00"},
    },
]

PATIENTS = [
    {"phone": "919810011111", "name": "Ravi Deshpande", "age": 34, "gender": "Male"},
    {"phone": "919810022222", "name": "Meera Iyer", "age": 29, "gender": "Female"},
    {"phone": "919810033333", "name": "Sanjay Gupta", "age": 51, "gender": "Male"},
    {"phone": DEMO_PATIENT_PHONE, "name": "Piyush Poddar", "age": 24, "gender": "Male"},
]


def _upcoming_working_dates(available_days: list[str], count: int) -> list[str]:
    out: list[str] = []
    cursor = date.today()
    for _ in range(60):
        cursor += timedelta(days=1)
        if WEEKDAYS[(cursor.weekday() + 1) % 7] in available_days:
            out.append(cursor.isoformat())
            if len(out) >= count:
                break
    return out


def main() -> None:
    db = get_db()
    db.client.admin.command("ping")

    # Fresh runtime state.
    for c in ["conversations", "transcripts", "patients", "appointments", "pending_messages"]:
        db[c].delete_many({})
    db["clinic"].delete_many({})
    db["doctors"].delete_many({})

    # Clinic + doctors.
    db["clinic"].insert_one(dict(CLINIC))
    now = datetime.now(UTC)
    for doc in DOCTORS:
        db["doctors"].insert_one({**doc, "isActive": True, "createdAt": now, "updatedAt": now})
    doctors = {d["name"]: d for d in db["doctors"].find()}

    # Patients.
    for p in PATIENTS:
        db["patients"].insert_one({**p, "createdAt": now, "updatedAt": now})
    patients = {p["phone"]: db["patients"].find_one({"phone": p["phone"]}) for p in PATIENTS}

    # Sample appointments: partially fill each doctor's next working day, plus one
    # on the demo patient's number so "my appointments" has data.
    appts = 0

    def book(doctor_name: str, phone: str, date_key: str, slot: str, status: str = "Confirmed"):
        nonlocal appts
        doc = doctors[doctor_name]
        db["appointments"].insert_one(
            {
                "patientId": patients[phone]["_id"],
                "doctorId": doc["_id"],
                "date": date_key,
                "slot": slot,
                "status": status,
                "createdAt": now,
                "updatedAt": now,
            }
        )
        appts += 1

    for name, doc in doctors.items():
        dates = _upcoming_working_dates(doc["availableDays"], 1)
        if not dates:
            continue
        slots = generate_slots(doc["workingHours"], doc["consultationDuration"])
        # Take the first two slots as already-booked, spread across sample patients.
        for i, slot in enumerate(slots[:2]):
            phone = PATIENTS[i % 3]["phone"]  # rotate the first three patients
            book(name, phone, dates[0], slot)

    # A clear upcoming appointment for the demo patient.
    asha_dates = _upcoming_working_dates(doctors["Asha Mehta"]["availableDays"], 1)
    if asha_dates:
        book("Asha Mehta", DEMO_PATIENT_PHONE, asha_dates[0], "11:00", status="Confirmed")

    ensure_indexes()
    conversations.ensure_indexes()
    debounce.ensure_indexes()

    print(
        f"Demo seed complete (db='{db.name}'):\n"
        f"  clinic: {CLINIC['clinicName']}\n"
        f"  doctors: {len(DOCTORS)}\n"
        f"  patients: {len(PATIENTS)}\n"
        f"  appointments: {appts}\n"
        f"  demo patient ({DEMO_PATIENT_PHONE}) has an appointment with Dr. Asha Mehta."
    )


if __name__ == "__main__":
    main()
