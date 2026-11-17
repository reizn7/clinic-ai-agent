from __future__ import annotations

import json

import pytest

from clinic_agent.voice import protocol


def test_parse_message() -> None:
    assert protocol.parse_message('{"event": "stop"}') == {"event": "stop"}
    assert protocol.parse_message("not json") is None
    assert protocol.parse_message("[1, 2]") is None
    assert protocol.parse_message('{"no": "event"}') is None
    assert protocol.parse_message('{"event": 3}') is None


def test_parse_start() -> None:
    start = protocol.parse_start(
        {"event": "start", "call_id": "c0ffee-42_x", "caller": " +91 98765 43210 ", "transcripts": True}
    )
    assert start == protocol.CallStart(
        call_id="c0ffee-42_x", caller="+91 98765 43210", transcripts=True
    )


def test_parse_start_missing_fields() -> None:
    start = protocol.parse_start({"event": "start"})
    assert start.caller == ""
    assert start.transcripts is False
    assert len(start.call_id) == 32  # generated server-side


@pytest.mark.parametrize("bad_id", [None, "", 42, "has space", "x" * 65, "../etc", {"a": 1}])
def test_parse_start_replaces_invalid_call_id(bad_id) -> None:
    start = protocol.parse_start({"event": "start", "call_id": bad_id, "caller": "9876543210"})
    assert start.call_id != bad_id
    assert len(start.call_id) == 32


@pytest.mark.parametrize("bad_caller", [None, 9876543210, ["9876543210"]])
def test_parse_start_non_string_caller(bad_caller) -> None:
    assert protocol.parse_start({"event": "start", "caller": bad_caller}).caller == ""


@pytest.mark.parametrize("flag", ["true", 1, None, "yes"])
def test_parse_start_transcripts_must_be_true(flag) -> None:
    assert protocol.parse_start({"event": "start", "transcripts": flag}).transcripts is False


def test_media_payload() -> None:
    assert protocol.media_payload({"event": "media", "payload": "AAEC"}) == "AAEC"
    assert protocol.media_payload({"event": "media"}) == ""
    assert protocol.media_payload({"event": "media", "payload": 12}) == ""


def test_outbound_builders() -> None:
    assert json.loads(protocol.ready_event(16000, 24000)) == {
        "event": "ready",
        "input_rate": 16000,
        "output_rate": 24000,
    }
    assert json.loads(protocol.media_event("AAAA")) == {"event": "media", "payload": "AAAA"}
    assert json.loads(protocol.clear_event()) == {"event": "clear"}
    assert json.loads(protocol.transcript_event("user", "Hi there", False)) == {
        "event": "transcript",
        "role": "user",
        "text": "Hi there",
        "final": False,
    }
    assert json.loads(protocol.transcript_event("assistant", "Hello", True))["final"] is True
    assert json.loads(protocol.end_event(protocol.END_INVALID_CALLER)) == {
        "event": "end",
        "reason": "invalid_caller",
    }
