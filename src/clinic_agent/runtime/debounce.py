"""Inbound message debounce — coalesce rapid WhatsApp bubbles into one turn.

Replicates nextdim's DB-backed debounce (a Mongo buffer per phone + a flusher
worker), adapted to a single-service in-process loop:

  * enqueue: buffer the message and push the ``deadline`` out by the quiet window
    on every new bubble; stamp ``firstAt`` once so a hard ``maxWait`` cap can fire
    even while the patient keeps typing.
  * claim: the flusher (``runtime/flusher.py``) atomically removes each due buffer
    (``find_one_and_delete``) so a batch is processed exactly once, then joins the
    messages with newlines into a single agent turn.
  * requeue: on a transient failure the batch is pushed back to the front for the
    next tick.

Crash-safety note: unlike nextdim's cross-worker in-flight lock, this relies on a
single instance (same assumption as the idle sweeper). A hard crash mid-flush can
drop one in-flight batch — acceptable for a single-instance deploy.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from clinic_agent.config import settings
from clinic_agent.db import get_db

COLLECTION = "pending_messages"


def ensure_indexes() -> None:
    col = get_db()[COLLECTION]
    col.create_index("phone", unique=True, name="uniq_pending_phone")
    col.create_index("deadline", name="pending_deadline")


def _enqueue_sync(phone: str, text: str, now: datetime) -> None:
    deadline = (now + timedelta(seconds=settings.DEBOUNCE_WINDOW_SECONDS)).isoformat()
    get_db()[COLLECTION].update_one(
        {"phone": phone},
        {
            "$push": {"messages": text},
            "$set": {"deadline": deadline},
            "$setOnInsert": {"phone": phone, "firstAt": now.isoformat()},
        },
        upsert=True,
    )


def _claim_due_sync(now: datetime) -> list[tuple[str, list[str]]]:
    col = get_db()[COLLECTION]
    now_iso = now.isoformat()
    cap_cutoff = (now - timedelta(seconds=settings.DEBOUNCE_MAX_WAIT_SECONDS)).isoformat()
    query = {"$or": [{"deadline": {"$lte": now_iso}}, {"firstAt": {"$lte": cap_cutoff}}]}

    out: list[tuple[str, list[str]]] = []
    while True:
        doc = col.find_one_and_delete(query)
        if not doc:
            break
        messages = doc.get("messages", [])
        if messages:
            out.append((doc["phone"], messages))
    return out


def _requeue_sync(phone: str, messages: list[str], now: datetime) -> None:
    deadline = (now + timedelta(seconds=settings.DEBOUNCE_WINDOW_SECONDS)).isoformat()
    get_db()[COLLECTION].update_one(
        {"phone": phone},
        {
            "$push": {"messages": {"$each": messages, "$position": 0}},
            "$set": {"deadline": deadline},
            "$setOnInsert": {"phone": phone, "firstAt": now.isoformat()},
        },
        upsert=True,
    )


async def enqueue(phone: str, text: str) -> None:
    """Buffer an inbound message, resetting the quiet window."""
    await asyncio.to_thread(_enqueue_sync, phone, text, datetime.now(UTC))


async def claim_due() -> list[tuple[str, list[str]]]:
    """Remove and return every buffer whose quiet window (or cap) has elapsed."""
    return await asyncio.to_thread(_claim_due_sync, datetime.now(UTC))


async def requeue(phone: str, messages: list[str]) -> None:
    """Return a failed batch to the front of the buffer for a later retry."""
    await asyncio.to_thread(_requeue_sync, phone, messages, datetime.now(UTC))
