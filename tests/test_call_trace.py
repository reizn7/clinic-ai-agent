"""Voice-call Langfuse generations (CallTracer), exported to memory not the network."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from clinic_agent import observability
from clinic_agent.voice.call_trace import GENERATION_NAME, CallTracer

MODEL = "gemini-test-live"


def _event(*, audio=False, text=None, calls=(), usage=None, turn_complete=False, interrupted=False):
    parts = [SimpleNamespace(inline_data=SimpleNamespace(data=b"\0\0"))] if audio else []
    return SimpleNamespace(
        content=SimpleNamespace(parts=parts),
        output_transcription=SimpleNamespace(text=text) if text else None,
        usage_metadata=usage,
        turn_complete=turn_complete,
        interrupted=interrupted,
        get_function_calls=lambda: list(calls),
    )


def _usage(inp, out):
    return SimpleNamespace(
        prompt_token_count=inp,
        candidates_token_count=out,
        thoughts_token_count=None,
        total_token_count=inp + out,
    )


@pytest.fixture
def exported(langfuse_spans):
    return lambda: [s for s in langfuse_spans() if s.name == GENERATION_NAME]


def _attr(span, suffix):
    for key, value in span.attributes.items():
        if key.endswith(suffix):
            return json.loads(value) if isinstance(value, str) and value[:1] in "[{" else value
    return None


def test_one_generation_per_model_response_with_io_and_usage(exported):
    t = CallTracer(MODEL)
    # Greeting: audio, then its transcript lands AFTER turn_complete.
    t.observe(_event(audio=True))
    t.observe(_event(usage=_usage(100, 10), turn_complete=True))
    t.agent_said("Hello, how can I help?")
    # Caller asks; the model answers with a tool call (its own response) ...
    t.caller_said("Which doctors are there?")
    call = SimpleNamespace(id="c1", name="list_doctors", args={})
    t.observe(_event(calls=[call], usage=_usage(200, 5), turn_complete=True))
    # ... then speaks the result.
    t.observe(_event(audio=True))
    t.observe(_event(usage=_usage(250, 30), turn_complete=True))
    t.agent_said("We have Dr Asha Mehta.")
    t.close("stop")

    greeting, tool_turn, answer = exported()
    assert _attr(greeting, "observation.output") == {
        "role": "assistant",
        "content": "Hello, how can I help?",
    }
    assert _attr(greeting, "observation.model.name") == MODEL
    assert _attr(tool_turn, "observation.input") == [
        {"role": "user", "content": "Which doctors are there?"}
    ]
    out = _attr(tool_turn, "observation.output")
    assert out["tool_calls"][0]["function"] == {"name": "list_doctors", "arguments": "{}"}
    assert _attr(answer, "observation.output")["content"] == "We have Dr Asha Mehta."
    assert _attr(answer, "observation.usage_details") == {
        "input": 250,
        "output": 30,
        "total": 280,
    }


def test_barge_in_and_hangup_mid_reply_are_flagged(exported):
    t = CallTracer(MODEL)
    t.observe(_event(audio=True))
    t.observe(_event(interrupted=True))
    t.caller_said("wait, actually")
    t.observe(_event(audio=True))  # next reply starts, never completes
    t.close("disconnected")

    barged, cut_off = exported()
    assert "barge-in" in barged.attributes.get("langfuse.observation.status_message", "")
    assert "call ended mid-reply" in cut_off.attributes.get(
        "langfuse.observation.status_message", ""
    )


def test_noop_when_tracing_off(monkeypatch):
    monkeypatch.setattr(observability, "_client", None)
    t = CallTracer(MODEL)
    t.observe(_event(audio=True, turn_complete=True))
    t.agent_said("hi")
    t.close()  # nothing to assert beyond "doesn't raise"
