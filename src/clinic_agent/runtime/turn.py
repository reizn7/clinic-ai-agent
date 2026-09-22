"""Run one agent turn: message in → reply out.

Distilled from nextdim's ``_default_run_turn_impl`` + ``contextualize``. The agent
is stateless per turn; prior turns come from Mongo (``history``) and are seeded
into the message, mirroring nextdim's text cold-start seeding.
"""

from __future__ import annotations

import logging
import uuid

from google.adk.runners import InMemoryRunner
from google.genai import types

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
    """Prepend caller identity + prior turns so a fresh session has context."""
    identity = (
        f"## Caller\nThe patient is texting from WhatsApp number: {phone}\n\n"
        if phone
        else ""
    )
    if not history:
        return f"{identity}{message}" if identity else message

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
    try:
        async for event in _runner.run_async(
            user_id=phone, session_id=session_id, new_message=new_message
        ):
            text = _final_text(event)
            if text:
                reply = text  # keep the latest non-empty (the final answer)
    except Exception:
        log.exception("Turn failed for %s", phone)
        return FALLBACK_REPLY

    reply = reply.strip() or FALLBACK_REPLY
    await append_turn(phone, message, reply)
    return reply
