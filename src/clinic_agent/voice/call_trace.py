"""Langfuse observations for a live voice call (inside its ``handle-voice-call`` trace).

The ADK instrumentor wraps ``Runner.run_async`` (WhatsApp) but not ``run_live``:
on a call it still emits each **tool** span (ADK's tool-tracing hook runs in live
mode too) but no model spans. So the bridge adds the model side itself, shaped
like the WhatsApp traces:

* one ``generate-voice-reply`` **generation** per model response — model, what
  the caller said (input, as a chat message), what the agent said plus any tool
  calls it made (output, as an assistant message with ``tool_calls``), and
  Gemini Live's token usage. Live ends a response at ``turn_complete`` (a tool
  call is its own response), so generations interleave with the tool spans as
  siblings. A generation opens on its first model output and ends at
  ``turn_complete`` or barge-in; transcripts can arrive just after
  ``turn_complete``, so it is finalised lazily but stamped with the real end time.

Never raises into the bridge: tracing problems are logged and dropped.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from clinic_agent import observability

log = logging.getLogger("clinic_agent.voice.call_trace")

GENERATION_NAME = "generate-voice-reply"


class CallTracer:
    def __init__(self, model: str) -> None:
        self.model = model
        self._gen: Any = None  # the open generation, if any
        self._gen_end_ns: int | None = None  # set at turn_complete / barge-in
        self._gen_interrupted_reason = ""
        self._caller_text: list[str] = []  # caller speech awaiting the next reply
        self._gen_input: list[str] = []
        self._gen_output: list[str] = []
        self._gen_tool_calls: list[dict] = []
        self._gen_usage: dict[str, int] | None = None

    # ---- inputs from the bridge -------------------------------------------

    def caller_said(self, text: str) -> None:
        """A finished caller transcription."""
        if self._gen is not None and self._gen_end_ns is not None:
            self._finish_generation()  # the previous reply is over
        self._caller_text.append(text)

    def agent_said(self, text: str) -> None:
        """A finished agent transcription (may land just after turn_complete)."""
        if self._gen is None:
            self._open_generation()
        self._gen_output.append(text)

    def observe(self, event: Any) -> None:
        """Feed every live event (before transcriptions are applied)."""
        try:
            self._observe(event)
        except Exception:
            log.exception("voice tracing failed on an event")

    def close(self, reason: str = "") -> None:
        """Call over: end anything still open."""
        try:
            if self._gen is not None:
                if self._gen_end_ns is None:
                    self._gen_interrupted_reason = f"call ended mid-reply ({reason or 'closed'})"
                self._finish_generation()
        except Exception:
            log.exception("voice tracing failed at call end")

    # ---- internals ---------------------------------------------------------

    def _observe(self, event: Any) -> None:
        calls = event.get_function_calls() or []
        content = getattr(event, "content", None)
        has_model_audio = any(
            getattr(part, "inline_data", None) for part in getattr(content, "parts", None) or []
        )
        out_tr = getattr(event, "output_transcription", None)
        model_output = bool(calls) or has_model_audio or bool(out_tr and out_tr.text)

        if model_output:
            if self._gen is not None and self._gen_end_ns is not None:
                self._finish_generation()  # a new reply started
            if self._gen is None:
                self._open_generation()

        usage = observability.usage_details(getattr(event, "usage_metadata", None))
        if usage and self._gen is not None:
            self._gen_usage = usage  # Live reports running totals for the turn

        for call in calls:
            args = dict(call.args or {})
            # OpenAI-style tool call (arguments JSON-encoded) so Langfuse renders it
            # as a card; the id matches the instrumentor's tool span output.
            self._gen_tool_calls.append(
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(args, default=str)},
                }
            )

        if self._gen is not None and self._gen_end_ns is None:
            if getattr(event, "interrupted", None):
                self._gen_interrupted_reason = "caller interrupted (barge-in)"
                self._gen_end_ns = time.time_ns()
            elif getattr(event, "turn_complete", None):
                self._gen_end_ns = time.time_ns()

    def _open_generation(self) -> None:
        self._gen_input, self._caller_text = self._caller_text, []
        self._gen_output, self._gen_tool_calls = [], []
        self._gen_usage, self._gen_end_ns, self._gen_interrupted_reason = None, None, ""
        self._gen = observability.start_observation(
            GENERATION_NAME, as_type="generation", model=self.model
        )

    def _finish_generation(self) -> None:
        gen, self._gen = self._gen, None
        if gen is None:
            return
        message: dict[str, Any] = {"role": "assistant", "content": " ".join(self._gen_output)}
        if self._gen_tool_calls:
            message["tool_calls"] = self._gen_tool_calls
        update: dict[str, Any] = {
            # Chat-message shape so Langfuse renders a readable exchange.
            "input": [{"role": "user", "content": t} for t in self._gen_input] or None,
            "output": message,
            "usage_details": self._gen_usage,
        }
        if self._gen_interrupted_reason:
            update |= {"level": "WARNING", "status_message": self._gen_interrupted_reason}
        gen.update(**update)
        gen.end(end_time=self._gen_end_ns)
