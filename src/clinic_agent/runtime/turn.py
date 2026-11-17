"""Run one agent turn: message in → reply out.

Distilled from nextdim's ``_default_run_turn_impl`` + ``contextualize``. The agent
is stateless per turn; prior turns come from Mongo (``history``) and are seeded
into the message, mirroring nextdim's text cold-start seeding.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime
from typing import Literal

from google.adk.runners import InMemoryRunner
from google.genai import types

from clinic_agent import observability
from clinic_agent.runtime import conversations
from clinic_agent.runtime.agent import build_agent
from clinic_agent.runtime.history import append_turn, get_history
from clinic_agent.runtime.tool_capture import ToolCallRecorder
from clinic_agent.runtime.tool_capture import (  # noqa: F401  (re-exported; tests import it)
    looks_like_error as _looks_like_error,
)
from clinic_agent.tools.booking_tools import CALLER_PHONE, CHANNEL

log = logging.getLogger("clinic_agent.runtime.turn")

APP_NAME = "medi-desk-ai"

FALLBACK_REPLY = (
    "Sorry — I hit a problem on my end and couldn't finish that. "
    "Please try again in a moment."
)

# The agent tree is static, so build the runner once and reuse it.
_runner = InMemoryRunner(agent=build_agent(), app_name=APP_NAME)


def contextualize(
    message: str,
    history: list[dict],
    phone: str,
    channel: Literal["whatsapp", "voice"] = "whatsapp",
) -> str:
    """Prepend caller identity + today's date + prior turns for a fresh session.

    ``channel="voice"`` only changes the identity line and the trailing label
    (the message is the bridge's cue, not something the patient said); the
    WhatsApp output is unchanged.
    """
    now = datetime.now()
    header = (
        "## Context\n"
        f"Today's date is {now:%A, %B %d, %Y} (use it to resolve relative dates "
        "like 'tomorrow' and to format dates as YYYY-MM-DD for tools).\n"
    )
    if phone and channel == "voice":
        header += f"The patient is phoning the clinic from number: {phone}\n"
    elif phone:
        header += f"The patient is texting from WhatsApp number: {phone}\n"
    identity = header + "\n"

    if not history:
        return f"{identity}{message}"

    lines = []
    for turn in history:
        if turn.get("role") == "user":
            lines.append(f"Patient: {turn.get('content', '')}")
            continue
        line = f"Assistant: {turn.get('content', '')}"
        tools = turn.get("tools")
        if tools:
            # Surface prior tool RESULTS so the model can recall them (e.g. an
            # appointment id or slots it looked up earlier).
            summary = "; ".join(
                f"{t.get('name')} → {json.dumps(t.get('result'), default=str)}" for t in tools
            )
            line += f"\n  (tool results: {summary})"
        lines.append(line)
    prior = "\n".join(lines)
    trailer = message if channel == "voice" else f"Patient message: {message}"
    return (
        f"{identity}"
        "## Prior conversation (for context; do not re-ask for facts already given)\n"
        f"{prior}\n\n---\n{trailer}\n"
    )


def _final_text(event) -> str:
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) if content else None
    if not parts:
        return ""
    return "".join(getattr(p, "text", "") or "" for p in parts)


async def run_turn(phone: str, message: str, *, bubbles: int = 1) -> str:
    """Produce the agent's reply to one inbound message from ``phone``.

    Traced as one Langfuse trace (``handle-whatsapp-message``) inside the
    conversation's session; ADK's agent/LLM/tool spans nest under it. ``bubbles``
    = how many WhatsApp messages the debounce merged into this turn.
    """
    try:
        conversation_id: str | None = await conversations.resolve_conversation_id(phone)
    except Exception:
        log.exception("Could not resolve conversation for %s", phone)
        conversation_id = None

    with observability.trace_root(
        "handle-whatsapp-message",
        phone=phone,
        channel="whatsapp",
        session_id=conversation_id,
        input=message,
        metadata={"bubbles": bubbles, "conversation_id": conversation_id},
    ) as obs:
        reply, tool_calls, failed = await _run_turn(phone, message, conversation_id)
        update: dict = {
            "output": reply,
            "metadata": {
                "tools": [c.get("name") for c in tool_calls],
                "tool_errors": sum(c.get("status") == "error" for c in tool_calls),
            },
        }
        if failed:
            update |= {"level": "ERROR", "status_message": "turn failed; fallback reply sent"}
        obs.update(**update)
        observability.score_tool_errors(obs, tool_calls)
        return reply


async def _run_turn(
    phone: str, message: str, conversation_id: str | None = None
) -> tuple[str, list[dict], bool]:
    """The turn itself → ``(reply, tool_calls, failed)``."""
    history = await get_history(phone)
    seeded = contextualize(message, history, phone)

    # Fresh session per turn: history is carried in the prompt, not ADK state.
    session_id = uuid.uuid4().hex
    adk_user = observability.adk_user_id(phone, fallback=session_id)
    # The caller's number lives in session state, where the tools read it; the
    # model never supplies it (see tools.booking_tools).
    await _runner.session_service.create_session(
        app_name=APP_NAME,
        user_id=adk_user,
        session_id=session_id,
        state={CALLER_PHONE: phone, CHANNEL: "whatsapp"},
    )

    new_message = types.Content(role="user", parts=[types.Part.from_text(text=seeded)])

    reply = ""
    recorder = ToolCallRecorder()
    tool_calls = recorder.calls
    t0 = time.perf_counter()

    try:
        async for event in _runner.run_async(
            user_id=adk_user, session_id=session_id, new_message=new_message
        ):
            recorder.observe(event)
            text = _final_text(event)
            if text:
                reply = text  # keep the latest non-empty (the final answer)
    except Exception:
        log.exception("Turn failed for %s", phone)
        latency_ms = round((time.perf_counter() - t0) * 1000)
        # Record the failed turn in both stores so we're not blind to it.
        await append_turn(phone, message, FALLBACK_REPLY, tool_calls)
        await conversations.record_turn(
            phone, message, FALLBACK_REPLY, tool_calls, latency_ms, conversation_id
        )
        return FALLBACK_REPLY, tool_calls, True

    latency_ms = round((time.perf_counter() - t0) * 1000)
    reply = reply.strip() or FALLBACK_REPLY

    # transcripts: lean prompt-history (+ tool results for recall);
    # conversations: full console record.
    await append_turn(phone, message, reply, tool_calls)
    await conversations.record_turn(
        phone, message, reply, tool_calls, latency_ms, conversation_id
    )
    return reply, tool_calls, False
