"""Builds the single ADK receptionist agent (root + only agent).

Collapses nextdim's behaviors/plugins layers into one factory: model + prompt +
tools → one ``LlmAgent``. No handoff tree, no manifests.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent

from clinic_agent.prompt import SYSTEM_PROMPT
from clinic_agent.runtime.model_factory import get_model
from clinic_agent.tools.booking_tools import ALL_TOOLS


def build_agent() -> LlmAgent:
    return LlmAgent(
        name="clinic_receptionist",
        model=get_model(),
        instruction=SYSTEM_PROMPT,
        tools=list(ALL_TOOLS),
    )
