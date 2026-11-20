from datetime import UTC, datetime, timedelta

import mongomock
import pytest

from clinic_agent.runtime import conversations as conv

BOOK_OK = {
    "name": "book_appointment",
    "args": {"doctor_name": "Asha Mehta", "date": "2026-10-14", "slot": "09:30"},
    "result": {"success": True, "appointmentId": "abc"},
    "status": "success",
    "duration_ms": 12,
}
CANCEL_OK = {
    "name": "cancel_appointment",
    "args": {"appointment_id": "abc"},
    "result": {"success": True},
    "status": "success",
    "duration_ms": 8,
}
INFO_OK = {"name": "list_doctors", "args": {}, "result": {"doctors": []}, "status": "success"}
ESCALATE_OK = {
    "name": "escalate_to_human",
    "args": {"reason": "billing"},
    "result": {"escalated": True},
    "status": "success",
}
CHECK_ERR = {
    "name": "check_availability",
    "args": {"doctor_name": "Ghost", "date": "2026-10-14"},
    "result": {"error": "No active doctor named 'Ghost'."},
    "status": "error",
    "duration_ms": 5,
}


# ---- pure derivations -----------------------------------------------------


RESCHEDULE_OK = {
    "name": "reschedule_appointment",
    "args": {"appointment_id": "abc", "new_date": "2026-10-15", "new_slot": "10:00"},
    "result": {"success": True},
    "status": "success",
}


def test_derive_outcome_variants():
    assert conv.derive_outcome([BOOK_OK]) == "appointment_booked"
    assert conv.derive_outcome([CANCEL_OK]) == "appointment_cancelled"
    assert conv.derive_outcome([CANCEL_OK, BOOK_OK]) == "appointment_rescheduled"
    assert conv.derive_outcome([RESCHEDULE_OK]) == "appointment_rescheduled"
    assert conv.derive_outcome([ESCALATE_OK]) == "escalated"
    assert conv.derive_outcome([INFO_OK]) == "info_provided"
    assert conv.derive_outcome([CHECK_ERR]) == "no_resolution"
    assert conv.derive_outcome([]) == "no_resolution"


def test_reschedule_intent_mapped():
    assert conv.derive_intents([RESCHEDULE_OK]) == ["reschedule_appointment"]


def test_derive_intents_ordered_unique():
    calls = [INFO_OK, BOOK_OK, BOOK_OK]
    assert conv.derive_intents(calls) == ["clinic_info", "book_appointment"]


def test_primary_intent_prefers_goal_over_lookup():
    # Lookups first, booking last: the call is still a booking.
    assert conv.derive_primary_intent([INFO_OK, BOOK_OK]) == "book_appointment"
    assert conv.derive_primary_intent([INFO_OK]) == "clinic_info"
    assert conv.derive_primary_intent([]) == "other"


def test_escalation_reason():
    assert conv.escalation_reason([ESCALATE_OK]) == "billing"
    assert conv.escalation_reason([BOOK_OK]) is None


# ---- the sessionised writer ----------------------------------------------


@pytest.fixture
def db(monkeypatch):
    fake = mongomock.MongoClient()["clinic-test"]
    monkeypatch.setattr(conv, "get_db", lambda: fake)
    return fake


def test_first_turn_creates_conversation_with_contract_shape(db):
    conv._record_turn_sync("919", "book with Asha", "Booked!", [BOOK_OK], 940)

    doc = db["conversations"].find_one({"phone": "+919"})
    assert doc["clinicId"] == "main-clinic"
    assert doc["channel"] == "whatsapp"
    assert doc["status"] == "active"
    assert doc["outcome"] == "appointment_booked"
    assert doc["primaryIntent"] == "book_appointment"
    assert doc["doctorName"] == "Asha Mehta"
    assert doc["appointmentAt"] == "2026-10-14T04:00:00+00:00"

    roles = [(m["role"], m["type"]) for m in doc["messages"]]
    assert roles == [("patient", "text"), ("agent", "tool_call"), ("agent", "text")]
    tool_msg = doc["messages"][1]
    assert tool_msg["tool"]["name"] == "book_appointment"
    assert tool_msg["tool"]["durationMs"] == 12
    assert doc["messages"][2]["latencyMs"] == 940


