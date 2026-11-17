"""Human handoff: escalation requests for the staff console, and staff replies.

The contract lives in the console repo: ``docs/staff-handoff-contract.md``.

* ``escalate_to_human`` (the agent tool) calls ``request_handoff``, which inserts
  one ``escalations`` document (``status: "open"``). Staff work these on the
  console's **Escalations** page; voice callers are phoned back from there.
* **WhatsApp:** the turn that escalates flips its conversation to
  ``handledBy: "staff"`` (``conversations.record_turn``). From then on the AI
  stays quiet: ``capture_for_staff`` stores the patient's messages for staff to
  read, until the console resolves the escalation and sets ``handledBy`` back.
* Staff replies are ``staff.reply`` outbox jobs the console inserts; ``send_staff_reply``
  delivers them and records the delivery on the staff message.

No WhatsApp alerts go to staff phones.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from clinic_agent.config import settings
from clinic_agent.db import create_index_safe, get_db
from clinic_agent.phone import to_e164
from clinic_agent.runtime import conversations, history, outbox
from clinic_agent.runtime.outbox import PermanentError
from clinic_agent.whatsapp import client as whatsapp

log = logging.getLogger("clinic_agent.runtime.escalation")

COLLECTION = conversations.ESCALATIONS
OPEN, CLAIMED, RESOLVED = "open", "claimed", "resolved"
REASON_MAX = 300


def ensure_indexes() -> None:
    col = get_db()[COLLECTION]
    create_index_safe(
        col, [("clinicId", 1), ("status", 1), ("createdAt", -1)], name="clinic_status_created"
    )
    create_index_safe(
        col,
        [("clinicId", 1), ("conversationId", 1), ("status", 1)],
        name="clinic_conversation_status",
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---- the tool's side: open an escalation ---------------------------------------


def _patient_name(patient_phone: str) -> str:
    doc = get_db()["patients"].find_one(
        {"clinicId": settings.CLINIC_ID, "phone": patient_phone}, {"name": 1}
    )
    return str((doc or {}).get("name") or "")


def request_handoff(
    patient_phone: str, channel: str, reason: str, conversation_id: str | None
) -> dict:
    """Open an escalation for staff. Returns ``{escalation_id, already_requested}``.

    At most one unresolved escalation per conversation: asking again returns the
    existing one.
    """
    col = get_db()[COLLECTION]
    if conversation_id:
        existing = col.find_one(
            {
                "clinicId": settings.CLINIC_ID,
                "conversationId": conversation_id,
                "status": {"$ne": RESOLVED},
            },
            {"_id": 1},
        )
        if existing:
            return {"escalation_id": str(existing["_id"]), "already_requested": True}
    now = _now()
    res = col.insert_one(
        {
            "clinicId": settings.CLINIC_ID,
            "conversationId": conversation_id,
            "channel": channel,
            "patientPhone": to_e164(patient_phone) if patient_phone else "",
            "patientName": _patient_name(patient_phone) if patient_phone else "",
            "reason": (reason or "").strip()[:REASON_MAX],
            "status": OPEN,
            "createdAt": now,
            "updatedAt": now,
            "claimedBy": None,
            "claimedByName": None,
            "claimedAt": None,
            "resolvedBy": None,
            "resolvedByName": None,
            "resolvedAt": None,
            "resolutionNote": None,
        }
    )
    log.info(
        "escalation %s opened (%s, conversation %s)", res.inserted_id, channel, conversation_id
    )
    return {"escalation_id": str(res.inserted_id), "already_requested": False}


# ---- inbound while staff handle the chat -----------------------------------------


async def capture_for_staff(phone: str, texts: list[str]) -> bool:
    """If staff are handling this patient's chat, store the messages and return True.

    The caller then skips the AI turn: no reply is sent.
    """
    conv_id = await asyncio.to_thread(conversations.record_staff_handled_messages, phone, texts)
    if conv_id is None:
        return False
    await history.append_messages(phone, [{"role": "user", "content": t} for t in texts])
    log.info("chat %s is with staff: stored %d message(s), no AI reply", conv_id, len(texts))
    return True


# ---- outbox handler: deliver a staff reply ---------------------------------------


def _mark_delivery(conversation_id: str, client_id: str, delivery: dict) -> None:
    get_db()[conversations.COLLECTION].update_one(
        {
            "clinicId": settings.CLINIC_ID,
            "externalId": conversation_id,
            "messages.clientId": client_id,
        },
        {"$set": {"messages.$.delivery": {**delivery, "at": _now()}}},
    )


def _conversation_phone(conversation_id: str) -> str | None:
    doc = get_db()[conversations.COLLECTION].find_one(
        {"clinicId": settings.CLINIC_ID, "externalId": conversation_id},
        {"phone": 1, "channel": 1},
    )
    if not doc or doc.get("channel", conversations.CHANNEL) != conversations.CHANNEL:
        return None
    digits = "".join(ch for ch in str(doc.get("phone") or "") if ch.isdigit())
    return digits or None


async def send_staff_reply(job: dict) -> tuple[str, dict]:
    """Send one staff reply to the patient on WhatsApp (outbox ``staff.reply``)."""
    conversation_id = str(job.get("conversationId") or "")
    client_id = str(job.get("messageClientId") or "")
    text = str(job.get("text") or "").strip()

    async def fail(error: str) -> PermanentError:
        if client_id:
            await asyncio.to_thread(
                _mark_delivery, conversation_id, client_id, {"status": "failed", "error": error}
            )
        return PermanentError(error)

    if not text:
        raise await fail("empty message")
    phone = await asyncio.to_thread(_conversation_phone, conversation_id)
    if phone is None:
        raise await fail("no WhatsApp conversation for this reply")

    try:
        resp = await whatsapp.send_text_message(phone, text)
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None and 400 <= status < 500 and status != 429:
            # Usually the 24-hour window has closed, or the number left WhatsApp.
            raise await fail(f"WhatsApp rejected the message ({status})") from exc
        if job.get("attempts", 1) >= outbox.MAX_ATTEMPTS:
            raise await fail("couldn't reach WhatsApp; gave up after retries") from exc
        raise

    message_id = ((resp or {}).get("messages") or [{}])[0].get("id")
    await asyncio.to_thread(
        _mark_delivery, conversation_id, client_id, {"status": "sent", "waMessageId": message_id}
    )
    # The AI reads this when the chat is handed back, so it knows what staff said.
    await history.append_messages(phone, [{"role": "staff", "content": text}])
    return "done", {"result": "sent", "messageId": message_id}
