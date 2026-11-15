"""Exotel Voicebot (AgentStream) WebSocket message parsing and builders.

Confirmed against developer.exotel.com/docs/agentstream/websocket-protocol and
stream-voicebot-applet (2026-11):

Inbound (Exotel → us), one JSON object per text frame, keyed by ``event``:
  connected  {"event": "connected"}
  start      {"event": "start", "sequence_number": "1", "stream_sid": "MZ…",
              "start": {"stream_sid", "call_sid", "account_sid", "from", "to",
                        "custom_parameters": {...},
                        "media_format": {"encoding", "sample_rate", "bit_rate"}}}
  media      {"event": "media", "sequence_number", "stream_sid",
              "media": {"chunk", "timestamp", "payload": <base64 PCM>}}
  dtmf       {"dtmf": {"digit", "duration"}}
  mark       {"mark": {"name"}}            (our earlier mark finished playing)
  stop       {"stop": {"call_sid", "account_sid", "reason": "stopped"|"callended"}}

Outbound (us → Exotel): ``media`` {"event","stream_sid","media":{"payload"}},
``mark`` {"event","stream_sid","mark":{"name"}}, ``clear`` {"event","stream_sid"}.

Exotel's docs mix string and number types (``sequence_number``/``chunk``), and
their reference repo shows camelCase (``streamSid``/``callSid``); parsing here
tolerates both.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CallInfo:
    stream_sid: str
    call_sid: str
    caller: str  # raw ``start.from`` (normalise with phone.normalize_phone)
    callee: str = ""
    account_sid: str = ""
    sample_rate: int | None = None  # media_format.sample_rate, if Exotel sent one
    custom_parameters: dict[str, Any] = field(default_factory=dict)


def _pick(d: dict, *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def parse_message(text: str) -> dict[str, Any] | None:
    """Decode one frame; ``None`` if it isn't a JSON object with an ``event``."""
    try:
        msg = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(msg, dict) or not isinstance(msg.get("event"), str):
        return None
    return msg


def parse_start(msg: dict[str, Any]) -> CallInfo:
    start = msg.get("start") or {}
    fmt = start.get("media_format") or start.get("mediaFormat") or {}
    rate = _pick(fmt, "sample_rate", "sampleRate")
    try:
        sample_rate = int(rate) if rate is not None else None
    except (TypeError, ValueError):
        sample_rate = None
    return CallInfo(
        stream_sid=str(
            _pick(start, "stream_sid", "streamSid") or _pick(msg, "stream_sid", "streamSid") or ""
        ),
        call_sid=str(_pick(start, "call_sid", "callSid") or ""),
        caller=str(_pick(start, "from", "From", "caller") or ""),
        callee=str(_pick(start, "to", "To") or ""),
        account_sid=str(_pick(start, "account_sid", "accountSid") or ""),
        sample_rate=sample_rate,
        custom_parameters=dict(
            start.get("custom_parameters") or start.get("customParameters") or {}
        ),
    )


def media_payload(msg: dict[str, Any]) -> str:
    return str((msg.get("media") or {}).get("payload") or "")


def media_event(stream_sid: str, payload_b64: str) -> str:
    return json.dumps({"event": "media", "stream_sid": stream_sid, "media": {"payload": payload_b64}})


def clear_event(stream_sid: str) -> str:
    return json.dumps({"event": "clear", "stream_sid": stream_sid})


def mark_event(stream_sid: str, name: str) -> str:
    return json.dumps({"event": "mark", "stream_sid": stream_sid, "mark": {"name": name}})
