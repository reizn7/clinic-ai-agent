"""LLM configuration — Gemini via the Google AI Studio API key (no Vertex/GCP).

Distilled from nextdim's ``model_factory``: we keep only the API-key Gemini path
and drop the Vertex/Claude fallback chain, priority routing, and thinking knobs.
"""

from __future__ import annotations

import os

from google.adk.models import BaseLlm, FallbackModel, Gemini
from google.adk.models.lite_llm import LiteLlm
from google.genai import types

from clinic_agent.config import settings


def configure_genai_env() -> None:
    """Ensure google-genai uses the Gemini Developer API (API key), not Vertex.

    ADK's Gemini model reads these from the environment at call time.
    """
    os.environ.setdefault(
        "GOOGLE_GENAI_USE_VERTEXAI",
        "true" if settings.GOOGLE_GENAI_USE_VERTEXAI else "false",
    )
    if settings.GOOGLE_API_KEY:
        os.environ.setdefault("GOOGLE_API_KEY", settings.GOOGLE_API_KEY)
    if settings.GROQ_API_KEY:  # LiteLLM reads it from the environment
        os.environ.setdefault("GROQ_API_KEY", settings.GROQ_API_KEY)


def _retry_options() -> types.HttpRetryOptions:
    # Without this google-genai makes exactly one attempt, so a single transient
    # 503 ("model is experiencing high demand") fails the whole turn.
    return types.HttpRetryOptions(
        attempts=max(1, settings.MODEL_RETRY_ATTEMPTS),
        initial_delay=1.0,
        max_delay=8.0,
    )


def get_model() -> BaseLlm:
    """The text model: ``MODEL`` with retries, then each ``MODEL_FALLBACKS`` entry
    (Gemini, or Groq via LiteLLM for ``groq:<model>``).

    Each model retries 408/429/5xx with backoff; if it still fails with a
    retriable status, ``FallbackModel`` moves on to the next one.
    """
    configure_genai_env()
    names = [settings.MODEL] + [
        m.strip()
        for m in settings.MODEL_FALLBACKS.split(",")
        if m.strip() and m.strip() != settings.MODEL
    ]
    models = [m for m in map(_build, names) if m is not None]
    return models[0] if len(models) == 1 else FallbackModel(models=models)


def _build(name: str) -> BaseLlm | None:
    if name.startswith("groq:"):
        if not settings.GROQ_API_KEY:
            return None  # no key → leave Groq out of the chain
        # LiteLLM retries 429/5xx itself; FallbackModel reads its status codes.
        return LiteLlm(
            model=f"groq/{name.removeprefix('groq:')}",
            num_retries=max(0, settings.MODEL_RETRY_ATTEMPTS - 1),
        )
    return Gemini(model=name, retry_options=_retry_options())


def get_voice_model() -> str:
    """Return the Live API native-audio model for the voice channel."""
    configure_genai_env()
    return settings.VOICE_MODEL
