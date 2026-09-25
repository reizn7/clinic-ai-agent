"""Run one agent turn: message in → reply out.

Distilled from nextdim's ``_default_run_turn_impl`` + ``contextualize``. The agent
is stateless per turn; prior turns come from Mongo (``history``) and are seeded
into the message, mirroring nextdim's text cold-start seeding.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Any

from google.adk.runners import InMemoryRunner
from google.genai import types

from clinic_agent.runtime import conversations
from clinic_agent.runtime.agent import build_agent
from clinic_agent.runtime.history import append_turn, get_history

log = logging.getLogger("clinic_agent.runtime.turn")

APP_NAME = "clinic-ai-agent"

FALLBACK_REPLY = (
    "Sorry — I hit a problem on my end and couldn't finish that. "
    "Please try again in a moment."
)

# The agent tree is static, so build the runner once and reuse it.
_runner = InMemoryRunner(agent=build_agent(), app_name=APP_NAME)


def contextualize(message: str, history: list[dict], phone: str) -> str:
    """Prepend caller identity + today's date + prior turns for a fresh session."""
    now = datetime.now()
    header = (
        "## Context\n"
        f"Today's date is {now:%A, %B %d, %Y} (use it to resolve relative dates "
        "like 'tomorrow' and to format dates as YYYY-MM-DD for tools).\n"
    )
    if phone:
        header += f"The patient is texting from WhatsApp number: {phone}\n"
    identity = header + "\n"

    if not history:
        return f"{identity}{message}"

    lines = []
    for turn in history:
        who = "Patient" if turn.get("role") == "user" else "Assistant"
        lines.append(f"{who}: {turn.get('content', '')}")
    prior = "\n".join(lines)
    return (
        f"{identity}"
        "## Prior conversation (for context; do not re-ask for facts already given)\n"
        f"{prior}\n\n---\nPatient message: {message}\n"
    )


def _final_text(event) -> str:
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) if content else None
    if not parts:
        return ""
    return "".join(getattr(p, "text", "") or "" for p in parts)


def _looks_like_error(result: Any) -> bool:
    """Our tools signal failure with ``error`` or a falsy ``success``."""
    if not isinstance(result, dict):
        return False
    return "error" in result or result.get("success") is False


async def run_turn(phone: str, message: str) -> str:
    """Produce the agent's reply to one inbound message from ``phone``."""
    history = await get_history(phone)
    seeded = contextualize(message, history, phone)

    # Fresh session per turn: history is carried in the prompt, not ADK state.
    session_id = uuid.uuid4().hex
    await _runner.session_service.create_session(
        app_name=APP_NAME, user_id=phone, session_id=session_id
    )

    new_message = types.Content(role="user", parts=[types.Part.from_text(text=seeded)])

    reply = ""
    tool_calls: list[dict] = []
    pending: dict[str, dict] = {}  # function_call.id → {name, args, t0}
    t0 = time.perf_counter()

    try:
        async for event in _runner.run_async(
            user_id=phone, session_id=session_id, new_message=new_message
        ):
            for call in event.get_function_calls() or []:
                pending[call.id] = {
                    "name": call.name,
                    "args": dict(call.args or {}),
                    "t0": time.perf_counter(),
                }
            for resp in event.get_function_responses() or []:
                started = pending.pop(resp.id, None)
                result = dict(resp.response or {})
                tool_calls.append(
                    {
                        "name": resp.name,
                        "args": (started or {}).get("args", {}),
                        "result": result,
                        "status": "error" if _looks_like_error(result) else "success",
                        "duration_ms": (
                            round((time.perf_counter() - started["t0"]) * 1000)
                            if started
                            else None
                        ),
                    }
                )
            text = _final_text(event)
            if text:
                reply = text  # keep the latest non-empty (the final answer)
    except Exception:
        log.exception("Turn failed for %s", phone)
        latency_ms = round((time.perf_counter() - t0) * 1000)
        # Record the failed turn in both stores so we're not blind to it.
        await append_turn(phone, message, FALLBACK_REPLY)
        await conversations.record_turn(phone, message, FALLBACK_REPLY, tool_calls, latency_ms)
        return FALLBACK_REPLY

    latency_ms = round((time.perf_counter() - t0) * 1000)
    reply = reply.strip() or FALLBACK_REPLY

    # transcripts: lean prompt-history; conversations: full console record.
    await append_turn(phone, message, reply)
    await conversations.record_turn(phone, message, reply, tool_calls, latency_ms)
    return reply
