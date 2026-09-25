from datetime import datetime

import mongomock
import pytest

from clinic_agent.runtime import history


@pytest.fixture
def db(monkeypatch):
    fake = mongomock.MongoClient()["clinic-test"]
    monkeypatch.setattr(history, "get_db", lambda: fake)
    return fake


def test_append_stamps_timestamps(db):
    history._append_turn_sync("919", "hi", "hello there")

    doc = db["transcripts"].find_one({"phone": "919"})
    assert "createdAt" in doc and "updatedAt" in doc
    msgs = doc["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert all(isinstance(m["ts"], datetime) for m in msgs)


def test_get_history_returns_role_content(db):
    history._append_turn_sync("919", "hi", "hello there")
    hist = history._get_history_sync("919")
    assert hist[0]["content"] == "hi"
    assert hist[1]["content"] == "hello there"


def test_history_capped(db, monkeypatch):
    monkeypatch.setattr(history, "MAX_HISTORY_MESSAGES", 4)
    for i in range(5):
        history._append_turn_sync("919", f"u{i}", f"a{i}")
    hist = history._get_history_sync("919")
    assert len(hist) == 4  # last 2 turns
    assert hist[0]["content"] == "u3"
