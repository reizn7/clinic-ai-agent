"""Outbox handlers: put appointments on the doctor's calendar, confirm to the patient.

Both handlers start by re-reading the appointment (and its doctor, patient and
clinic) from MongoDB, so they act on what is actually stored — never on what the
model said — and a job that runs late reflects the latest state (a confirmation
for a booking cancelled in the meantime is skipped).

Message wording is fixed text built from the stored fields, not model output, so a
patient can rely on it. The same values feed the optional Meta templates:

* booked / rescheduled: {{1}} patient name, {{2}} doctor, {{3}} date and time,
  {{4}} clinic, {{5}} booking reference, {{6}} security code

Booked / rescheduled confirmations carry the patient's security code
(``tools.access_code``): the only way a phone caller can prove they own the
number. It is created here on first use, so every confirmation has one.
* cancelled: {{1}} patient name, {{2}} doctor, {{3}} date and time, {{4}} clinic
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bson import ObjectId

from clinic_agent.config import settings
from clinic_agent.db import get_db
from clinic_agent.integrations import google_calendar
from clinic_agent.runtime import escalation
from clinic_agent.runtime.outbox import CALENDAR_SYNC, STAFF_ALERT, WHATSAPP_NOTIFY, PermanentError
from clinic_agent.tools import access_code
from clinic_agent.whatsapp import client as whatsapp


@dataclass(frozen=True)
class Snapshot:
    appointment_id: str
    status: str
    date: str  # YYYY-MM-DD (clinic local)
    slot: str  # HH:mm (clinic local)
    doctor_name: str
    specialization: str
    calendar_email: str
    duration_min: int
    patient_name: str
    patient_phone: str
    patient_age: int | None
    patient_gender: str
    clinic_name: str
    clinic_address: str
    clinic_phone: str
    patient_id: ObjectId | None = None
    access_code: str = ""  # filled in only for booked/rescheduled confirmations

    @property
    def start(self) -> datetime:
        tz = ZoneInfo(settings.CLINIC_TIMEZONE)
        return datetime.strptime(f"{self.date} {self.slot}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)

    @property
    def when(self) -> str:
        """e.g. 'Monday, 23 November 2026 at 10:00 AM'."""
        return f"{self.start:%A, %-d %B %Y} at {self.start:%-I:%M %p}"

    @property
    def doctor(self) -> str:
        return f"Dr. {self.doctor_name}"

    @property
    def reference(self) -> str:
        return self.appointment_id[-6:].upper()


def _load_sync(appointment_id: ObjectId) -> Snapshot | None:
    db = get_db()
    scope = {"clinicId": settings.CLINIC_ID}
    appt = db["appointments"].find_one({**scope, "_id": appointment_id})
    if not appt:
        return None
    doctor = db["doctors"].find_one({**scope, "_id": appt.get("doctorId")}) or {}
    patient = db["patients"].find_one({**scope, "_id": appt.get("patientId")}) or {}
    clinic = db["clinic"].find_one(scope) or {}
    return Snapshot(
        appointment_id=str(appt["_id"]),
        status=appt.get("status", ""),
        date=appt.get("date", ""),
        slot=appt.get("slot", ""),
        doctor_name=doctor.get("name", "your doctor"),
        specialization=doctor.get("specialization", ""),
        calendar_email=(doctor.get("calendarEmail") or "").strip(),
        duration_min=int(doctor.get("consultationDuration") or 30),
        patient_name=patient.get("name", "") or "there",
        patient_phone=patient.get("phone", ""),
        patient_age=patient.get("age"),
        patient_gender=patient.get("gender", ""),
        clinic_name=clinic.get("clinicName", "the clinic"),
        clinic_address=clinic.get("address", ""),
        clinic_phone=clinic.get("phone", ""),
        patient_id=patient.get("_id"),
    )


async def load(appointment_id: ObjectId) -> Snapshot | None:
    return await asyncio.to_thread(_load_sync, appointment_id)


def _set_appointment_sync(appointment_id: ObjectId, fields: dict) -> None:
    """Record a follow-up's outcome on the appointment (for staff/console)."""
    get_db()["appointments"].update_one(
        {"clinicId": settings.CLINIC_ID, "_id": appointment_id}, {"$set": fields}
    )


# ---- calendar -----------------------------------------------------------------------


def event_body(s: Snapshot) -> dict:
    tz = settings.CLINIC_TIMEZONE
    start = s.start
    end = start + timedelta(minutes=s.duration_min)
    patient = s.patient_name
    if s.patient_age or s.patient_gender:
        patient += f" ({', '.join(str(x) for x in (s.patient_age, s.patient_gender) if x)})"
    return {
        "summary": f"Appointment: {s.patient_name}",
        "description": (
            f"Patient: {patient}\n"
            f"Phone: +{s.patient_phone}\n"
            f"Booked via Medi Desk AI, {s.clinic_name}'s AI receptionist.\n"
            f"Booking ref: {s.reference} (status: {s.status})"
        ),
        "start": {"dateTime": start.isoformat(), "timeZone": tz},
        "end": {"dateTime": end.isoformat(), "timeZone": tz},
        "extendedProperties": {"private": {"appointmentId": s.appointment_id}},
        "reminders": {"useDefault": True},
    }


