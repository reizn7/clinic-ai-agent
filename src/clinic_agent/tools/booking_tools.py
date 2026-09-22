"""ADK tools the receptionist agent can call.

Thin async wrappers over ``repo`` (sync pymongo run in a thread). ADK builds each
tool's schema from the function signature + docstring, so keep those accurate.
"""

from __future__ import annotations

import asyncio
import logging

from clinic_agent.tools import repo

log = logging.getLogger("clinic_agent.tools")


async def get_clinic_info() -> dict:
    """Get the clinic's name, opening timings, address, and contact phone number."""
    return await asyncio.to_thread(repo.get_clinic)


async def list_doctors() -> dict:
    """List the clinic's currently available doctors and their specializations."""
    doctors = await asyncio.to_thread(repo.list_active_doctors)
    return {"doctors": doctors}


async def check_availability(doctor_name: str, date: str) -> dict:
    """Check a doctor's open appointment slots on a given date.

    Args:
        doctor_name: The doctor's name (without the "Dr." prefix).
        date: The date to check, formatted as YYYY-MM-DD.
    """
    return await asyncio.to_thread(repo.get_available_slots, doctor_name, date)


async def book_appointment(
    patient_name: str,
    age: int,
    gender: str,
    doctor_name: str,
    date: str,
    slot: str,
    phone: str,
) -> dict:
    """Book an appointment after the patient has confirmed the details.

    Args:
        patient_name: The patient's full name.
        age: The patient's age in years.
        gender: The patient's gender (Male, Female, or Other).
        doctor_name: The chosen doctor's name (without the "Dr." prefix).
        date: The appointment date, formatted as YYYY-MM-DD.
        slot: The chosen slot start time, formatted as HH:mm.
        phone: The patient's WhatsApp phone number (from the conversation).
    """
    return await asyncio.to_thread(
        repo.create_appointment, phone, patient_name, age, gender, doctor_name, date, slot
    )


async def list_my_appointments(phone: str) -> dict:
    """List the caller's upcoming (non-cancelled) appointments.

    Args:
        phone: The patient's WhatsApp phone number (from the conversation).
    """
    appts = await asyncio.to_thread(repo.list_patient_appointments, phone)
    return {"appointments": appts}


async def cancel_appointment(appointment_id: str) -> dict:
    """Cancel an existing appointment by its id.

    Args:
        appointment_id: The appointment id (from list_my_appointments).
    """
    return await asyncio.to_thread(repo.cancel_appointment, appointment_id)


async def escalate_to_human(reason: str) -> dict:
    """Hand off to the clinic's human team when the patient asks for a human or
    has an urgent/clinical concern the agent cannot handle.

    Args:
        reason: A short description of why a human is needed.
    """
    log.info("Escalation requested: %s", reason)
    return {
        "escalated": True,
        "message": (
            "I've let our clinic team know — someone will follow up with you here shortly."
        ),
    }


ALL_TOOLS = [
    get_clinic_info,
    list_doctors,
    check_availability,
    book_appointment,
    list_my_appointments,
    cancel_appointment,
    escalate_to_human,
]
