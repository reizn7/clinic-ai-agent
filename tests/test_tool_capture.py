from __future__ import annotations

from google.adk.events import Event
from google.genai import types

from clinic_agent.runtime.tool_capture import ToolCallRecorder, looks_like_error


def _call(id_: str, name: str, args: dict) -> Event:
    return Event(
        author="clinic_receptionist",
        content=types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(id=id_, name=name, args=args))],
        ),
    )


def _resp(id_: str, name: str, response: dict) -> Event:
    return Event(
        author="clinic_receptionist",
        content=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(id=id_, name=name, response=response)
                )
            ],
        ),
    )


def test_looks_like_error() -> None:
    assert looks_like_error({"error": "x"})
    assert looks_like_error({"success": False})
    assert not looks_like_error({"success": True})
    assert not looks_like_error(None)


def test_pairs_calls_with_responses_by_id() -> None:
    rec = ToolCallRecorder()
    assert rec.observe(_call("a", "list_doctors", {})) == []
    assert rec.has_pending
    rec.observe(_call("b", "check_availability", {"doctor_name": "Ghost", "date": "2026-11-20"}))
    # responses may come back out of order
    done = rec.observe(_resp("b", "check_availability", {"error": "No doctor"}))
    assert done == rec.calls[-1:]
    rec.observe(_resp("a", "list_doctors", {"doctors": []}))
    assert not rec.has_pending

    b, a = rec.calls
    assert b["name"] == "check_availability"
    assert b["args"] == {"doctor_name": "Ghost", "date": "2026-11-20"}
    assert b["status"] == "error"
    assert isinstance(b["duration_ms"], int)
    assert a["name"] == "list_doctors"
    assert a["status"] == "success"
    assert a["result"] == {"doctors": []}


def test_orphan_response_has_no_args_or_duration() -> None:
    rec = ToolCallRecorder()
    rec.observe(_resp("zzz", "get_clinic_info", {"clinicName": "ABC"}))
    (c,) = rec.calls
    assert c["args"] == {}
    assert c["duration_ms"] is None
    assert c["status"] == "success"


def test_events_without_tools_are_ignored() -> None:
    rec = ToolCallRecorder()
    ev = Event(
        author="clinic_receptionist",
        content=types.Content(role="model", parts=[types.Part(text="hello")]),
    )
    assert rec.observe(ev) == []
    assert rec.calls == []
