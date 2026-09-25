"""Per-phone conversation history in MongoDB (prompt-seeding only).

The agent runs statelessly each turn; prior turns are stored here and re-seeded
into the prompt (see ``turn.contextualize``). One append-only document per phone.

Per the console data contract this ``transcripts`` collection is the agent's own
prompt-history store and is NOT read by the console — the console reads the
separate ``conversations`` collection (see ``runtime/conversations.py``). Keep
this lean: role/content/ts only.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from clinic_agent.db import get_db

# Cap how many prior messages we replay, to bound prompt tokens.
MAX_HISTORY_MESSAGES = 20

_COLLECTION = "transcripts"


def _get_history_sync(phone: str) -> list[dict]:
    doc = get_db()[_COLLECTION].find_one({"phone": phone}, {"messages": 1})
    messages = (doc or {}).get("messages", [])
    return messages[-MAX_HISTORY_MESSAGES:]


def _append_turn_sync(phone: str, user_text: str, assistant_text: str) -> None:
    now = datetime.now(UTC)
    get_db()[_COLLECTION].update_one(
        {"phone": phone},
        {
            "$push": {
                "messages": {
                    "$each": [
                        {"role": "user", "content": user_text, "ts": now},
                        {"role": "assistant", "content": assistant_text, "ts": now},
                    ]
                }
            },
            "$set": {"updatedAt": now},
            "$setOnInsert": {"phone": phone, "createdAt": now},
        },
        upsert=True,
    )


async def get_history(phone: str) -> list[dict]:
    """Return the most recent prior messages for a phone (oldest → newest)."""
    return await asyncio.to_thread(_get_history_sync, phone)


async def append_turn(phone: str, user_text: str, assistant_text: str) -> None:
    """Persist the user message + the agent's reply for prompt-history seeding."""
    await asyncio.to_thread(_append_turn_sync, phone, user_text, assistant_text)