async def sync_calendar(job: dict) -> tuple[str, dict]:
    """Make the doctor's calendar match the appointment (create/move/remove)."""
    appt_id = job["appointmentId"]
    s = await load(appt_id)
    if s is None:
        return "skipped", {"result": "appointment not found"}
    if not s.calendar_email:
        await asyncio.to_thread(
            _set_appointment_sync,
            appt_id,
            {"calendarSync": {"status": "skipped", "reason": "doctor has no calendarEmail"}},
        )
        return "skipped", {"result": "doctor has no calendarEmail"}

    event_id = google_calendar.event_id_for(s.appointment_id)
    try:
        if s.status == "Cancelled":
            await google_calendar.delete_event(s.calendar_email, event_id)
            result = "removed"
        else:
            await google_calendar.upsert_event(s.calendar_email, event_id, event_body(s))
            result = "synced"
    except google_calendar.CalendarError as exc:
        await asyncio.to_thread(
            _set_appointment_sync,
            appt_id,
            {"calendarSync": {"status": "error", "error": str(exc)[:300]}},
        )
        if exc.permanent:
            raise PermanentError(str(exc)) from exc
        raise
    await asyncio.to_thread(
        _set_appointment_sync,
        appt_id,
        {
            "calendarSync": {
                "status": result,
                "calendarId": s.calendar_email,
                "eventId": event_id,
                "at": datetime.now().astimezone().isoformat(),
            }
        },
    )
    return "done", {"result": result, "eventId": event_id}


# ---- WhatsApp confirmation ----------------------------------------------------------


def message_text(event: str, s: Snapshot) -> str:
    where = s.clinic_name + (f", {s.clinic_address}" if s.clinic_address else "")
    doctor = s.doctor + (f" ({s.specialization})" if s.specialization else "")
    if event == "cancelled":
        return (
            f"Appointment cancelled\n\n"
            f"Hi {s.patient_name}, your appointment with {doctor} on {s.when} at "
            f"{s.clinic_name} has been cancelled.\n\n"
            f"Message us here any time to book again."
        )
    title = "Appointment rescheduled" if event == "rescheduled" else "Appointment confirmed"
    contact = f"\nClinic phone: {s.clinic_phone}" if s.clinic_phone else ""
    return (
        f"{title}\n\n"
        f"Patient: {s.patient_name}\n"
        f"Doctor: {doctor}\n"
        f"When: {s.when}\n"
        f"Where: {where}{contact}\n"
        f"Booking ref: {s.reference}\n"
        + (
            f"Security code: {s.access_code}\n"
            "Keep it private. You'll need it to manage your appointments on a call.\n"
            if s.access_code
            else ""
        )
        + "\nReply here to reschedule or cancel."
    )


def template_for(event: str, s: Snapshot) -> tuple[str, list[str]] | None:
    name = {
        "booked": settings.WHATSAPP_TEMPLATE_BOOKED,
        "rescheduled": settings.WHATSAPP_TEMPLATE_RESCHEDULED,
        "cancelled": settings.WHATSAPP_TEMPLATE_CANCELLED,
    }.get(event, "")
    if not name:
        return None
    params = [s.patient_name, s.doctor, s.when, s.clinic_name]
    if event != "cancelled":
        params.append(s.reference)
        params.append(s.access_code)
    return name, params


async def notify_patient(job: dict) -> tuple[str, dict]:
    """Send the patient a confirmation built from the stored appointment."""
    appt_id, event = job["appointmentId"], job.get("event", "booked")
    s = await load(appt_id)
    if s is None:
        return "skipped", {"result": "appointment not found"}
    if not s.patient_phone:
        return "skipped", {"result": "patient has no phone"}
    if event != "cancelled" and s.status == "Cancelled":
        return "skipped", {"result": "appointment cancelled before the confirmation went out"}
    if event != "cancelled" and s.patient_id is not None:
        code = await asyncio.to_thread(access_code.ensure_access_code, s.patient_id)
        s = replace(s, access_code=code or "")

    template = template_for(event, s)
    try:
        if template:
            resp = await whatsapp.send_template_message(
                s.patient_phone, template[0], settings.WHATSAPP_TEMPLATE_LANGUAGE, template[1]
            )
        else:
            resp = await whatsapp.send_text_message(s.patient_phone, message_text(event, s))
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        await asyncio.to_thread(
            _set_appointment_sync,
            appt_id,
            {"confirmation": {"status": "error", "event": event, "error": repr(exc)[:300]}},
        )
        if status is not None and 400 <= status < 500 and status != 429:
            # e.g. outside the 24h window without a template, invalid number.
            raise PermanentError(f"WhatsApp rejected the message ({status})") from exc
        raise
    message_id = ((resp or {}).get("messages") or [{}])[0].get("id")
    await asyncio.to_thread(
        _set_appointment_sync,
        appt_id,
        {
            "confirmation": {
                "status": "sent",
                "event": event,
                "messageId": message_id,
                "via": "template" if template else "text",
                "at": datetime.now().astimezone().isoformat(),
            }
        },
    )
    return "done", {"result": "sent", "messageId": message_id}


HANDLERS = {
    CALENDAR_SYNC: sync_calendar,
    WHATSAPP_NOTIFY: notify_patient,
    STAFF_ALERT: escalation.alert_staff,
}
