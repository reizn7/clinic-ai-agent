"""Console-facing ``conversations`` collection writer.

Implements the staff-console data contract: one document per *conversation*
(not per phone), written to a separate ``conversations`` collection alongside
the agent's own ``transcripts``. ``channel`` says where it happened:

* ``whatsapp`` — sessionised by a 30-minute idle window (``record_turn``).
* ``voice`` — exactly one conversation per browser voice call, no rotation
  (``start_call`` on connect, ``finish_call`` on hangup), plus ``callId`` and
  ``durationSec``. WhatsApp sessionisation never picks up a voice conversation.

Derived fields (outcome, intents, doctorName, appointmentAt, escalation) are
computed here from the captured tool calls — no extra LLM call. Nice-to-have
fields (summary, sentiment, language, confidence) are left unset for now; the
console degrades gracefully without them.

DESIGN NOTES / judgement calls (flag for the console team):
  * Timestamps are ISO 8601 UTC strings.
  * ``appointmentAt`` is built from the booked date+slot as a NAIVE local
    datetime (no clinic timezone configured yet) — revisit once a clinic TZ is
    available if the console needs true UTC.
  * A conversation is closed LAZILY: the next inbound from the same phone after
    the idle window finalizes the previous one (completed/abandoned/escalated).
    A trailing conversation with no follow-up stays ``active`` until a future
    sweeper closes it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pymongo import ReturnDocument

from clinic_agent.config import settings
from clinic_agent.db import create_index_safe, get_db
from clinic_agent.phone import to_e164

if TYPE_CHECKING:
    from clinic_agent.runtime.analytics import ConversationAnalytics

COLLECTION = "conversations"
IDLE_TIMEOUT = timedelta(minutes=30)
CHANNEL = "whatsapp"
VOICE_CHANNEL = "voice"
# Text marker for a voice turn the agent opened (its greeting): no caller speech.
CALL_STARTED = "[voice call started]"

# tool name → primaryIntent enum (closed set in the contract §4).
_TOOL_INTENT = {
    "book_appointment": "book_appointment",
    "cancel_appointment": "cancel_appointment",
    "reschedule_appointment": "reschedule_appointment",
    "check_availability": "doctor_availability",
    "find_next_available": "doctor_availability",
    "get_clinic_info": "clinic_info",
    "list_doctors": "clinic_info",
    "list_my_appointments": "other",
    "escalate_to_human": "other",
}
_INFO_TOOLS = {"get_clinic_info", "list_doctors", "list_my_appointments"}


def ensure_indexes() -> None:
    col = get_db()[COLLECTION]
    create_index_safe(col, "externalId", unique=True, name="uniq_externalId")
    create_index_safe(col, [("clinicId", 1), ("startedAt", -1)], name="clinic_started")
    create_index_safe(col, [("clinicId", 1), ("outcome", 1), ("startedAt", -1)], name="clinic_outcome")
    create_index_safe(col, [("clinicId", 1), ("escalated", 1), ("startedAt", -1)], name="clinic_escalated")
    create_index_safe(col, [("phone", 1), ("startedAt", -1)], name="phone_started")


# ---- derivations (pure) ---------------------------------------------------


def _to_e164(phone: str) -> str:
    return to_e164(phone)


def _succeeded(calls: list[dict], name: str) -> bool:
    return any(c.get("name") == name and c.get("status") == "success" for c in calls)


def derive_outcome(calls: list[dict]) -> str:
    booked = _succeeded(calls, "book_appointment")
    cancelled = _succeeded(calls, "cancel_appointment")
    rescheduled = _succeeded(calls, "reschedule_appointment")
    if rescheduled or (booked and cancelled):
        return "appointment_rescheduled"
    if booked:
        return "appointment_booked"
    if cancelled:
        return "appointment_cancelled"
    if _succeeded(calls, "escalate_to_human"):
        return "escalated"
    if any(c.get("name") in _INFO_TOOLS and c.get("status") == "success" for c in calls):
        return "info_provided"
    return "no_resolution"


def derive_intents(calls: list[dict]) -> list[str]:
    seen: list[str] = []
    for c in calls:
        intent = _TOOL_INTENT.get(c.get("name"), "other")
        if intent not in seen:
            seen.append(intent)
    return seen


# Most goal-like first. A booking call usually opens with list_doctors /
# check_availability, so "first tool called" would label it clinic_info and
# hide it from the console's booking-completion KPI.
_INTENT_PRIORITY = (
    "reschedule_appointment",
    "cancel_appointment",
    "book_appointment",
    "doctor_availability",
    "clinic_info",
    "other",
)


def derive_primary_intent(calls: list[dict]) -> str:
    """The patient's main goal: the highest-priority intent attempted (success or not)."""
    intents = set(derive_intents(calls))
    return next((i for i in _INTENT_PRIORITY if i in intents), "other")


