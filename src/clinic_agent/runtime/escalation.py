"""Human handoff: alert the clinic's staff when a patient asks for a person.

``escalate_to_human`` (the agent tool) calls ``request_handoff``. It queues one
``staff.alert`` outbox job per staff member who should hear about it, and the
outbox worker sends each one a WhatsApp alert (``alert_staff``). Queuing keeps a
slow or failing Meta call out of the patient's conversation and gives retries.

**Who is alerted** is configured in the console on the **Staff** custom page
(``records`` with ``entityType: "staff"``, see the clinic data contract): every
active row with ``receivesEscalations: true`` and a usable ``phone``. The page is
not ``agentEnabled`` and its fields are not agent-visible, so staff numbers never
reach the model; only this module reads them.

**No spam.** If staff were already alerted about the same patient within
``DEDUPE_WINDOW``, asking again doesn't alert them again.

Alert wording is fixed text built from stored data. Outside WhatsApp's 24-hour
window Meta only delivers approved templates; set ``WHATSAPP_TEMPLATE_ESCALATION``
to one with body params {{1}} patient name, {{2}} patient phone, {{3}} channel,
{{4}} reason, {{5}} clinic name.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from clinic_agent.config import settings
from clinic_agent.db import get_db
from clinic_agent.phone import is_valid_phone, normalize_phone, to_e164
from clinic_agent.runtime import outbox
from clinic_agent.runtime.outbox import PermanentError
from clinic_agent.tools import catalog
from clinic_agent.whatsapp import client as whatsapp

log = logging.getLogger("clinic_agent.runtime.escalation")

STAFF_TYPE = "staff"
DEDUPE_WINDOW = timedelta(minutes=30)
_CHANNEL_LABEL = {"whatsapp": "WhatsApp chat", "voice": "voice call"}


def escalation_recipients(clinic_id: str | None = None) -> list[dict]:
    """Active staff with handoff alerts on: [{name, phone (digits)}]."""
    clinic_id = clinic_id or settings.CLINIC_ID
    if STAFF_TYPE not in catalog.get_definitions(clinic_id):
        return []  # page missing or archived
    out: list[dict] = []
    seen: set[str] = set()
    for doc in get_db()[catalog.RECORDS].find(
        {"clinicId": clinic_id, "entityType": STAFF_TYPE, "isActive": True}
    ):
        attrs = doc.get("attributes") or {}
        if attrs.get("receivesEscalations") is not True:
            continue
        raw = attrs.get("phone")
        if not isinstance(raw, str) or not is_valid_phone(raw):
            log.warning("staff %s has handoff alerts on but no valid phone", doc.get("_id"))
            continue
        phone = normalize_phone(raw)
        if phone in seen:
            continue
        seen.add(phone)
        out.append({"name": str(attrs.get("name") or "Team"), "phone": phone})
    return out


def _recently_alerted(patient_phone: str) -> bool:
    since = (datetime.now(UTC) - DEDUPE_WINDOW).isoformat()
    return (
        get_db()[outbox.COLLECTION].find_one(
            {
                "clinicId": settings.CLINIC_ID,
                "kind": outbox.STAFF_ALERT,
                "patientPhone": patient_phone,
                "createdAt": {"$gte": since},
                "status": {"$ne": "failed"},
            }
        )
        is not None
    )


def request_handoff(patient_phone: str, channel: str, reason: str) -> dict:
    """Queue staff alerts for this patient. Returns {staff_alerted, already_alerted}."""
    if not settings.STAFF_ALERTS_ENABLED or not patient_phone:
        return {"staff_alerted": 0, "already_alerted": False}
    if _recently_alerted(patient_phone):
        return {"staff_alerted": 0, "already_alerted": True}
    recipients = escalation_recipients()
    if not recipients:
        log.warning("handoff requested but no staff receive alerts (Staff page)")
    for r in recipients:
        outbox.enqueue_staff_alert(
            patientPhone=patient_phone,
            channel=channel,
            reason=(reason or "").strip()[:300],
            staffName=r["name"],
            staffPhone=r["phone"],
        )
    return {"staff_alerted": len(recipients), "already_alerted": False}


# ---- outbox handler -------------------------------------------------------------


def _context_sync(patient_phone: str) -> tuple[str, str]:
    """(patient name or "", clinic name) from the stored records."""
    scope = {"clinicId": settings.CLINIC_ID}
    patient = get_db()["patients"].find_one({**scope, "phone": patient_phone}) or {}
    clinic = get_db()["clinic"].find_one(scope) or {}
    return str(patient.get("name") or ""), str(clinic.get("clinicName") or "the clinic")


def alert_text(job: dict, patient_name: str, clinic_name: str, at: datetime) -> str:
    phone = to_e164(job["patientPhone"])
    channel = _CHANNEL_LABEL.get(job.get("channel", ""), job.get("channel") or "chat")
    follow_up = (
        "Reply to them on WhatsApp from the clinic number."
        if job.get("channel") == "whatsapp"
        else "Please call them back on this number."
    )
    return (
        f"Patient wants to talk to a person - {clinic_name}\n\n"
        f"Hi {job.get('staffName') or 'team'},\n"
        f"Patient: {patient_name or 'Not given yet'}\n"
        f"Phone: {phone}\n"
        f"Via: {channel}\n"
        f"Reason: {job.get('reason') or 'Asked for a human'}\n"
        f"Time: {at:%-d %b, %-I:%M %p}\n\n"
        f"{follow_up}"
    )


async def alert_staff(job: dict) -> tuple[str, dict]:
    """Send one staff member the handoff alert (outbox ``staff.alert``)."""
    patient_name, clinic_name = await asyncio.to_thread(_context_sync, job["patientPhone"])
    template = settings.WHATSAPP_TEMPLATE_ESCALATION
    try:
        if template:
            params = [
                patient_name or "Not given",
                to_e164(job["patientPhone"]),
                _CHANNEL_LABEL.get(job.get("channel", ""), "chat"),
                job.get("reason") or "Asked for a human",
                clinic_name,
            ]
            resp = await whatsapp.send_template_message(
                job["staffPhone"], template, settings.WHATSAPP_TEMPLATE_LANGUAGE, params
            )
        else:
            now = datetime.now(ZoneInfo(settings.CLINIC_TIMEZONE))
            body = alert_text(job, patient_name, clinic_name, now)
            resp = await whatsapp.send_text_message(job["staffPhone"], body)
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None and 400 <= status < 500 and status != 429:
            # Usually: the staff number hasn't messaged the clinic in 24h and no
            # template is configured, or the number isn't on WhatsApp.
            raise PermanentError(f"WhatsApp rejected the staff alert ({status})") from exc
        raise
    message_id = ((resp or {}).get("messages") or [{}])[0].get("id")
    return "done", {
        "result": "sent",
        "messageId": message_id,
        "via": "template" if template else "text",
    }
