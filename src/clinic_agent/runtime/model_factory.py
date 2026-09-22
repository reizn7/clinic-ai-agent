"""LLM configuration — Gemini via the Google AI Studio API key (no Vertex/GCP).

Distilled from nextdim's ``model_factory``: we keep only the API-key Gemini path
and drop the Vertex/Claude fallback chain, priority routing, and thinking knobs.
"""

from __future__ import annotations

import os

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


def get_model() -> str:
    """Return the ADK model identifier to use (e.g. ``gemini-2.5-flash``)."""
    configure_genai_env()
    return settings.MODEL
