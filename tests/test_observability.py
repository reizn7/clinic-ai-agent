"""Langfuse tracing helpers: masking, ids, no-op when off, and real span export."""

from __future__ import annotations

import pytest
from opentelemetry import trace

from clinic_agent import observability
from clinic_agent.config import settings

PHONE = "919876543210"


def test_redact_phone_numbers_only():
    text = f"call +{PHONE} or {PHONE}; appt 6650f1c2a9b8 on 2026-01-16 at 09:30, age 34"
    out = observability.redact(text)
    assert PHONE not in out
    assert out.count(observability.REDACTED) == 2
    assert "6650f1c2a9b8" in out and "2026-01-16" in out and "09:30" in out


def test_redact_recurses_into_structures():
    data = {"args": {"phone": PHONE, "age": 34}, "list": [PHONE, "ok"]}
    assert observability.redact(data) == {
        "args": {"phone": observability.REDACTED, "age": 34},
        "list": [observability.REDACTED, "ok"],
    }


def test_trace_user_id_hashed_and_stable(monkeypatch):
    monkeypatch.setattr(settings, "LANGFUSE_MASK_PHONES", True)
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "sk-test")
    a = observability.trace_user_id(PHONE)
    assert a == observability.trace_user_id("+" + PHONE)  # same patient, either format
    assert a.startswith("p_") and PHONE not in a
    assert observability.trace_user_id("") is None


def test_trace_user_id_raw_when_unmasked(monkeypatch):
    monkeypatch.setattr(settings, "LANGFUSE_MASK_PHONES", False)
    assert observability.trace_user_id("+" + PHONE) == PHONE


def test_disabled_is_a_noop(monkeypatch):
    monkeypatch.setattr(observability, "_client", None)
    monkeypatch.setattr(settings, "LANGFUSE_ENABLED", False)
    observability.init_tracing()
    assert not observability.is_enabled()
    with observability.trace_root("t", phone=PHONE) as obs:
        obs.update(output="x")
    with observability.generation("g", model="m") as gen:
        gen.update(output="y")


def test_enabled_without_keys_stays_off(monkeypatch):
    monkeypatch.setattr(observability, "_client", None)
    monkeypatch.setattr(settings, "LANGFUSE_ENABLED", True)
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    observability.init_tracing()
    assert not observability.is_enabled()


def test_noop_trace_root_propagates_exceptions(monkeypatch):
    monkeypatch.setattr(observability, "_client", None)
    with pytest.raises(ValueError), observability.trace_root("t"):
        raise ValueError("boom")


def test_spans_exported_nested_masked_and_attributed(langfuse_spans, monkeypatch):
    """End to end against the real Langfuse SDK, exporting to memory not the network."""
    monkeypatch.setattr(settings, "LANGFUSE_MASK_PHONES", True)

    with observability.trace_root(
        "handle-whatsapp-message",
        phone=PHONE,
        channel="whatsapp",
        session_id="conv_test",
        input=f"hi from {PHONE}",
    ) as obs:
        # Stand-in for an instrumented ADK tool span, with the phone in a tool arg.
        adk = trace.get_tracer("openinference.instrumentation.google_adk")
        with adk.start_as_current_span("book_appointment") as span:
            span.set_attribute("openinference.span.kind", "TOOL")
            span.set_attribute("input.value", f'{{"phone": "{PHONE}"}}')
        obs.update(output="booked")

    spans = {s.name: s for s in langfuse_spans()}
    root, tool = spans["handle-whatsapp-message"], spans["book_appointment"]

    # The tool span nests under our root, in the same trace.
    assert tool.parent.span_id == root.context.span_id
    assert tool.context.trace_id == root.context.trace_id

    # No raw phone number anywhere in what was exported.
    for s in (root, tool):
        for value in s.attributes.values():
            assert PHONE not in str(value)
    assert observability.REDACTED in tool.attributes["input.value"]

    # user / session / tags propagate to child spans too.
    values = {str(v) for v in tool.attributes.values()}
    assert observability.trace_user_id(PHONE) in values
    assert "conv_test" in values
