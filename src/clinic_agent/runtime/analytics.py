"""Conversation analytics — summary / sentiment / language.

Replicates nextdim's ``GeminiAnalyzer.generate_analytics`` pattern (one Gemini
structured-output call over the rendered transcript, temperature 0.2, thinking
minimal), but with OUR console data-contract schema: sentiment is
positive/neutral/negative and we DO emit a language (nextdim omits language).

Runs off the reply hot path — the idle sweeper calls this when a conversation
has ended. Synchronous (google-genai is sync); callers use ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel

from clinic_agent import observability
from clinic_agent.config import settings

log = logging.getLogger("clinic_agent.runtime.analytics")


def _minimal_thinking() -> types.ThinkingConfig:
    """Low-thinking config for the configured model family.

    Gemini 3.x uses ``thinking_level`` and REJECTS ``thinking_budget`` (400);
    2.x uses ``thinking_budget``. Keeping this correct avoids a hard failure
    when the model is switched.
    """
    m = re.search(r"gemini-(\d+)", settings.MODEL or "")
    if m and int(m.group(1)) >= 3:
        return types.ThinkingConfig(thinking_level="LOW")
    return types.ThinkingConfig(thinking_budget=0)


class ConversationAnalytics(BaseModel):
    """Console data-contract §3.3 nice-to-have fields."""

    summary: str
    sentiment: Literal["positive", "neutral", "negative"]
    language: Literal["en", "hi"]


_PROMPT = (
    "You are a clinic operations analyst. Read this WhatsApp conversation between "
    "a patient and the clinic's AI receptionist, then return JSON with:\n"
    "- summary: 1-2 sentences on what the patient wanted and the outcome.\n"
    "- sentiment: the patient's overall sentiment (positive, neutral, or negative).\n"
    "- language: the primary language (en, or hi for Hindi/Hinglish).\n\n"
    "Transcript:\n{transcript}"
)

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.GOOGLE_API_KEY)
    return _client


def generate_analytics(transcript: str) -> ConversationAnalytics | None:
    """One structured Gemini call → analytics, or None on empty input / failure."""
    if not transcript.strip():
        return None
    prompt = _PROMPT.format(transcript=transcript)
    with observability.generation(
        "generate-conversation-analytics",
        model=settings.MODEL,
        input=prompt,
        model_parameters={"temperature": 0.2},
    ) as gen:
        try:
            resp = _get_client().models.generate_content(
                model=settings.MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type="application/json",
                    response_schema=ConversationAnalytics,
                    thinking_config=_minimal_thinking(),
                ),
            )
            parsed = resp.parsed
            gen.update(
                output=resp.text,
                usage_details=observability.usage_details(resp.usage_metadata),
            )
            return parsed if isinstance(parsed, ConversationAnalytics) else None
        except Exception as exc:
            log.exception("analytics generation failed")
            gen.update(level="ERROR", status_message=repr(exc))
            return None