def escalation_reason(calls: list[dict]) -> str | None:
    for c in calls:
        if c.get("name") == "escalate_to_human" and c.get("status") == "success":
            return (c.get("args") or {}).get("reason", "")
    return None


def _doctor_name(calls: list[dict]) -> str | None:
    for c in reversed(calls):
        if c.get("name") in ("book_appointment", "check_availability"):
            dn = (c.get("args") or {}).get("doctor_name")
            if dn:
                return dn
    return None


def _appointment_at(calls: list[dict]) -> str | None:
    for c in reversed(calls):
        if c.get("name") == "book_appointment" and c.get("status") == "success":
            args = c.get("args") or {}
            date, slot = args.get("date"), args.get("slot")
            if date and slot:
                try:
                    return datetime.fromisoformat(f"{date}T{slot}").isoformat()
                except ValueError:
                    return None
    return None


def _calls_from_messages(messages: list[dict]) -> list[dict]:
    """Reconstruct the tool-call list from stored tool_call messages."""
    out = []
    for m in messages:
        if m.get("type") == "tool_call" and m.get("tool"):
            t = m["tool"]
            out.append(
                {
                    "name": t.get("name"),
                    "status": t.get("status"),
                    "args": t.get("args") or {},
                    "result": t.get("result"),
                }
            )
    return out


def _final_status(calls: list[dict]) -> str:
    if _succeeded(calls, "escalate_to_human"):
        return "escalated"
    return "abandoned" if derive_outcome(calls) == "no_resolution" else "completed"


def _build_turn_messages(
    patient_text: str,
    agent_reply: str,
    tool_calls: list[dict],
    latency_ms: int | None,
    now: datetime,
) -> list[dict]:
    ts = now.isoformat()
    messages: list[dict[str, Any]] = [
        {"role": "patient", "type": "text", "content": patient_text, "timestamp": ts}
    ]
    for c in tool_calls:
        args = c.get("args") or {}
        is_error = c.get("status") == "error"
        messages.append(
            {
                "role": "agent",
                "type": "tool_call",
                "content": f"{c.get('name')}({', '.join(args.keys())})",
                "timestamp": ts,
                "tool": {
                    "name": c.get("name"),
                    "args": args,
                    "result": None if is_error else c.get("result"),
                    "status": c.get("status"),
                    "error": (c.get("result") or {}).get("error") if is_error else None,
                    "durationMs": c.get("duration_ms"),
                },
            }
        )
    agent_msg: dict[str, Any] = {
        "role": "agent",
        "type": "text",
        "content": agent_reply,
        "timestamp": ts,
    }
    if latency_ms is not None:
        agent_msg["latencyMs"] = latency_ms
    messages.append(agent_msg)
    return messages


def _derived_fields(all_calls: list[dict]) -> dict[str, Any]:
    """Console fields recomputed from every tool call in a conversation."""
    return {
        "outcome": derive_outcome(all_calls),
        "primaryIntent": derive_primary_intent(all_calls),
        "intents": derive_intents(all_calls),
        "doctorName": _doctor_name(all_calls),
        "appointmentAt": _appointment_at(all_calls),
        "escalated": _succeeded(all_calls, "escalate_to_human"),
        "escalationReason": escalation_reason(all_calls),
    }


def _patient_name(phone: str) -> str:
    doc = get_db()["patients"].find_one(
        {"clinicId": settings.CLINIC_ID, "phone": phone}, {"name": 1}
    )
    return (doc or {}).get("name") or "Unknown"


def _render_transcript(messages: list[dict]) -> str:
    """Plain 'role: content' transcript of the text turns (skips tool calls)."""
    lines = [
        f"{m.get('role')}: {m.get('content', '')}"
        for m in messages
        if m.get("type", "text") == "text"
    ]
    return "\n".join(lines)


# ---- finalize + analytics (called by the idle sweeper) --------------------


def close_idle_active(idle_cutoff_iso: str) -> int:
    """Close active conversations whose last activity predates the cutoff.

    ``endedAt`` is refreshed to now on every turn, so it is the last-activity
    marker; ISO-8601 UTC strings compare chronologically as plain strings.
    """
    col = get_db()[COLLECTION]
    closed = 0
    query = {"clinicId": settings.CLINIC_ID, "status": "active", "endedAt": {"$lt": idle_cutoff_iso}}
    for conv in col.find(query):
        status = _final_status(_calls_from_messages(conv.get("messages", [])))
        col.update_one({"externalId": conv["externalId"]}, {"$set": {"status": status}})
        closed += 1
    return closed


