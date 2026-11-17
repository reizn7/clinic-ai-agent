from __future__ import annotations

from datetime import UTC, datetime, timedelta

import mongomock
import pytest

from clinic_agent.runtime import conversations as conv

BOOK_OK = {
    "name": "book_appointment",
    "args": {"doctor_name": "Asha Mehta", "date": "2026-11-16", "slot": "10:30"},
    "result": {"success": True, "appointmentId": "abc"},
    "status": "success",
    "duration_ms": 40,
}
AVAIL_OK = {
    "name": "check_availability",
    "args": {"doctor_name": "Asha Mehta", "date": "2026-11-16"},
    "result": {"slots": ["10:30"]},
    "status": "success",
    "duration_ms": 20,
}
ESCALATE_OK = {
    "name": "escalate_to_human",
    "args": {"reason": "billing question"},
    "result": {"escalated": True},
    "status": "success",
}

PHONE = "919876543210"


@pytest.fixture
def db(monkeypatch):
    fake = mongomock.MongoClient()["test"]
    monkeypatch.setattr(conv, "get_db", lambda: fake)
    return fake


def _turns(*specs):
    now = datetime.now(UTC)
    return [
        {"user": u, "assistant": a, "tools": list(t), "ts": now + timedelta(seconds=i)}
        for i, (u, a, t) in enumerate(specs)
    ]


async def test_start_call_creates_active_voice_conversation(db):
    started = datetime.now(UTC)
    cid = await conv.start_call(PHONE, "call-1", started)
    doc = db[conv.COLLECTION].find_one({"externalId": cid})
    assert doc["channel"] == "voice"
    assert doc["callId"] == "call-1"
    assert doc["phone"] == "+919876543210"
    assert doc["status"] == "active"
    assert doc["startedAt"] == started.isoformat()
    assert doc["clinicId"]


async def test_finish_call_writes_turns_and_derived_fields(db):
    started = datetime.now(UTC) - timedelta(seconds=95)
    cid = await conv.start_call(PHONE, "call-1", started)
    turns = _turns(
        (conv.CALL_STARTED, "Hello, ABC Clinic.", []),
        ("Book me with Dr Asha Monday", "10:30 is free. Shall I book?", [AVAIL_OK]),
        ("Yes", "You're booked.", [BOOK_OK]),
    )
    await conv.finish_call(cid, PHONE, turns, started, datetime.now(UTC))

    docs = list(db[conv.COLLECTION].find())
    assert len(docs) == 1  # exactly one conversation per call
    doc = docs[0]
    assert doc["status"] == "completed"
    assert doc["outcome"] == "appointment_booked"
    assert doc["primaryIntent"] == "doctor_availability"
    assert doc["intents"] == ["doctor_availability", "book_appointment"]
    assert doc["doctorName"] == "Asha Mehta"
    assert doc["appointmentAt"] == "2026-11-16T10:30:00"
    assert doc["escalated"] is False
    assert 94 <= doc["durationSec"] <= 97

    msgs = doc["messages"]
    # The greeting turn has no caller speech → no empty patient message.
    assert msgs[0] == {
        "role": "agent",
        "type": "text",
        "content": "Hello, ABC Clinic.",
        "timestamp": msgs[0]["timestamp"],
    }
    assert [m["type"] for m in msgs].count("tool_call") == 2
    assert (msgs[1]["role"], msgs[1]["type"]) == ("patient", "text")
    assert msgs[1]["content"] == "Book me with Dr Asha Monday"
    assert all(m["content"] for m in msgs if m["type"] == "text")


async def test_escalated_call_status(db):
    started = datetime.now(UTC)
    cid = await conv.start_call(PHONE, "call-2", started)
    turns = _turns(("I have a billing question", "The clinic will call you back.", [ESCALATE_OK]))
    await conv.finish_call(cid, PHONE, turns, started, datetime.now(UTC))
    doc = db[conv.COLLECTION].find_one({"externalId": cid})
    assert doc["status"] == "escalated"
    assert doc["escalated"] is True
    assert doc["escalationReason"] == "billing question"


async def test_empty_call_is_abandoned(db):
    started = datetime.now(UTC)
    cid = await conv.start_call(PHONE, "call-3", started)
    await conv.finish_call(cid, PHONE, [], started, datetime.now(UTC))
    doc = db[conv.COLLECTION].find_one({"externalId": cid})
    assert doc["status"] == "abandoned"
    assert doc["outcome"] == "no_resolution"
    assert doc["messages"] == []


async def test_finished_call_is_eligible_for_analytics(db):
    started = datetime.now(UTC)
    cid = await conv.start_call(PHONE, "call-4", started)
    # simulate the sweeper having analysed a stale snapshot mid-call
    db[conv.COLLECTION].update_one({"externalId": cid}, {"$set": {"analyticsStatus": "complete"}})
    turns = _turns(("Hi", "Hello!", []))
    await conv.finish_call(cid, PHONE, turns, started, datetime.now(UTC))
    stuck = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    claimed = conv.claim_analytics_batch(stuck, 10)
    assert [c[0] for c in claimed] == [cid]
    assert "patient: Hi" in claimed[0][1]


async def test_whatsapp_never_appends_to_active_voice_call(db):
    started = datetime.now(UTC)
    cid = await conv.start_call(PHONE, "call-5", started)
    await conv.record_turn(PHONE, "hi on whatsapp", "hello", [], 100)

    voice = db[conv.COLLECTION].find_one({"externalId": cid})
    assert voice["messages"] == []  # untouched
    wa = db[conv.COLLECTION].find_one({"channel": "whatsapp"})
    assert wa is not None and wa["externalId"] != cid
    assert wa["phone"] == "+919876543210"
    assert len(wa["messages"]) == 2

    # and a second WhatsApp turn still lands in the same WhatsApp conversation
    await conv.record_turn(PHONE, "again", "sure", [], 100)
    assert db[conv.COLLECTION].count_documents({"channel": "whatsapp"}) == 1
