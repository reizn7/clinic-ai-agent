"""Builds the ADK receptionist agent for a channel.

Collapses nextdim's behaviors/plugins layers into one factory: model + prompt +
tools → one ``LlmAgent``. No handoff tree, no manifests. Both channels share the
same brain (``ALL_TOOLS`` + the core prompt rules); only the model and the
channel style differ:

* ``whatsapp`` — text model (``MODEL``) + ``SYSTEM_PROMPT``; run turn-by-turn.
* ``voice`` — Live native-audio model (``VOICE_MODEL``) + ``VOICE_SYSTEM_PROMPT``;
  run with ``Runner.run_live`` by the Exotel bridge.
"""

from __future__ import annotations

from typing import Literal

from google.adk.agents import LlmAgent

from clinic_agent.prompt import SYSTEM_PROMPT, VOICE_SYSTEM_PROMPT
from clinic_agent.runtime.model_factory import get_model, get_voice_model
from clinic_agent.tools.booking_tools import ALL_TOOLS

Channel = Literal["whatsapp", "voice"]


def build_agent(channel: Channel = "whatsapp") -> LlmAgent:
    if channel == "voice":
        model, instruction = get_voice_model(), VOICE_SYSTEM_PROMPT
    else:
        model, instruction = get_model(), SYSTEM_PROMPT
    return LlmAgent(
        name="clinic_receptionist",
        model=model,
        instruction=instruction,
        tools=list(ALL_TOOLS),
    )
