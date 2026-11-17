"""Langfuse tracing (OpenTelemetry-based). Entirely optional: a no-op unless enabled.

How the pieces fit:

* ``init_tracing()`` creates the Langfuse client (it registers itself as the
  global OpenTelemetry tracer provider) and turns on Langfuse's recommended ADK
  integration, OpenInference's ``GoogleADKInstrumentor``: every agent run, model
  call (a ``generation`` with model, tokens, thinking) and tool call (a ``tool``)
  becomes a span. The instrumentor switches ADK's own tracer off, so nothing is
  recorded twice.
* ``trace_root()`` opens the root observation for one unit of work — one trace
  per WhatsApp turn, per voice call, per analytics job — with the trace-level
  input/output, and propagates user / session / tags to every span inside it.
  The session is the conversation (``conversations.externalId``), so a chat
  replays as one Langfuse session and lines up with the staff console.
* ``generation()`` records the one Gemini call made outside ADK (analytics).

Observation names are verb-first and stable (Langfuse best practice: dashboards
and evaluators target them by name) — change them only deliberately.

Patient phone numbers appear in prompts and tool arguments. With
``LANGFUSE_MASK_PHONES`` (the default) they are redacted from every exported span
(``mask_otel_spans``, the hook Langfuse recommends), and the user id is a keyed
hash of the number instead.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import importlib.metadata
import logging
import os
import re
from collections.abc import Iterator
from typing import Any

from clinic_agent.config import settings

log = logging.getLogger("clinic_agent.observability")

SERVICE_NAME = "medi-desk-ai"

_client: Any = None  # langfuse.Langfuse once initialised

# A bare run of 10-15 digits (optionally +-prefixed) not glued to letters/digits:
# matches 919876543210 / +919876543210 but not ids, dates (2026-01-16) or times.
_PHONE_RE = re.compile(r"(?<![0-9A-Za-z])\+?\d{10,15}(?![0-9A-Za-z])")
REDACTED = "<phone>"


# ---- masking ----------------------------------------------------------------


def redact(value: Any) -> Any:
    """Return ``value`` with phone numbers replaced, recursing into dicts/lists."""
    if isinstance(value, str):
        return _PHONE_RE.sub(REDACTED, value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def _mask_otel_spans(*, params: Any) -> Any:
    """Langfuse ``mask_otel_spans`` hook: runs on every span at export time.

    Covers our own observations and the ADK instrumentor's spans alike. v5
    metadata attributes are JSON-encoded strings; replacing a digit run inside
    one keeps it valid JSON, so no decode/encode is needed.
    """
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches = {}
    for ident, span in params.spans.items():
        changed: dict[str, Any] = {}
        for key, val in span.attributes.items():
            if isinstance(val, str):
                new = _PHONE_RE.sub(REDACTED, val)
                if new != val:
                    changed[key] = new
            elif isinstance(val, (list, tuple)) and val and isinstance(val[0], str):
                new_seq = [_PHONE_RE.sub(REDACTED, v) for v in val]
                if new_seq != list(val):
                    changed[key] = new_seq
        if changed:
            patches[ident] = OtelSpanPatch(set_attributes=changed)
    return MaskOtelSpansResult(span_patches=patches)


def trace_user_id(phone: str | None) -> str | None:
    """Stable per-patient id for Langfuse's user/session fields.

    Masked: ``p_`` + HMAC-SHA256(phone) keyed by the Langfuse secret key, so the
    number can't be recovered by hashing every possible phone number.
    """
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit()) or phone
    if not settings.LANGFUSE_MASK_PHONES:
        return digits
    key = (settings.LANGFUSE_SECRET_KEY or settings.CLINIC_ID).encode()
    return "p_" + hmac.new(key, digits.encode(), hashlib.sha256).hexdigest()[:16]


# ---- lifecycle --------------------------------------------------------------


def is_enabled() -> bool:
    return _client is not None


def init_tracing() -> None:
    """Start exporting traces to Langfuse, if configured. Safe to call twice."""
    global _client
    if _client is not None or not settings.LANGFUSE_ENABLED:
        return
    if not (settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY):
        log.warning("LANGFUSE_ENABLED is set but the Langfuse keys are missing; tracing off")
        return
    try:
        from langfuse import Langfuse

        # Name the service on every span (otherwise "unknown_service:python3").
        os.environ.setdefault("OTEL_SERVICE_NAME", SERVICE_NAME)
        mask_on = settings.LANGFUSE_MASK_PHONES
        _client = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            base_url=settings.LANGFUSE_BASE_URL,
            environment=settings.LANGFUSE_ENVIRONMENT,
            # Lets Langfuse compare cost/latency/quality across deploys.
            release=_app_release(),
            mask_otel_spans=_mask_otel_spans if mask_on else None,
        )
        from openinference.instrumentation.google_adk import GoogleADKInstrumentor

        instrumentor = GoogleADKInstrumentor()
        if not instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.instrument()
        log.info(
            "Langfuse tracing on → %s (env %s)",
            settings.LANGFUSE_BASE_URL,
            settings.LANGFUSE_ENVIRONMENT,
        )
    except Exception:
        log.exception("Langfuse init failed; tracing off")
        _client = None


def _app_release() -> str | None:
    """The deployed version: ``LANGFUSE_RELEASE`` if set, else the package version."""
    if release := os.environ.get("LANGFUSE_RELEASE"):
        return release
    try:
        return importlib.metadata.version(SERVICE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


def adk_user_id(phone: str | None, fallback: str) -> str:
    """The ``user_id`` to hand ADK's runner.

    ADK only uses it to key its throwaway in-memory session, but the ADK
    instrumentor copies it onto every agent/LLM/tool span as the Langfuse user,
    overriding ``trace_root``'s. Giving ADK the same (hashed) id keeps the user
    consistent across the whole trace.
    """
    return trace_user_id(phone) or fallback


def shutdown_tracing() -> None:
    """Flush buffered spans and stop the exporter (app shutdown)."""
    global _client
    if _client is None:
        return
    try:
        _client.shutdown()
    except Exception:
        log.exception("Langfuse shutdown failed")
    _client = None


# ---- observations -----------------------------------------------------------


class _NoopObservation:
    """Stand-in when tracing is off, so call sites never branch."""

    def update(self, **_: Any) -> _NoopObservation:
        return self

    def end(self, **_: Any) -> _NoopObservation:
        return self

    def score_trace(self, **_: Any) -> None:
        return None


_NOOP = _NoopObservation()


@contextlib.contextmanager
def trace_root(
    name: str,
    *,
    phone: str | None = None,
    channel: str | None = None,
    session_id: str | None = None,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Open the root observation (= the trace) for one unit of work.

    ``input``/``output`` set here become the trace's input/output: keep them to
    what a reviewer needs (the patient's message, the reply); put the rest in
    ``metadata``. ``session_id`` should be the conversation id. Yields an object
    with ``.update(output=..., metadata=..., level=..., status_message=...)``.
    Tracing failures never break the wrapped code.
    """
    if _client is None:
        yield _NOOP
        return

    from langfuse import propagate_attributes

    user_id = trace_user_id(phone)
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(
            propagate_attributes(
                user_id=user_id,
                session_id=session_id,
                tags=[channel] if channel else None,
                trace_name=name,
            )
        )
        if session_id:
            # The ADK instrumentor stamps ADK's per-turn session uuid on its spans
            # unless an OpenInference session is already in context; set ours so
            # the whole trace lands in the conversation's Langfuse session.
            from openinference.instrumentation import using_session

            stack.enter_context(using_session(session_id))
        obs = stack.enter_context(
            _client.start_as_current_observation(
                name=name, as_type="span", input=input, metadata=metadata
            )
        )
    except Exception:
        log.exception("could not open trace %s", name)
        with contextlib.suppress(Exception):
            stack.close()
        yield _NOOP
        return

    with stack:
        yield from _run_observed(obs)