def claim_analytics_batch(stuck_cutoff_iso: str, limit: int) -> list[tuple[str, str]]:
    """Claim ended conversations needing analytics; return (externalId, transcript).

    Picks conversations that are no longer active and have no analytics yet, plus
    any stuck in ``processing`` past the cutoff. Claiming flips them to
    ``processing`` atomically so a second pass won't double-process them.
    """
    col = get_db()[COLLECTION]
    now = datetime.now(UTC).isoformat()
    query = {
        "clinicId": settings.CLINIC_ID,
        "status": {"$ne": "active"},
        "$or": [
            {"analyticsStatus": {"$exists": False}},
            {"analyticsStatus": "processing", "analyticsStartedAt": {"$lt": stuck_cutoff_iso}},
        ],
    }
    claimed: list[tuple[str, str]] = []
    for conv in col.find(query).limit(limit):
        doc = col.find_one_and_update(
            {
                "externalId": conv["externalId"],
                "$or": [
                    {"analyticsStatus": {"$exists": False}},
                    {"analyticsStatus": "processing"},
                ],
            },
            {"$set": {"analyticsStatus": "processing", "analyticsStartedAt": now}},
            return_document=ReturnDocument.AFTER,
        )
        if doc:
            claimed.append((doc["externalId"], _render_transcript(doc.get("messages", []))))
    return claimed


def apply_analytics(external_id: str, result: ConversationAnalytics | None) -> None:
    """Store analytics (or mark failed) for a claimed conversation."""
    col = get_db()[COLLECTION]
    if result is None:
        col.update_one({"externalId": external_id}, {"$set": {"analyticsStatus": "failed"}})
        return
    col.update_one(
        {"externalId": external_id},
        {
            "$set": {
                "summary": result.summary,
                "sentiment": result.sentiment,
                "language": result.language,
                "analyticsStatus": "complete",
            }
        },
    )


# ---- the writer -----------------------------------------------------------


def _new_conversation_id() -> str:
    return f"conv_{uuid.uuid4().hex}"


def _is_stale(conv: dict, now: datetime) -> bool:
    """True once the idle window has elapsed since the conversation's last message."""
    msgs = conv.get("messages", [])
    if not msgs:
        return False
    try:
        last_ts = datetime.fromisoformat(msgs[-1]["timestamp"])
    except (ValueError, KeyError, TypeError):
        return False
    return now - last_ts > IDLE_TIMEOUT


def _latest_active_whatsapp(col: Any, e164: str) -> dict | None:
    # Voice calls are their own conversations; never append WhatsApp turns to one.
    return col.find_one(
        {
            "clinicId": settings.CLINIC_ID,
            "phone": e164,
            "status": "active",
            "channel": {"$ne": VOICE_CHANNEL},
        },
        sort=[("startedAt", -1)],
    )


def _resolve_conversation_id_sync(phone: str) -> str:
    """The id the NEXT WhatsApp turn from ``phone`` will be recorded under.

    Read-only: the open conversation's id if it is still inside the idle window,
    otherwise a fresh id that ``record_turn(..., conversation_id=)`` will create
    the conversation with. Lets a turn be traced under its conversation (the
    Langfuse session) before the turn runs.
    """
    conv = _latest_active_whatsapp(get_db()[COLLECTION], _to_e164(phone))
    if conv and not _is_stale(conv, datetime.now(UTC)):
        return conv["externalId"]
    return _new_conversation_id()


async def resolve_conversation_id(phone: str) -> str:
    return await asyncio.to_thread(_resolve_conversation_id_sync, phone)


