from __future__ import annotations

import json

from clinic_agent.voice import protocol

# Shape from developer.exotel.com/docs/agentstream/websocket-protocol.
START = {
    "event": "start",
    "sequence_number": "1",
    "stream_sid": "MZ123",
    "start": {
        "stream_sid": "MZ123",
        "call_sid": "CA456",
        "account_sid": "AC789",
        "from": "+919876543210",
        "to": "+918047491899",
        "custom_parameters": {"key1": "value1"},
        "media_format": {"encoding": "audio/x-raw", "sample_rate": "16000", "bit_rate": "16"},
    },
}


def test_parse_message() -> None:
    assert protocol.parse_message('{"event": "connected"}') == {"event": "connected"}
    assert protocol.parse_message("not json") is None
    assert protocol.parse_message("[1, 2]") is None
    assert protocol.parse_message('{"no": "event"}') is None


def test_parse_start_snake_case() -> None:
    info = protocol.parse_start(START)
    assert info.stream_sid == "MZ123"
    assert info.call_sid == "CA456"
    assert info.account_sid == "AC789"
    assert info.caller == "+919876543210"
    assert info.callee == "+918047491899"
    assert info.sample_rate == 16000
    assert info.custom_parameters == {"key1": "value1"}


def test_parse_start_tolerates_camel_case_and_missing() -> None:
    msg = {
        "event": "start",
        "streamSid": "MZ1",
        "start": {"callSid": "CA1", "from": "09876543210", "mediaFormat": {"sampleRate": 8000}},
    }
    info = protocol.parse_start(msg)
    assert info.stream_sid == "MZ1"  # falls back to the top-level sid
    assert info.call_sid == "CA1"
    assert info.caller == "09876543210"
    assert info.sample_rate == 8000

    empty = protocol.parse_start({"event": "start"})
    assert empty.stream_sid == "" and empty.caller == "" and empty.sample_rate is None


def test_media_payload() -> None:
    msg = {"event": "media", "media": {"chunk": 2, "timestamp": "200", "payload": "AAEC"}}
    assert protocol.media_payload(msg) == "AAEC"
    assert protocol.media_payload({"event": "media"}) == ""


def test_outbound_builders() -> None:
    assert json.loads(protocol.media_event("MZ1", "AAAA")) == {
        "event": "media",
        "stream_sid": "MZ1",
        "media": {"payload": "AAAA"},
    }
    assert json.loads(protocol.clear_event("MZ1")) == {"event": "clear", "stream_sid": "MZ1"}
    assert json.loads(protocol.mark_event("MZ1", "end")) == {
        "event": "mark",
        "stream_sid": "MZ1",
        "mark": {"name": "end"},
    }