@contextlib.contextmanager
def generation(name: str, *, model: str, input: Any = None, **kwargs: Any) -> Iterator[Any]:
    """A model-call observation (for Gemini calls made outside ADK)."""
    if _client is None:
        yield _NOOP
        return
    stack = contextlib.ExitStack()
    try:
        obs = stack.enter_context(
            _client.start_as_current_observation(
                name=name, as_type="generation", model=model, input=input, **kwargs
            )
        )
    except Exception:
        log.exception("could not open generation %s", name)
        with contextlib.suppress(Exception):
            stack.close()
        yield _NOOP
        return

    with stack:
        yield from _run_observed(obs)


def _run_observed(obs: Any) -> Iterator[Any]:
    """Yield ``obs`` to the caller's block; mark it ERROR if the block raises."""
    try:
        yield obs
    except BaseException as exc:
        with contextlib.suppress(Exception):
            obs.update(level="ERROR", status_message=repr(exc))
        raise


def start_observation(name: str, *, as_type: str, **kwargs: Any) -> Any:
    """Open a child of the current observation WITHOUT making it current.

    For steps whose start and end happen in different callbacks (a voice turn,
    a live tool call); the caller must ``.end()`` it. A no-op when tracing is off.
    """
    if _client is None:
        return _NOOP
    try:
        return _client.start_observation(name=name, as_type=as_type, **kwargs)
    except Exception:
        log.exception("could not open observation %s", name)
        return _NOOP


# ---- scores -----------------------------------------------------------------
#
# Score names are stable (dashboards and filters key on them). Session scores use
# a deterministic id, so re-scoring a conversation (e.g. a voice call closed by
# the sweeper and then again on hangup) overwrites instead of duplicating.


def score_session(
    session_id: str | None,
    name: str,
    value: float | str,
    *,
    data_type: str,
    comment: str | None = None,
) -> None:
    """Attach a score to a Langfuse session (= one conversation). Never raises."""
    if _client is None or not session_id:
        return
    try:
        _client.create_score(
            name=name,
            value=value,
            session_id=session_id,
            score_id=f"{session_id}-{name}",
            data_type=data_type,
            comment=redact(comment) if comment else None,
        )
    except Exception:
        log.exception("could not score session %s (%s)", session_id, name)


def score_tool_errors(obs: Any, tool_calls: list[dict]) -> None:
    """``tool_error`` (bool) on the trace ``obs`` roots; skipped if no tool ran."""
    if not tool_calls:
        return
    failed = [c.get("name") for c in tool_calls if c.get("status") == "error"]
    try:
        obs.score_trace(
            name="tool_error",
            value=1 if failed else 0,
            data_type="BOOLEAN",
            comment=", ".join(str(n) for n in failed) or None,
        )
    except Exception:
        log.exception("could not score tool_error")


def usage_details(usage_metadata: Any) -> dict[str, int] | None:
    """Langfuse ``usage_details`` from a google-genai ``usage_metadata`` (or None)."""
    if usage_metadata is None:
        return None
    usage = {
        "input": getattr(usage_metadata, "prompt_token_count", None),
        "output": getattr(usage_metadata, "candidates_token_count", None),
        "output_reasoning": getattr(usage_metadata, "thoughts_token_count", None),
        "total": getattr(usage_metadata, "total_token_count", None),
    }
    return {k: v for k, v in usage.items() if isinstance(v, int)} or None