def _record_turn_sync(
    phone: str,
    patient_text: str,
    agent_reply: str,
    tool_calls: list[dict],
    latency_ms: int | None,
    conversation_id: str | None = None,
) -> str:
    """Record one turn; return the conversation's ``externalId``.

    ``conversation_id`` (from ``resolve_conversation_id``) names the conversation
    if a new one has to be started.
    """
    col = get_db()[COLLECTION]
    now = datetime.now(UTC)
    e164 = _to_e164(phone)

    conv = _latest_active_whatsapp(col, e164)
    if conv and _is_stale(conv, now):
        # Idle window elapsed → finalize the stale conversation, start fresh.
        col.update_one(
            {"externalId": conv["externalId"]},
            {"$set": {"status": _final_status(_calls_from_messages(conv.get("messages", [])))}},
        )
        conv = None

    if conv is None:
        conv = {
            "externalId": conversation_id or _new_conversation_id(),
            "clinicId": settings.CLINIC_ID,
            "channel": CHANNEL,
            "phone": e164,
            "patientName": _patient_name(phone),
            "startedAt": now.isoformat(),
            "endedAt": now.isoformat(),
            "status": "active",
            "messages": [],
            "intents": [],
            "tags": [],
        }
        col.insert_one(dict(conv))

    all_calls = _calls_from_messages(conv.get("messages", [])) + tool_calls
    turn_messages = _build_turn_messages(patient_text, agent_reply, tool_calls, latency_ms, now)

    col.update_one(
        {"externalId": conv["externalId"]},
        {
            "$push": {"messages": {"$each": turn_messages}},
            "$set": {
                "endedAt": now.isoformat(),
                "patientName": _patient_name(phone),
                **_derived_fields(all_calls),
                # Stay OPEN through escalation — the `escalated` flag drives the
                # console follow-up queue; the final "escalated" status is set
                # when the sweeper closes the conversation on idle. This keeps a
                # continued chat in ONE conversation instead of splitting it.
                "status": "active",
            },
        },
    )
    return conv["externalId"]


async def record_turn(
    phone: str,
    patient_text: str,
    agent_reply: str,
    tool_calls: list[dict],
    latency_ms: int | None,
    conversation_id: str | None = None,
) -> str:
    """Append one turn to the phone's open conversation (creating/rotating it)."""
    return await asyncio.to_thread(
        _record_turn_sync,
        phone,
        patient_text,
        agent_reply,
        tool_calls,
        latency_ms,
        conversation_id,
    )


# ---- voice calls (one conversation per call) ------------------------------


def _start_call_sync(phone: str, call_id: str, started_at: datetime) -> str:
    external_id = f"conv_{uuid.uuid4().hex}"
    ts = started_at.isoformat()
    get_db()[COLLECTION].insert_one(
        {
            "externalId": external_id,
            "clinicId": settings.CLINIC_ID,
            "channel": VOICE_CHANNEL,
            "callId": call_id,
            "phone": _to_e164(phone) if phone else "",
            "patientName": _patient_name(phone) if phone else "Unknown",
            "startedAt": ts,
            "endedAt": ts,
            "status": "active",
            "messages": [],
            "intents": [],
            "tags": [],
        }
    )
    return external_id


def _voice_turn_messages(turn: dict) -> list[dict]:
    """Console messages for one voice turn; empty text lines are dropped."""
    user = turn.get("user", "")
    msgs = _build_turn_messages(
        "" if user == CALL_STARTED else user,
        turn.get("assistant", ""),
        turn.get("tools") or [],
        None,
        turn.get("ts") or datetime.now(UTC),
    )
    return [m for m in msgs if m["type"] != "text" or m["content"]]


def _finish_call_sync(
    external_id: str,
    phone: str,
    turns: list[dict],
    started_at: datetime,
    ended_at: datetime,
) -> None:
    messages = [m for t in turns for m in _voice_turn_messages(t)]
    all_calls = [c for t in turns for c in t.get("tools") or []]
    get_db()[COLLECTION].update_one(
        {"externalId": external_id},
        {
            "$set": {
                "messages": messages,
                "endedAt": ended_at.isoformat(),
                "durationSec": max(0, round((ended_at - started_at).total_seconds())),
                "patientName": _patient_name(phone) if phone else "Unknown",
                **_derived_fields(all_calls),
                # Finalized on hangup — immediately eligible for analytics.
                "status": _final_status(all_calls),
            },
            # If the sweeper closed (and analysed) it mid-call, redo analytics on
            # the full call.
            "$unset": {"analyticsStatus": "", "analyticsStartedAt": ""},
        },
    )


async def start_call(phone: str, call_id: str, started_at: datetime) -> str:
    """Open the (single) ``active`` voice conversation for a call; return its id."""
    return await asyncio.to_thread(_start_call_sync, phone, call_id, started_at)


async def finish_call(
    external_id: str,
    phone: str,
    turns: list[dict],
    started_at: datetime,
    ended_at: datetime,
) -> None:
    """Write a finished call's turns + derived fields and finalize its status.

    ``turns``: ``[{user, assistant, tools: [tool-call records], ts}]`` in order.
    """
    await asyncio.to_thread(_finish_call_sync, external_id, phone, turns, started_at, ended_at)
