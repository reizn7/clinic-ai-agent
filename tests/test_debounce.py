from datetime import UTC, datetime, timedelta

import mongomock
import pytest

from clinic_agent.runtime import debounce


@pytest.fixture
def db(monkeypatch):
    fake = mongomock.MongoClient()["clinic-test"]
    monkeypatch.setattr(debounce, "get_db", lambda: fake)
    return fake


def test_enqueue_accumulates_and_pushes_deadline_out(db):
    t0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)
    debounce._enqueue_sync("919", "I want", t0)
    debounce._enqueue_sync("919", "to book", t0 + timedelta(seconds=1))

    doc = db[debounce.COLLECTION].find_one({"phone": "919"})
    assert doc["messages"] == ["I want", "to book"]
    # firstAt is stamped once; deadline follows the latest message.
    assert doc["firstAt"] == t0.isoformat()
    assert doc["deadline"] == (t0 + timedelta(seconds=6)).isoformat()  # +1s +5s window


def test_not_due_within_window(db):
    t0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)
    debounce._enqueue_sync("919", "hi", t0)
    # Only 2s later — still inside the 5s window.
    assert debounce._claim_due_sync(t0 + timedelta(seconds=2)) == []
    assert db[debounce.COLLECTION].find_one({"phone": "919"}) is not None  # still buffered


def test_due_after_quiet_window_joined(db):
    t0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)
    debounce._enqueue_sync("919", "I want", t0)
    debounce._enqueue_sync("919", "to book with Asha", t0 + timedelta(seconds=1))

    claimed = debounce._claim_due_sync(t0 + timedelta(seconds=7))  # >1s+5s
    assert claimed == [("919", ["I want", "to book with Asha"])]
    # Claim removes the buffer (processed exactly once).
    assert db[debounce.COLLECTION].find_one({"phone": "919"}) is None


def test_max_wait_cap_forces_flush_while_still_typing(db):
    t0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)
    debounce._enqueue_sync("919", "one", t0)
    # A fresh bubble at +29s keeps pushing the deadline, but firstAt hit the cap.
    debounce._enqueue_sync("919", "two", t0 + timedelta(seconds=29))
    claimed = debounce._claim_due_sync(t0 + timedelta(seconds=31))  # >30s cap
    assert claimed == [("919", ["one", "two"])]


def test_requeue_prepends_for_retry(db):
    t0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)
    debounce._enqueue_sync("919", "new msg", t0)
    debounce._requeue_sync("919", ["failed1", "failed2"], t0)
    doc = db[debounce.COLLECTION].find_one({"phone": "919"})
    assert doc["messages"] == ["failed1", "failed2", "new msg"]
