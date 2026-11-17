"""Langfuse scores: what each conversation / trace is scored with, and when."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import mongomock
import pytest

from clinic_agent import observability
from clinic_agent.runtime import conversations as conv

PHONE = "919876543210"

BOOK_OK = {
    "name": "book_appointment",
    "args": {"doctor_name": "Asha Mehta", "date": "2026-10-14", "slot": "09:30"},
    "result": {"success": True},
    "status": "success",
}
BOOK_ERR = {**BOOK_OK, "result": {"error": "slot taken"}, "status": "error"}
RESCHEDULE_OK = {
    "name": "reschedule_appointment",
    "args": {"appointment_id": "abc"},
    "result": {"success": True},
    "status": "success",
}
INFO_OK = {"name": "list_doctors", "args": {}, "result": {"doctors": []}, "status": "success"}
ESCALATE_OK = {
    "name": "escalate_to_human",
    "args": {"reason": "billing"},
    "result": {"escalated": True},
    "status": "success",
}


def _by_name(scores: list[dict]) -> dict[str, dict]:
    return {s["name"]: s for s in scores}


# ---- conversation_scores (pure) ---------------------------------------------


def test_successful_booking_scores():
    s = _by_name(conv.conversation_scores([INFO_OK, BOOK_OK]))
    assert s["conversation_outcome"]["value"] == "appointment_booked"
    assert s["primary_intent"]["value"] == "book_appointment"
    assert s["escalated"]["value"] == 0
    assert s["booking_succeeded"] == {
        "name": "booking_succeeded",
        "value": 1,
        "data_type": "BOOLEAN",
        "comment": "book_appointment",
    }


def test_failed_booking_scores_zero():
    s = _by_name(conv.conversation_scores([BOOK_ERR]))
    assert s["booking_succeeded"]["value"] == 0
    assert s["conversation_outcome"]["value"] == "no_resolution"


def test_reschedule_counts_as_booking():
    s = _by_name(conv.conversation_scores([RESCHEDULE_OK]))
    assert s["booking_succeeded"]["value"] == 1
    assert s["booking_succeeded"]["comment"] == "reschedule_appointment"


def test_no_booking_score_when_patient_was_not_booking():
    s = _by_name(conv.conversation_scores([INFO_OK]))
    assert "booking_succeeded" not in s
    assert s["conversation_outcome"]["value"] == "info_provided"


def test_escalation_scored_with_reason():
    s = _by_name(conv.conversation_scores([ESCALATE_OK]))
    assert s["escalated"]["value"] == 1
    assert s["escalated"]["comment"] == "billing"


# ---- observability helpers --------------------------------------------------


class _FakeClient:
    def __init__(self):
        self.scores: list[dict] = []

    def create_score(self, **kw):
        self.scores.append(kw)


class _FakeObs:
    def __init__(self):
        self.scores: list[dict] = []

    def score_trace(self, **kw):
        self.scores.append(kw)


def test_score_session_is_idempotent_and_masked(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(observability, "_client", client)
    observability.score_session(
        "conv_1", "escalated", 1, data_type="BOOLEAN", comment=f"call {PHONE}"
    )
    (score,) = client.scores
    assert score["session_id"] == "conv_1"
    assert score["score_id"] == "conv_1-escalated"  # re-scoring overwrites
    assert score["data_type"] == "BOOLEAN"
    assert PHONE not in score["comment"]


def test_score_session_noop_when_off_or_no_session(monkeypatch):
    monkeypatch.setattr(observability, "_client", None)
    observability.score_session("conv_1", "x", 1, data_type="BOOLEAN")
    client = _FakeClient()
    monkeypatch.setattr(observability, "_client", client)
    observability.score_session(None, "x", 1, data_type="BOOLEAN")
    assert client.scores == []


def test_score_session_swallows_errors(monkeypatch):
    class Boom:
        def create_score(self, **_):
            raise RuntimeError("network")

    monkeypatch.setattr(observability, "_client", Boom())
    observability.score_session("conv_1", "x", 1, data_type="BOOLEAN")  # no raise


def test_tool_error_scored_only_when_tools_ran():
    obs = _FakeObs()
    observability.score_tool_errors(obs, [])
    assert obs.scores == []

    observability.score_tool_errors(obs, [INFO_OK])
    observability.score_tool_errors(obs, [INFO_OK, BOOK_ERR])
    assert [s["value"] for s in obs.scores] == [0, 1]
    assert obs.scores[1]["comment"] == "book_appointment"
    assert all(s["name"] == "tool_error" and s["data_type"] == "BOOLEAN" for s in obs.scores)


def test_noop_observation_accepts_score_trace(monkeypatch):
    monkeypatch.setattr(observability, "_client", None)
    with observability.trace_root("t") as obs:
        observability.score_tool_errors(obs, [BOOK_ERR])


# ---- conversations are scored when they are finalized -----------------------


@pytest.fixture
def db(monkeypatch):
    fake = mongomock.MongoClient()["clinic-test"]
    monkeypatch.setattr(conv, "get_db", lambda: fake)
    return fake


@pytest.fixture
def sent(monkeypatch):
    out: list[tuple[str, str, object]] = []
    monkeypatch.setattr(
        observability,
        "score_session",
        lambda sid, name, value, **_: out.append((sid, name, value)),
    )
    return out


def _ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def test_open_conversation_is_not_scored(db, sent):
    conv._record_turn_sync("919", "book", "Booked!", [BOOK_OK], 100)
    assert sent == []


def test_sweeper_close_scores_conversation(db, sent):
    conv._record_turn_sync("919", "book", "Booked!", [BOOK_OK], 100)
    db["conversations"].update_one({"phone": "+919"}, {"$set": {"endedAt": _ago(45)}})
    conv.close_idle_active(_ago(30))
    ext = db["conversations"].find_one({"phone": "+919"})["externalId"]
    assert (ext, "booking_succeeded", 1) in sent
    assert (ext, "conversation_outcome", "appointment_booked") in sent


def test_idle_rotation_scores_previous_conversation(db, sent):
    conv._record_turn_sync("919", "book", "Booked!", [BOOK_OK], 100)
    doc = db["conversations"].find_one({"phone": "+919"})
    doc["messages"][-1]["timestamp"] = _ago(45)
    db["conversations"].update_one(
        {"externalId": doc["externalId"]}, {"$set": {"messages": doc["messages"]}}
    )
    conv._record_turn_sync("919", "back", "welcome back", [], 100)
    assert (doc["externalId"], "booking_succeeded", 1) in sent
    assert {sid for sid, _, _ in sent} == {doc["externalId"]}  # new one not scored yet


def test_finished_call_is_scored(db, sent):
    started = datetime.now(UTC)
    ext = conv._start_call_sync("919", "call_1", started)
    turns = [{"user": "book me", "assistant": "Done", "tools": [BOOK_OK], "ts": started}]
    conv._finish_call_sync(ext, "919", turns, started, started + timedelta(seconds=40))
    assert (ext, "booking_succeeded", 1) in sent
