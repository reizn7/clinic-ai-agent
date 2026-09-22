"""Per-phone conversation history in MongoDB.

The agent runs statelessly each turn; prior turns are stored here and re-seeded
into the prompt (see ``turn.contextualize``). One document per phone number.
"""

from __future__ import annotations

import asyncio

from clinic_agent.db import get_db

# Cap how many prior messages we replay, to bound prompt tokens.
MAX_HISTORY_MESSAGES = 20

_COLLECTION = "transcripts"


def _get_history_sync(phone: str) -> list[dict]:
    doc = get_db()[_COLLECTION].find_one({"phone": phone}, {"messages": 1})
    messages = (doc or {}).get("messages", [])
    return messages[-MAX_HISTORY_MESSAGES:]


def _append_turn_sync(phone: str, user_text: str, assistant_text: str) -> None:
    get_db()[_COLLECTION].update_one(
        {"phone": phone},
        {
            "$push": {
                "messages": {
                    "$each": [
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": assistant_text},
                    ]
                }
            }
        },
        upsert=True,
    )


async def get_history(phone: str) -> list[dict]:
    """Return the most recent prior messages for a phone (oldest → newest)."""
    return await asyncio.to_thread(_get_history_sync, phone)


async def append_turn(phone: str, user_text: str, assistant_text: str) -> None:
    """Persist the user message + the agent's reply for this turn."""
    await asyncio.to_thread(_append_turn_sync, phone, user_text, assistant_text)
