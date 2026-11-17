"""Human handoff: escalation records, staff-handled chats, staff replies, the tool."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from clinic_agent.config import settings
from clinic_agent.runtime import conversations, escalation, flusher, followups, history, outbox
from clinic_agent.tools.booking_tools import (
    CALLER_PHONE,
    CHANNEL,
    CONVERSATION_ID,
    escalate_to_human,
)

PATIENT = "919811122233"


@pytest.fixture
def db(_no_real_mongo):
    fake = _no_real_mongo
    fake["patients"].insert_one(
        {"clinicId": settings.CLINIC_ID, "phone": PATIENT, "name": "Ravi Kumar"}
    )
    return fake


def _escalations(db):
    return list(db[escalation.COLLECTION].find())


def _escalate_call(escalation_id: str) -> dict:
    return {
        "name": "escalate_to_human",
        "args": {"reason": "billing"},
        "result": {"escalated": True, "escalation_id": escalation_id},
        "status": "success",
    }


def _staff_chat(db) -> tuple[str, str]:
    """A WhatsApp chat whose turn escalated → (conversation id, escalation id)."""
    conv_id = conversations._resolve_conversation_id_sync(PATIENT)
    esc = escalation.request_handoff(PATIENT, "whatsapp", "billing", conv_id)
    conversations._record_turn_sync(
        PATIENT,
        "I need a person",
        "I've passed this chat to our clinic team.",
        [_escalate_call(esc["escalation_id"])],
        90,
        conv_id,
    )
    return conv_id, esc["escalation_id"]


# ---- request_handoff --------------------------------------------------------


def test_request_handoff_opens_one_escalation(db):
    first = escalation.request_handoff(PATIENT, "voice", " wants to talk about a bill ", "conv_1")
    assert first["already_requested"] is False
    [doc] = _escalations(db)
    assert str(doc["_id"]) == first["escalation_id"]
    assert doc["status"] == "open"
    assert doc["conversationId"] == "conv_1"
    assert doc["channel"] == "voice"
    assert doc["patientPhone"] == "+919811122233"
    assert doc["patientName"] == "Ravi Kumar"
    assert doc["reason"] == "wants to talk about a bill"
    assert doc["claimedBy"] is None and doc["resolvedAt"] is None
    assert isinstance(doc["createdAt"], str)


def test_one_unresolved_escalation_per_conversation(db):
    first = escalation.request_handoff(PATIENT, "voice", "a", "conv_1")
    again = escalation.request_handoff(PATIENT, "voice", "b", "conv_1")
    assert again == {"escalation_id": first["escalation_id"], "already_requested": True}

    db[escalation.COLLECTION].update_one({}, {"$set": {"status": "resolved"}})
    later = escalation.request_handoff(PATIENT, "voice", "c", "conv_1")
    assert later["already_requested"] is False
    assert len(_escalations(db)) == 2


def test_no_staff_alert_jobs_are_queued(db):
    escalation.request_handoff(PATIENT, "whatsapp", "x", "conv_1")
    assert list(db[outbox.COLLECTION].find()) == []


# ---- the escalating turn hands the chat to staff -------------------------------


def test_escalating_turn_hands_chat_to_staff(db):
    conv_id, esc_id = _staff_chat(db)
    doc = db[conversations.COLLECTION].find_one({"externalId": conv_id})
    assert doc["handledBy"] == "staff"
    assert doc["escalationId"] == esc_id
    assert doc["escalatedAt"] and doc["lastPatientMessageAt"]
    assert doc["status"] == "active"


def test_escalation_linked_when_conversation_id_was_unknown(db):
    esc = escalation.request_handoff(PATIENT, "whatsapp", "x", None)
    conv_id = conversations._record_turn_sync(
        PATIENT, "human please", "ok", [_escalate_call(esc["escalation_id"])], 50
    )
    assert _escalations(db)[0]["conversationId"] == conv_id


def test_staff_chat_never_goes_idle(db):
    conv_id, _ = _staff_chat(db)
    old = "2000-01-01T00:00:00+00:00"
    db[conversations.COLLECTION].update_one({"externalId": conv_id}, {"$set": {"endedAt": old}})
    assert conversations.close_idle_active("2100-01-01T00:00:00+00:00") == 0

    doc = db[conversations.COLLECTION].find_one({"externalId": conv_id})
    doc["messages"][-1]["timestamp"] = old
    assert conversations._is_stale(doc, conversations.datetime.now(conversations.UTC)) is False


# ---- inbound while staff handle the chat ----------------------------------------


async def test_messages_go_to_staff_not_the_ai(db, monkeypatch):
    conv_id, _ = _staff_chat(db)

    async def no_turn(*_a, **_k):
        raise AssertionError("the AI must not reply while staff handle the chat")

    async def no_send(*_a, **_k):
        raise AssertionError("nothing may be sent to the patient")

    monkeypatch.setattr(flusher, "run_turn", no_turn)
    monkeypatch.setattr(flusher, "send_text_message", no_send)

    await flusher._flush_one(PATIENT, ["hello?", "anyone there"])

    doc = db[conversations.COLLECTION].find_one({"externalId": conv_id})
    tail = doc["messages"][-2:]
    assert [m["content"] for m in tail] == ["hello?", "anyone there"]
    assert all(m["role"] == "patient" and m["type"] == "text" for m in tail)
    assert doc["lastPatientMessageAt"] == tail[-1]["timestamp"]
    hist = await history.get_history(PATIENT)
    assert [m["content"] for m in hist[-2:]] == ["hello?", "anyone there"]


async def test_ai_answers_again_once_handed_back(db, monkeypatch):
    conv_id, _ = _staff_chat(db)
    db[conversations.COLLECTION].update_one(
        {"externalId": conv_id}, {"$set": {"handledBy": "agent"}}
    )
    assert await escalation.capture_for_staff(PATIENT, ["hi again"]) is False


async def test_no_chat_with_staff_means_normal_turn(db):
    assert await escalation.capture_for_staff(PATIENT, ["hi"]) is False


# ---- staff.reply outbox job ------------------------------------------------------


def _staff_reply(db, conv_id: str, text: str = "Hi Ravi, Priya here.", client_id: str = "c1"):
    """What the console writes: the pending staff message + the outbox job."""
    db[conversations.COLLECTION].update_one(
        {"externalId": conv_id},
        {
            "$push": {
                "messages": {
                    "role": "staff",
                    "type": "text",
                    "content": text,
                    "timestamp": "2026-11-18T10:00:00.000Z",
                    "clientId": client_id,
                    "staff": {"userId": "u1", "name": "Priya"},
                    "delivery": {"status": "pending"},
                }
            }
        },
    )
    db[outbox.COLLECTION].insert_one(
        {
            "clinicId": settings.CLINIC_ID,
            "kind": outbox.STAFF_REPLY,
            "appointmentId": None,
            "status": "pending",
            "attempts": 0,
            "nextAttemptAt": "2000-01-01T00:00:00.000Z",
            "createdAt": "2000-01-01T00:00:00.000Z",
            "updatedAt": "2000-01-01T00:00:00.000Z",
            "conversationId": conv_id,
            "messageClientId": client_id,
            "text": text,
            "staffUserId": "u1",
            "staffName": "Priya",
        }
    )


def _staff_message(db, conv_id: str, client_id: str = "c1") -> dict:
    doc = db[conversations.COLLECTION].find_one({"externalId": conv_id})
    return next(m for m in doc["messages"] if m.get("clientId") == client_id)


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple] = []

    async def text(to, body):
        calls.append((to, body))
        return {"messages": [{"id": "wamid.s1"}]}

    monkeypatch.setattr(escalation.whatsapp, "send_text_message", text)
    return calls


async def test_staff_reply_is_sent_and_marked(db, sent):
    conv_id, _ = _staff_chat(db)
    _staff_reply(db, conv_id)
    assert await outbox.process_one(followups.HANDLERS)

    assert sent == [(PATIENT, "Hi Ravi, Priya here.")]
    delivery = _staff_message(db, conv_id)["delivery"]
    assert delivery["status"] == "sent" and delivery["waMessageId"] == "wamid.s1"
    assert db[outbox.COLLECTION].find_one({"kind": outbox.STAFF_REPLY})["status"] == "done"
    # The AI sees what staff said once the chat is handed back.
    hist = await history.get_history(PATIENT)
    assert hist[-1] == {**hist[-1], "role": "staff", "content": "Hi Ravi, Priya here."}


def _http_error(status: int):
    async def reject(to, body):
        resp = httpx.Response(status, request=httpx.Request("POST", "https://graph"))
        raise httpx.HTTPStatusError(str(status), request=resp.request, response=resp)

    return reject


async def test_rejected_staff_reply_fails_at_once(db, monkeypatch):
    conv_id, _ = _staff_chat(db)
    _staff_reply(db, conv_id)
    monkeypatch.setattr(escalation.whatsapp, "send_text_message", _http_error(400))
    await outbox.process_one(followups.HANDLERS)

    assert db[outbox.COLLECTION].find_one({"kind": outbox.STAFF_REPLY})["status"] == "failed"
    delivery = _staff_message(db, conv_id)["delivery"]
    assert delivery["status"] == "failed" and "400" in delivery["error"]


async def test_transient_error_retries_then_fails_message(db, monkeypatch):
    conv_id, _ = _staff_chat(db)
    _staff_reply(db, conv_id)
    monkeypatch.setattr(escalation.whatsapp, "send_text_message", _http_error(503))

    await outbox.process_one(followups.HANDLERS)
    job = db[outbox.COLLECTION].find_one({"kind": outbox.STAFF_REPLY})
    assert job["status"] == "pending"  # will retry
    assert _staff_message(db, conv_id)["delivery"]["status"] == "pending"

    # Last attempt: the message is marked failed so staff aren't left guessing.
    db[outbox.COLLECTION].update_one(
        {"_id": job["_id"]},
        {"$set": {"attempts": outbox.MAX_ATTEMPTS - 1, "nextAttemptAt": "2000-01-01"}},
    )
    await outbox.process_one(followups.HANDLERS)
    assert db[outbox.COLLECTION].find_one({"_id": job["_id"]})["status"] == "failed"
    assert _staff_message(db, conv_id)["delivery"]["status"] == "failed"


async def test_reply_to_unknown_conversation_fails(db, sent):
    _staff_reply(db, "conv_missing")
    await outbox.process_one(followups.HANDLERS)
    assert sent == []
    assert db[outbox.COLLECTION].find_one({"kind": outbox.STAFF_REPLY})["status"] == "failed"


# ---- the tool ------------------------------------------------------------------


async def test_tool_opens_escalation_for_the_session(db):
    ctx = SimpleNamespace(
        state={CALLER_PHONE: PATIENT, CHANNEL: "voice", CONVERSATION_ID: "conv_9"}
    )
    result = await escalate_to_human("wants a person", ctx)
    assert result["escalated"] is True
    assert "call you back" in result["message"]
    assert result["already_requested"] is False
    [doc] = _escalations(db)
    assert str(doc["_id"]) == result["escalation_id"]
    assert (doc["conversationId"], doc["channel"]) == ("conv_9", "voice")


async def test_tool_whatsapp_message(db):
    ctx = SimpleNamespace(state={CALLER_PHONE: PATIENT, CHANNEL: "whatsapp"})
    result = await escalate_to_human("x", ctx)
    assert "reply to you here" in result["message"]


async def test_tool_never_fails_the_turn(db, monkeypatch):
    def boom(*_a):
        raise RuntimeError("db down")

    monkeypatch.setattr(escalation, "request_handoff", boom)
    ctx = SimpleNamespace(state={CALLER_PHONE: PATIENT, CHANNEL: "whatsapp"})
    result = await escalate_to_human("x", ctx)
    assert result["escalated"] is True and "error" not in result
