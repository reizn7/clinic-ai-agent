"""ADK tools the receptionist agent can call.

Thin async wrappers over ``repo`` (sync pymongo run in a thread). ADK builds each
tool's schema from the function signature + docstring, so keep those accurate.

**Caller identity comes from the session, never from the model.** The harness puts
the caller's number in the ADK session state (``CALLER_PHONE``) when it creates the
session: the WhatsApp sender, or the number typed on the voice call page. Tools
read it from ``tool_context``; there is no ``phone`` argument the model could fill
in with someone else's number. ADK injects ``tool_context`` and leaves it out of
the tool schema.

**Voice callers must verify before touching existing appointments.** A WhatsApp
sender is proven by Meta; a voice caller's number is self-entered. On voice,
``list_my_appointments`` / ``reschedule_appointment`` / ``cancel_appointment``
refuse until ``verify_caller`` accepts the security code from the patient's
WhatsApp confirmation, except for appointments booked earlier in the same call.
"""

from __future__ import annotations

import asyncio
import logging

from google.adk.tools import ToolContext

from clinic_agent.runtime import escalation
from clinic_agent.tools import access_code, repo
from clinic_agent.tools import catalog as catalog_data

log = logging.getLogger("clinic_agent.tools")

# ADK session-state keys, set by the harness (runtime.turn / voice.bridge).
CALLER_PHONE = "caller_phone"
CHANNEL = "channel"
CONVERSATION_ID = "conversation_id"  # conversations.externalId, when known
# Set by the tools themselves during a voice call.
VERIFIED = "caller_verified"
VERIFY_ATTEMPTS = "verify_attempts"
BOOKED_THIS_CALL = "booked_this_call"

MAX_ATTEMPTS_PER_CALL = 3

_NO_CALLER = {
    "success": False,
    "error": "The caller's phone number is unknown, so this can't be done here.",
}
_VERIFY_FIRST = {
    "success": False,
    "verification_required": True,
    "error": (
        "Not verified. Ask the caller for the 6-digit security code from their "
        "WhatsApp booking confirmation, then call verify_caller."
    ),
}


def _caller(tool_context: ToolContext) -> str:
    return str(tool_context.state.get(CALLER_PHONE) or "")


def _verified(tool_context: ToolContext) -> bool:
    """WhatsApp senders are verified by Meta; voice callers by verify_caller."""
    if tool_context.state.get(CHANNEL) != "voice":
        return True
    return bool(tool_context.state.get(VERIFIED))


def _booked_this_call(tool_context: ToolContext) -> list[str]:
    return list(tool_context.state.get(BOOKED_THIS_CALL) or [])


async def get_clinic_info(detail: str = "") -> dict:
    """Get the clinic's name, opening timings, address and contact phone number,
    plus ``available_details``: the names of the other details the clinic has
    (e.g. insurers / TPAs accepted, payment modes, facilities, specialities,
    visiting hours, parking). Call again with ``detail`` to read one of them.

    Args:
        detail: Optional. The name of one detail to read (e.g. "insurance",
            "payment modes", "parking"); empty for the basic info.
    """
    return await asyncio.to_thread(repo.get_clinic, detail)


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


async def find_next_available(doctor_name: str) -> dict:
    """Find the next upcoming dates a doctor has open slots, when the patient
    isn't sure of an exact date (e.g. "sometime this week"). Returns a few dates
    with their free slots.

    Args:
        doctor_name: The doctor's name (without the "Dr." prefix).
    """
    return await asyncio.to_thread(repo.get_upcoming_availability, doctor_name, 3)


async def book_appointment(
    patient_name: str,
    age: int,
    gender: str,
    doctor_name: str,
    date: str,
    slot: str,
    tool_context: ToolContext,
) -> dict:
    """Book an appointment. The caller's phone number is attached automatically.

    On WhatsApp the first call for a slot does NOT book: it checks the slot and
    returns needs_confirmation. Read the details back, and once the patient says
    yes in their next message, call again with the same details to book.

    Args:
        patient_name: The patient's full name.
        age: The patient's age in years.
        gender: The patient's gender (Male, Female, or Other).
        doctor_name: The chosen doctor's name (without the "Dr." prefix).
        date: The appointment date, formatted as YYYY-MM-DD.
        slot: The chosen slot start time, formatted as HH:mm.
    """
    phone = _caller(tool_context)
    if not phone:
        return _NO_CALLER
    if tool_context.state.get(CHANNEL) != "voice":
        # WhatsApp: one turn per patient message (invocation_id), so the gate can
        # require a reply between the read-back and the booking.
        gate = await asyncio.to_thread(
            repo.confirmation_gate, phone, doctor_name, date, slot, tool_context.invocation_id
        )
        if gate:
            return gate
    result = await asyncio.to_thread(
        repo.create_appointment,
        phone,
        patient_name,
        age,
        gender,
        doctor_name,
        date,
        slot,
        # An unverified voice caller may book, but never rewrites an existing
        # patient's name/age/gender.
        update_profile=_verified(tool_context),
    )
    if result.get("success"):
        tool_context.state[BOOKED_THIS_CALL] = [
            *_booked_this_call(tool_context),
            result["appointmentId"],
        ]
    return result


