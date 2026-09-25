from datetime import UTC, datetime, timedelta

import mongomock
import pytest

from clinic_agent.runtime import conversations as conv
from clinic_agent.runtime.analytics import ConversationAnalytics

INFO_OK = {"name": "list_doctors", "args": {}, "result": {"doctors": []}, "status": "success"}


@pytest.fixture
def db(monkeypatch):
    fake = mongomock.MongoClient()["clinic-test"]
    monkeypatch.setattr(conv, "get_db", lambda: fake)
    return fake


def _iso(minutes_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def test_render_transcript_skips_tool_calls():
    messages = [
        {"role": "patient", "type": "text", "content": "hi"},
        {"role": "agent", "type": "tool_call", "content": "list_doctors()"},
        {"role": "agent", "type": "text", "content": "We have Dr Asha."},
    ]
    assert conv._render_transcript(messages) == "patient: hi\nagent: We have Dr Asha."


def test_close_idle_active_only_closes_stale(db):
    conv._record_turn_sync("111", "hi", "hello", [INFO_OK], 100)  # fresh, stays active
    conv._record_turn_sync("222", "hi", "hello", [INFO_OK], 100)
    # Backdate 222 beyond the idle window.
    db["conversations"].update_one({"phone": "+222"}, {"$set": {"endedAt": _iso(45)}})

    closed = conv.close_idle_active(_iso(30))
    assert closed == 1
    assert db["conversations"].find_one({"phone": "+111"})["status"] == "active"
    assert db["conversations"].find_one({"phone": "+222"})["status"] == "completed"


def test_claim_and_apply_analytics_roundtrip(db):
    conv._record_turn_sync("222", "book me in", "Booked!", [INFO_OK], 100)
    db["conversations"].update_one({"phone": "+222"}, {"$set": {"status": "completed"}})

    batch = conv.claim_analytics_batch(_iso(10), 10)
    assert len(batch) == 1
    external_id, transcript = batch[0]
    assert "book me in" in transcript

    # Claimed docs are now "processing" → a second claim returns nothing.
    assert conv.claim_analytics_batch(_iso(10), 10) == []

    conv.apply_analytics(
        external_id,
        ConversationAnalytics(summary="Booked.", sentiment="positive", language="en"),
    )
    doc = db["conversations"].find_one({"externalId": external_id})
    assert doc["analyticsStatus"] == "complete"
    assert doc["sentiment"] == "positive"
    assert doc["language"] == "en"
    assert doc["summary"] == "Booked."


def test_active_conversations_are_not_claimed(db):
    conv._record_turn_sync("222", "hi", "hello", [INFO_OK], 100)  # stays active
    assert conv.claim_analytics_batch(_iso(10), 10) == []


def test_apply_analytics_failure_marks_failed(db):
    conv._record_turn_sync("222", "hi", "hello", [INFO_OK], 100)
    db["conversations"].update_one({"phone": "+222"}, {"$set": {"status": "completed"}})
    external_id = conv.claim_analytics_batch(_iso(10), 10)[0][0]

    conv.apply_analytics(external_id, None)
    assert db["conversations"].find_one({"externalId": external_id})["analyticsStatus"] == "failed"
