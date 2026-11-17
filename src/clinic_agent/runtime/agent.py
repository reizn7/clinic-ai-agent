"""Builds the ADK receptionist agent for a channel.

Collapses nextdim's behaviors/plugins layers into one factory: model + prompt +
tools → one ``LlmAgent``. No handoff tree, no manifests. Both channels share the
same brain (``ALL_TOOLS`` + the core prompt rules); only the model and the
channel style differ:

* ``whatsapp`` — text model (``MODEL``) + ``SYSTEM_PROMPT``; run turn-by-turn.
* ``voice`` — Live native-audio model (``VOICE_MODEL``) + ``VOICE_SYSTEM_PROMPT``;
  run with ``Runner.run_live`` by the browser voice-call bridge (``voice.bridge``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.utils.instructions_utils import InstructionProvider

from clinic_agent.prompt import SYSTEM_PROMPT, VOICE_SYSTEM_PROMPT
from clinic_agent.runtime.model_factory import get_model, get_voice_model
from clinic_agent.tools import catalog
from clinic_agent.tools.booking_tools import ALL_TOOLS

log = logging.getLogger("clinic_agent.runtime.agent")

Channel = Literal["whatsapp", "voice"]


def with_clinic_catalogs(base: str) -> InstructionProvider:
    """The channel prompt plus this clinic's catalog section, resolved per run.

    ADK calls the provider for every turn (WhatsApp) / at connect (voice), so
    catalogs a clinic adds or edits in the console show up without a restart
    (definitions are cached ~60 s in ``catalog``). Any failure falls back to the
    plain prompt — a DB hiccup must never block a reply.
    """

    async def provide(_ctx: ReadonlyContext) -> str:
        try:
            section = await asyncio.to_thread(catalog.prompt_section)
        except Exception:
            log.exception("clinic catalog prompt section unavailable")
            section = ""
        return f"{base}\n\n{section}" if section else base

    return provide


def build_agent(channel: Channel = "whatsapp") -> LlmAgent:
    if channel == "voice":
        model, base = get_voice_model(), VOICE_SYSTEM_PROMPT
    else:
        model, base = get_model(), SYSTEM_PROMPT
    return LlmAgent(
        name="clinic_receptionist",
        model=model,
        instruction=with_clinic_catalogs(base),
        tools=list(ALL_TOOLS),
    )