async def verify_caller(code: str, tool_context: ToolContext) -> dict:
    """Verify a phone caller with the 6-digit security code from their WhatsApp
    booking confirmation. Needed on calls before looking up, rescheduling or
    cancelling existing appointments.

    Args:
        code: The digits the caller said, e.g. "482913".
    """
    if _verified(tool_context):
        return {"verified": True}
    phone = _caller(tool_context)
    if not phone:
        return {**_NO_CALLER, "verified": False}
    attempts = int(tool_context.state.get(VERIFY_ATTEMPTS) or 0)
    if attempts >= MAX_ATTEMPTS_PER_CALL:
        return {
            "verified": False,
            "error": "Too many attempts on this call. Offer escalate_to_human instead.",
        }
    tool_context.state[VERIFY_ATTEMPTS] = attempts + 1
    result = await asyncio.to_thread(access_code.verify, phone, code)
    if result["verified"]:
        tool_context.state[VERIFIED] = True
        return {"verified": True}
    if result["reason"] == "locked":
        return {
            "verified": False,
            "error": "Too many wrong codes for this number. It is locked for an hour.",
        }
    left = MAX_ATTEMPTS_PER_CALL - attempts - 1
    return {
        "verified": False,
        "error": "That code doesn't match this number.",
        "attempts_left": left,
    }


async def list_my_appointments(tool_context: ToolContext) -> dict:
    """List the caller's upcoming (non-cancelled) appointments. The caller's phone
    number is used automatically."""
    phone = _caller(tool_context)
    if not phone:
        return _NO_CALLER
    if not _verified(tool_context):
        return _VERIFY_FIRST
    appts = await asyncio.to_thread(repo.list_patient_appointments, phone)
    return {"appointments": appts}


def _may_change(appointment_id: str, tool_context: ToolContext) -> bool:
    return _verified(tool_context) or appointment_id in _booked_this_call(tool_context)


async def cancel_appointment(appointment_id: str, tool_context: ToolContext) -> dict:
    """Cancel an existing appointment by its id.

    Args:
        appointment_id: The appointment id (from list_my_appointments).
    """
    phone = _caller(tool_context)
    if not phone:
        return _NO_CALLER
    if not _may_change(appointment_id, tool_context):
        return _VERIFY_FIRST
    return await asyncio.to_thread(repo.cancel_appointment, appointment_id, phone)


async def reschedule_appointment(
    appointment_id: str, new_date: str, new_slot: str, tool_context: ToolContext
) -> dict:
    """Move an existing appointment to a new date and time, after confirming the
    new slot with the patient.

    Args:
        appointment_id: The appointment id (from list_my_appointments).
        new_date: The new date, formatted as YYYY-MM-DD.
        new_slot: The new slot start time, formatted as HH:mm.
    """
    phone = _caller(tool_context)
    if not phone:
        return _NO_CALLER
    if not _may_change(appointment_id, tool_context):
        return _VERIFY_FIRST
    return await asyncio.to_thread(
        repo.reschedule_appointment, appointment_id, new_date, new_slot, phone
    )


async def search_catalog(catalog: str, query: str = "") -> dict:
    """Search one of the clinic's catalogs (e.g. lab tests, services, health
    packages) listed under "Clinic catalogs" in your instructions. Returns the
    matching items with their details (price, preparation, timings, …). Answer
    only from what this returns.

    Args:
        catalog: The catalog name exactly as listed (e.g. "lab_test").
        query: A few words to match (e.g. "thyroid" or "blood sugar fasting");
            empty to list the first few items.
    """
    return await asyncio.to_thread(catalog_data.search_catalog, catalog, query)


async def escalate_to_human(reason: str, tool_context: ToolContext) -> dict:
    """Hand off to the clinic's human team when the patient asks for a human or
    has a request outside your scope (billing disputes, medical questions,
    complaints). Staff see the request in the clinic console. On WhatsApp they
    take over this chat; on a call they phone the patient back.

    Args:
        reason: A short description of why a human is needed.
    """
    log.info("Escalation requested: %s", reason)
    phone = _caller(tool_context)
    channel = str(tool_context.state.get(CHANNEL) or "whatsapp")
    conversation_id = tool_context.state.get(CONVERSATION_ID) or None
    try:
        handoff = await asyncio.to_thread(
            escalation.request_handoff, phone, channel, reason, conversation_id
        )
    except Exception:
        # The conversation is still flagged `escalated`; never fail the turn.
        log.exception("could not open an escalation")
        handoff = {"escalation_id": None, "already_requested": False}
    if channel == "voice":
        message = (
            "I've let our clinic team know. Someone will call you back on this number shortly."
        )
    else:
        message = "I've passed this chat to our clinic team. A team member will reply to you here."
    return {"escalated": True, "message": message, **handoff}


ALL_TOOLS = [
    get_clinic_info,
    list_doctors,
    check_availability,
    find_next_available,
    book_appointment,
    verify_caller,
    list_my_appointments,
    reschedule_appointment,
    cancel_appointment,
    search_catalog,
    escalate_to_human,
]
