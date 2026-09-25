"""Live end-to-end smoke test against real Mongo + Gemini.

Run on a machine with network access to your Atlas cluster (i.e. your laptop,
not a restricted sandbox):

    uv run python scripts/smoke_live.py

It exercises, for real: debounce coalescing → one agent turn (live Gemini +
booking tools + the conversations record) → the idle sweeper's analytics
(summary/sentiment/language). It does NOT send a WhatsApp message, and it
cleans up its own test data. Seed first (`uv run python scripts/seed.py`).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

TEST_PHONE = "15550000001"
E164 = f"+{TEST_PHONE}"


async def main() -> None:
    from clinic_agent.db import get_db
    from clinic_agent.runtime import analytics, conversations, debounce
    from clinic_agent.runtime.turn import run_turn

    db = get_db()
    db.client.admin.command("ping")
    print("✓ Mongo connected\n")

    # 1. Debounce: three quick bubbles buffer into one doc (phone-scoped, so we
    #    never touch other pending buffers).
    now = datetime.now(UTC)
    for i, bubble in enumerate(["hi", "who are your doctors", "and your timings?"]):
        debounce._enqueue_sync(TEST_PHONE, bubble, now + timedelta(seconds=i))
    pending = db[debounce.COLLECTION].find_one_and_delete({"phone": TEST_PHONE})
    combined = pending["messages"]
    print(f"✓ debounce buffered {len(combined)} bubbles → one turn: {combined}\n")

    # 2. A real agent turn (live Gemini + tools + conversation record).
    reply = await run_turn(TEST_PHONE, "\n".join(combined))
    print(f"✓ agent reply:\n  {reply}\n")

    conv = db["conversations"].find_one({"phone": E164})
    tool_names = [m["tool"]["name"] for m in conv["messages"] if m.get("type") == "tool_call"]
    print(
        f"✓ conversation: status={conv['status']} outcome={conv['outcome']} "
        f"intents={conv['intents']} tools={tool_names}\n"
    )

    # 3. Close it + generate analytics for THIS conversation only (the background
    #    sweeper does the same across all ended conversations in production).
    db["conversations"].update_one({"_id": conv["_id"]}, {"$set": {"status": "completed"}})
    transcript = conversations._render_transcript(conv["messages"])
    result = await asyncio.to_thread(analytics.generate_analytics, transcript)
    conversations.apply_analytics(conv["externalId"], result)
    conv = db["conversations"].find_one({"_id": conv["_id"]})
    print(
        f"✓ analytics: status={conv.get('analyticsStatus')} "
        f"sentiment={conv.get('sentiment')} language={conv.get('language')}\n"
        f"  summary: {conv.get('summary')}\n"
    )

    # Cleanup.
    db["conversations"].delete_many({"phone": E164})
    db["transcripts"].delete_many({"phone": TEST_PHONE})
    db["pending_messages"].delete_many({"phone": TEST_PHONE})
    db["patients"].delete_many({"phone": TEST_PHONE})
    print("✓ cleaned up test data. Live smoke test passed.")


if __name__ == "__main__":
    asyncio.run(main())
