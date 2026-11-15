"""Capture ADK tool calls from an event stream (shared by text + voice harnesses).

ADK emits a ``function_call`` event when the model asks for a tool and a
``function_response`` event once the tool has run. ``ToolCallRecorder`` pairs
them by id into the record both stores use::

    {name, args, result, status: "success"|"error", duration_ms}

``status`` follows the tool error convention (``looks_like_error``), which the
``conversations`` outcome logic depends on.
"""

from __future__ import annotations

import time
from typing import Any


def looks_like_error(result: Any) -> bool:
    """Our tools signal failure with ``error`` or a falsy ``success``."""
    if not isinstance(result, dict):
        return False
    return "error" in result or result.get("success") is False


class ToolCallRecorder:
    """Pairs function calls with their responses across a stream of events."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._pending: dict[str | None, dict] = {}  # function_call.id → {name, args, t0}

    @property
    def has_pending(self) -> bool:
        """True while a tool has been called but its response not yet seen."""
        return bool(self._pending)

    def observe(self, event: Any) -> list[dict]:
        """Record any calls/responses in ``event``; return the calls completed by it."""
        for call in event.get_function_calls() or []:
            self._pending[call.id] = {
                "name": call.name,
                "args": dict(call.args or {}),
                "t0": time.perf_counter(),
            }
        completed: list[dict] = []
        for resp in event.get_function_responses() or []:
            started = self._pending.pop(resp.id, None)
            result = dict(resp.response or {})
            completed.append(
                {
                    "name": resp.name,
                    "args": (started or {}).get("args", {}),
                    "result": result,
                    "status": "error" if looks_like_error(result) else "success",
                    "duration_ms": (
                        round((time.perf_counter() - started["t0"]) * 1000) if started else None
                    ),
                }
            )
        self.calls.extend(completed)
        return completed
