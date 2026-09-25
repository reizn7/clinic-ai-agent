"""Console-facing ``conversations`` collection writer.

Implements the staff-console data contract: one document per *conversation*
(not per phone), sessionised by a 30-minute idle window, written to a separate
``conversations`` collection alongside the agent's own ``transcripts``.

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
from clinic_agent.db import get_db

if TYPE_CHECKING:
    from clinic_agent.runtime.analytics import ConversationAnalytics

COLLECTION = "conversations"
IDLE_TIMEOUT = timedelta(minutes=30)
CHANNEL = "whatsapp"

# tool name → primaryIntent enum (closed set in the contract §4).
_TOOL_INTENT = {
    "book_appointment": "book_appointment",
    "cancel_appointment": "cancel_appointment",
    "check_availability": "doctor_availability",
    "get_clinic_info": "clinic_info",
    "list_doctors": "clinic_info",
    "list_my_appointments": "other",
    "escalate_to_human": "other",
}
_INFO_TOOLS = {"get_clinic_info", "list_doctors", "list_my_appointments"}


def ensure_indexes() -> None:
    col = get_db()[COLLECTION]
    col.create_index("externalId", unique=True, name="uniq_externalId")
    col.create_index([("clinicId", 1), ("startedAt", -1)], name="clinic_started")
    col.create_index([("clinicId", 1), ("outcome", 1), ("startedAt", -1)], name="clinic_outcome")
    col.create_index([("clinicId", 1), ("escalated", 1), ("startedAt", -1)], name="clinic_escalated")
    col.create_index([("phone", 1), ("startedAt", -1)], name="phone_started")


# ---- derivations (pure) ---------------------------------------------------


def _to_e164(phone: str) -> str:
    p = phone.strip()
    return p if p.startswith("+") else f"+{p}"


def _succeeded(calls: list[dict], name: str) -> bool:
    return any(c.get("name") == name and c.get("status") == "success" for c in calls)


def derive_outcome(calls: list[dict]) -> str:
    booked = _succeeded(calls, "book_appointment")
    cancelled = _succeeded(calls, "cancel_appointment")
    if booked and cancelled:
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


def _patient_name(phone: str) -> str:
    doc = get_db()["patients"].find_one({"phone": phone}, {"name": 1})
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
    for conv in col.find({"status": "active", "endedAt": {"$lt": idle_cutoff_iso}}):
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


def _record_turn_sync(
    phone: str,
    patient_text: str,
    agent_reply: str,
    tool_calls: list[dict],
    latency_ms: int | None,
) -> None:
    col = get_db()[COLLECTION]
    now = datetime.now(UTC)
    e164 = _to_e164(phone)

    conv = col.find_one({"phone": e164, "status": "active"}, sort=[("startedAt", -1)])

    if conv:
        msgs = conv.get("messages", [])
        last_ts = None
        if msgs:
            try:
                last_ts = datetime.fromisoformat(msgs[-1]["timestamp"])
            except (ValueError, KeyError, TypeError):
                last_ts = None
        if last_ts and now - last_ts > IDLE_TIMEOUT:
            # Idle window elapsed → finalize the stale conversation, start fresh.
            col.update_one(
                {"externalId": conv["externalId"]},
                {"$set": {"status": _final_status(_calls_from_messages(msgs))}},
            )
            conv = None

    if conv is None:
        conv = {
            "externalId": f"conv_{uuid.uuid4().hex}",
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
    escalated = _succeeded(all_calls, "escalate_to_human")

    col.update_one(
        {"externalId": conv["externalId"]},
        {
            "$push": {"messages": {"$each": turn_messages}},
            "$set": {
                "endedAt": now.isoformat(),
                "patientName": _patient_name(phone),
                "outcome": derive_outcome(all_calls),
                "primaryIntent": (derive_intents(all_calls) or ["other"])[0],
                "intents": derive_intents(all_calls),
                "doctorName": _doctor_name(all_calls),
                "appointmentAt": _appointment_at(all_calls),
                "escalated": escalated,
                "escalationReason": escalation_reason(all_calls),
                "status": "escalated" if escalated else "active",
            },
        },
    )


async def record_turn(
    phone: str,
    patient_text: str,
    agent_reply: str,
    tool_calls: list[dict],
    latency_ms: int | None,
) -> None:
    """Append one turn to the phone's open conversation (creating/rotating it)."""
    await asyncio.to_thread(
        _record_turn_sync, phone, patient_text, agent_reply, tool_calls, latency_ms
    )
