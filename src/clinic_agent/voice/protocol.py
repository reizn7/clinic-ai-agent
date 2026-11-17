"""Wire protocol between the browser call page and ``WS /voice/ws``.

One JSON object per text frame, keyed by ``event``. Audio is base64 raw PCM:
16-bit signed little-endian, mono.

Client → server:
  start  {"event": "start", "call_id": "<uuid>", "caller": "<phone as typed>",
          "transcripts": true}
  media  {"event": "media", "payload": "<base64 PCM16 @ 16 kHz>"}   (~100 ms)
  stop   {"event": "stop"}

Server → client:
  ready       {"event": "ready", "input_rate": 16000, "output_rate": 24000}
              (the live session is up; start sending mic audio)
  media       {"event": "media", "payload": "<base64 PCM16 @ output_rate>"}
  clear       {"event": "clear"}  (barge-in: drop all queued playback now)
  transcript  {"event": "transcript", "role": "user"|"assistant", "text": "...",
               "final": bool}  (only when ``start.transcripts`` was true; a
               partial carries the whole line so far, so the client replaces it)
  end         {"event": "end", "reason": "..."}  (the server is ending the call)
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

# ``end`` reasons the server sends.
END_INVALID_CALLER = "invalid_caller"
END_NO_START = "no_start"
END_SESSION_ENDED = "session_ended"
END_ERROR = "error"

_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class CallStart:
    call_id: str
    caller: str  # as typed by the patient; validate with phone.is_valid_phone
    transcripts: bool = False


def parse_message(text: str) -> dict[str, Any] | None:
    """Decode one frame; ``None`` if it isn't a JSON object with a string ``event``."""
    try:
        msg = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(msg, dict) or not isinstance(msg.get("event"), str):
        return None
    return msg


def parse_start(msg: dict[str, Any]) -> CallStart:
    """Read a ``start`` frame, tolerating missing or mistyped fields.

    A missing or malformed ``call_id`` (anything but 1-64 of ``[A-Za-z0-9_-]``) is
    replaced by a server-generated one, since it is stored on the conversation.
    A non-string ``caller`` becomes ``""`` (the bridge then rejects the call), and
    ``transcripts`` is on only if it is literally ``true``.
    """
    call_id = msg.get("call_id")
    if not isinstance(call_id, str) or not _CALL_ID_RE.match(call_id):
        call_id = uuid.uuid4().hex
    caller = msg.get("caller")
    return CallStart(
        call_id=call_id,
        caller=caller.strip() if isinstance(caller, str) else "",
        transcripts=msg.get("transcripts") is True,
    )


def media_payload(msg: dict[str, Any]) -> str:
    payload = msg.get("payload")
    return payload if isinstance(payload, str) else ""


def ready_event(input_rate: int, output_rate: int) -> str:
    return json.dumps({"event": "ready", "input_rate": input_rate, "output_rate": output_rate})


def media_event(payload_b64: str) -> str:
    return json.dumps({"event": "media", "payload": payload_b64})


def clear_event() -> str:
    return json.dumps({"event": "clear"})


def transcript_event(role: Literal["user", "assistant"], text: str, final: bool) -> str:
    return json.dumps({"event": "transcript", "role": role, "text": text, "final": final})


def end_event(reason: str) -> str:
    return json.dumps({"event": "end", "reason": reason})