def test_second_turn_appends_same_conversation(db):
    conv._record_turn_sync("919", "hi", "hello", [], 100)
    conv._record_turn_sync("919", "who's there", "Dr Asha", [INFO_OK], 120)

    docs = list(db["conversations"].find({"phone": "+919"}))
    assert len(docs) == 1
    assert len(docs[0]["messages"]) == 5  # 2 patient + 1 tool + 2 agent text


def test_error_tool_call_records_error_and_null_result(db):
    conv._record_turn_sync("919", "any ghost slots?", "No such doctor.", [CHECK_ERR], 200)
    doc = db["conversations"].find_one({"phone": "+919"})
    tool = doc["messages"][1]["tool"]
    assert tool["status"] == "error"
    assert tool["result"] is None
    assert tool["error"] == "No active doctor named 'Ghost'."


def test_escalation_flags_but_keeps_conversation_open(db):
    conv._record_turn_sync("919", "get me a person", "Connecting you.", [ESCALATE_OK], 90)
    doc = db["conversations"].find_one({"phone": "+919"})
    # Flagged for the follow-up queue, but NOT closed — a continued chat stays
    # in this same conversation (final "escalated" status is set at idle close).
    assert doc["escalated"] is True
    assert doc["escalationReason"] == "billing"
    assert doc["status"] == "active"


def test_continued_chat_after_escalation_stays_one_conversation(db):
    conv._record_turn_sync("919", "get me a person", "Connecting you.", [ESCALATE_OK], 90)
    conv._record_turn_sync("919", "actually, book me instead", "Sure!", [BOOK_OK], 80)
    docs = list(db["conversations"].find({"phone": "+919"}))
    assert len(docs) == 1  # not split into two
    assert conv._final_status(conv._calls_from_messages(docs[0]["messages"])) == "escalated"


def test_idle_timeout_rotates_conversation(db):
    conv._record_turn_sync("919", "hi", "hello", [INFO_OK], 100)

    # Backdate the open conversation's last message beyond the idle window.
    old = (datetime.now(UTC) - timedelta(minutes=45)).isoformat()
    doc = db["conversations"].find_one({"phone": "+919"})
    doc["messages"][-1]["timestamp"] = old
    db["conversations"].update_one(
        {"externalId": doc["externalId"]}, {"$set": {"messages": doc["messages"]}}
    )

    conv._record_turn_sync("919", "back again", "welcome back", [], 100)

    docs = list(db["conversations"].find({"phone": "+919"}).sort("startedAt", 1))
    assert len(docs) == 2
    # The first was finalized (info_provided → completed), the new one is active.
    assert docs[0]["status"] == "completed"
    assert docs[1]["status"] == "active"


# ---- conversation id resolved before a turn (Langfuse session id) -----------


def test_resolved_id_names_the_new_conversation(db):
    cid = conv._resolve_conversation_id_sync("919")
    assert cid.startswith("conv_")
    assert db["conversations"].count_documents({}) == 0  # read-only

    recorded = conv._record_turn_sync("919", "hi", "hello", [], 100, cid)
    assert recorded == cid
    assert db["conversations"].find_one({"phone": "+919"})["externalId"] == cid


def test_resolved_id_is_the_open_conversation(db):
    first = conv._record_turn_sync("919", "hi", "hello", [], 100)
    assert conv._resolve_conversation_id_sync("919") == first
    # A turn in the same window stays in that conversation, whatever id is hinted.
    assert conv._record_turn_sync("919", "again", "ok", [], 100, first) == first


def test_resolved_id_is_fresh_after_idle_window(db):
    first = conv._record_turn_sync("919", "hi", "hello", [INFO_OK], 100)
    doc = db["conversations"].find_one({"externalId": first})
    doc["messages"][-1]["timestamp"] = (datetime.now(UTC) - timedelta(minutes=45)).isoformat()
    db["conversations"].update_one({"externalId": first}, {"$set": {"messages": doc["messages"]}})

    cid = conv._resolve_conversation_id_sync("919")
    assert cid != first
    assert conv._record_turn_sync("919", "back", "welcome", [], 100, cid) == cid
