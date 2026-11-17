"""Bridge tests: fake call-page socket + fake ADK live runner yielding scripted events."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import struct

import mongomock
import pytest
from google.adk.events import Event
from google.adk.sessions import InMemorySessionService
from google.genai import types
from starlette.websockets import WebSocketDisconnect

from clinic_agent.runtime import conversations, history
from clinic_agent.voice import bridge, protocol

AUTHOR = "clinic_receptionist"
CALL = "call-test-1"
CALLER_TYPED = "098765 43210"  # as typed on the page (trunk 0, spaces)
PHONE = "919876543210"  # normalised

BOOK_ARGS = {
    "patient_name": "Ravi Kumar",
    "age": 34,
    "gender": "Male",
    "doctor_name": "Asha Mehta",
    "date": "2026-11-16",
    "slot": "10:30",
    "phone": PHONE,
}
BOOK_RESULT = {"success": True, "appointmentId": "appt1", "status": "Pending"}


def _pcm(rate: int, seconds: float) -> bytes:
    n = int(rate * seconds)
    return struct.pack(f"<{n}h", *(int(6000 * math.sin(2 * math.pi * 300 * i / rate)) for i in range(n)))


GREETING_PCM = _pcm(24000, 0.25)  # 12000 bytes
INTERRUPTED_PCM = _pcm(24000, 0.35)  # 16800 bytes: 3 full chunks + 2400 dropped
REPLY_PCM = _pcm(24000, 0.15)  # 7200 bytes
CHUNK = 4800  # 100 ms at 24 kHz


def _start_frame(caller: str = CALLER_TYPED, transcripts: bool = True) -> str:
    return json.dumps(
        {"event": "start", "call_id": CALL, "caller": caller, "transcripts": transcripts}
    )


def _media_frame(pcm: bytes) -> str:
    return json.dumps({"event": "media", "payload": base64.b64encode(pcm).decode()})


STOP = json.dumps({"event": "stop"})


class FakeSocket:
    """Plays scripted inbound frames; a ``None`` entry waits for ``release``."""

    def __init__(self, frames: list[str | None]) -> None:
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.closed = False
        self.release = asyncio.Event()

    async def receive_text(self) -> str:
        if not self.frames:
            raise WebSocketDisconnect(1000)
        frame = self.frames.pop(0)
        if frame is None:
            await self.release.wait()
            return await self.receive_text()
        await asyncio.sleep(0)
        return frame

    async def send_text(self, data: str) -> None:
        if self.closed:
            raise RuntimeError("socket closed")
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000) -> None:
        self.closed = True


class FakeRunner:
    """Stands in for ``Runner.run_live``: records queue traffic, yields a script."""

    app_name = "test-voice"

    def __init__(
        self,
        script: list[Event],
        on_done=None,
        fail_after: int | None = None,
        end_after_script: bool = False,
    ) -> None:
        self.session_service = InMemorySessionService()
        self.script = script
        self.on_done = on_done
        self.fail_after = fail_after
        self.end_after_script = end_after_script
        self.requests = []
        self.run_kwargs: dict = {}

    async def _drain(self, queue) -> None:
        while True:
            req = await queue.get()
            self.requests.append(req)
            if req.close:
                return

    async def run_live(self, *, user_id, session_id, live_request_queue, run_config):
        self.run_kwargs = {"user_id": user_id, "session_id": session_id, "run_config": run_config}
        drain = asyncio.create_task(self._drain(live_request_queue))
        try:
            # wait for the seeded context before "speaking"
            while not self.requests:  # noqa: ASYNC110 (test fake: yield until seeded)
                await asyncio.sleep(0)
            for i, ev in enumerate(self.script):
                if self.fail_after is not None and i == self.fail_after:
                    raise RuntimeError("live session blew up")
                await asyncio.sleep(0)
                yield ev
            if self.on_done:
                self.on_done()
            if self.end_after_script:
                return
            await asyncio.Event().wait()  # live sessions stay open until cancelled
        finally:
            drain.cancel()


def _audio(pcm: bytes) -> Event:
    return Event(
        author=AUTHOR,
        partial=True,
        content=types.Content(
            role="model",
            parts=[types.Part(inline_data=types.Blob(data=pcm, mime_type="audio/pcm;rate=24000"))],
        ),
    )


def _saying(delta: str) -> Event:
    return Event(
        author=AUTHOR, partial=True, output_transcription=types.Transcription(text=delta)
    )


def _hearing(delta: str) -> Event:
    return Event(author=AUTHOR, partial=True, input_transcription=types.Transcription(text=delta))


def _said(text: str) -> Event:
    return Event(author=AUTHOR, output_transcription=types.Transcription(text=text, finished=True))


def _heard(text: str) -> Event:
    return Event(author=AUTHOR, input_transcription=types.Transcription(text=text, finished=True))


def _call() -> Event:
    fc = types.FunctionCall(id="fc1", name="book_appointment", args=BOOK_ARGS)
    return Event(author=AUTHOR, content=types.Content(role="model", parts=[types.Part(function_call=fc)]))


def _resp() -> Event:
    fr = types.FunctionResponse(id="fc1", name="book_appointment", response=BOOK_RESULT)
    return Event(author=AUTHOR, content=types.Content(role="user", parts=[types.Part(function_response=fr)]))


GREETING = "Hello, ABC Clinic, how can I help?"
REQUEST = "Book me with Doctor Asha Monday 10:30, Ravi Kumar 34 male."
BOOKED = "You're booked for Monday at 10:30."

SCRIPT = [
    _audio(GREETING_PCM),
    _saying("Hello, ABC Clinic,"),
    _saying(" how can I help?"),
    _said(GREETING),
    Event(author=AUTHOR, turn_complete=True),
    _hearing("Book me"),
    _heard(REQUEST),
    _audio(INTERRUPTED_PCM),
    Event(author=AUTHOR, interrupted=True),
    _call(),
    _resp(),
    _audio(REPLY_PCM),
    _said(BOOKED),
    Event(author=AUTHOR, turn_complete=True),
]


@pytest.fixture
def db(monkeypatch):
    fake = mongomock.MongoClient()["test"]
    monkeypatch.setattr(conversations, "get_db", lambda: fake)
    monkeypatch.setattr(history, "get_db", lambda: fake)
    return fake


async def _run(socket: FakeSocket, runner: FakeRunner) -> None:
    await asyncio.wait_for(
        bridge.run_call(socket, runner=runner, run_config=bridge.build_run_config()),
        timeout=5,
    )


def _full_call_socket(transcripts: bool = True) -> FakeSocket:
    inbound = _pcm(16000, 0.1)
    return FakeSocket(
        [
            _start_frame(transcripts=transcripts),
            _media_frame(inbound),
            _media_frame(inbound),
            None,  # wait until the runner has played its script
            STOP,
        ]
    )


def _media(sent: list[dict]) -> list[bytes]:
    return [base64.b64decode(m["payload"]) for m in sent if m["event"] == "media"]


async def test_full_call(db):
    socket = _full_call_socket()
    runner = FakeRunner(SCRIPT, on_done=socket.release.set)
    await _run(socket, runner)

    # --- upstream: context first, then 16 kHz realtime audio, then close ---
    first = runner.requests[0]
    assert first.content is not None
    seeded = first.content.parts[0].text
    assert "phoning the clinic from number: 919876543210" in seeded
    assert bridge.GREETING_CUE in seeded
    blobs = [r.blob for r in runner.requests if r.blob]
    assert len(blobs) == 2
    assert all(b.mime_type == "audio/pcm;rate=16000" for b in blobs)
    assert blobs[0].data == _pcm(16000, 0.1)  # forwarded untouched
    assert runner.run_kwargs["user_id"] == PHONE
    assert socket.closed

    # --- downstream: ready first, native-rate media, clear on barge-in ---
    assert socket.sent[0] == {"event": "ready", "input_rate": 16000, "output_rate": 24000}
    events = [m["event"] for m in socket.sent]
    assert events.count("ready") == 1
    assert "end" not in events  # the caller hung up; the server didn't end it
    clear_at = events.index("clear")
    before = _media(socket.sent[:clear_at])
    after = _media(socket.sent[clear_at + 1 :])
    # Greeting: two 100 ms chunks + the 2400-byte tail flushed on turn_complete,
    # passed through byte-for-byte at the model's 24 kHz.
    assert [len(p) for p in before[:3]] == [CHUNK, CHUNK, 2400]
    assert b"".join(before[:3]) == GREETING_PCM
    # Interrupted clip: its 3 full chunks went out before `interrupted`; the
    # unsent 2400-byte remainder is dropped, not flushed.
    assert [len(p) for p in before[3:]] == [CHUNK] * 3
    assert b"".join(after) == REPLY_PCM

    # --- transcript frames: partials carry the line so far, then final ---
    lines = [(m["role"], m["text"], m["final"]) for m in socket.sent if m["event"] == "transcript"]
    assert lines == [
        ("assistant", "Hello, ABC Clinic,", False),
        ("assistant", GREETING, False),
        ("assistant", GREETING, True),
        ("user", "Book me", False),
        ("user", REQUEST, True),
        ("assistant", BOOKED, True),
    ]

    # --- persisted: transcripts (normalised phone) ---
    msgs = db["transcripts"].find_one({"phone": PHONE})["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["content"] == conversations.CALL_STARTED
    assert msgs[1]["content"] == GREETING
    assert msgs[2]["content"] == REQUEST
    assert msgs[3]["content"] == BOOKED
    assert msgs[3]["tools"] == [{"name": "book_appointment", "result": BOOK_RESULT}]

    # --- persisted: ONE voice conversation, finalized, derived fields ---
    docs = list(db["conversations"].find())
    assert len(docs) == 1
    c = docs[0]
    assert c["channel"] == "voice"
    assert c["callId"] == CALL
    assert c["phone"] == "+919876543210"
    assert c["status"] == "completed"
    assert c["outcome"] == "appointment_booked"
    assert c["doctorName"] == "Asha Mehta"
    assert c["appointmentAt"] == "2026-11-16T10:30:00"
    assert isinstance(c["durationSec"], int)
    tool_msgs = [m for m in c["messages"] if m["type"] == "tool_call"]
    assert tool_msgs[0]["tool"]["name"] == "book_appointment"
    assert tool_msgs[0]["tool"]["status"] == "success"
    assert tool_msgs[0]["tool"]["args"] == BOOK_ARGS


async def test_no_transcript_frames_unless_requested(db):
    socket = _full_call_socket(transcripts=False)
    runner = FakeRunner(SCRIPT, on_done=socket.release.set)
    await _run(socket, runner)

    events = [m["event"] for m in socket.sent]
    assert "transcript" not in events
    assert "clear" in events and "media" in events
    # still recorded server-side
    assert db["conversations"].find_one()["outcome"] == "appointment_booked"


class FlakyTranscriptSocket(FakeSocket):
    """Fails every transcript send; audio must be unaffected."""

    async def send_text(self, data: str) -> None:
        if json.loads(data)["event"] == "transcript":
            raise RuntimeError("transcript send failed")
        await super().send_text(data)


async def test_transcript_send_failure_never_disrupts_audio(db):
    inbound = _pcm(16000, 0.1)
    socket = FlakyTranscriptSocket([_start_frame(), _media_frame(inbound), None, STOP])
    runner = FakeRunner(SCRIPT, on_done=socket.release.set)
    await _run(socket, runner)

    events = [m["event"] for m in socket.sent]
    assert "end" not in events
    assert b"".join(_media(socket.sent[events.index("clear") + 1 :])) == REPLY_PCM
    assert db["conversations"].find_one()["outcome"] == "appointment_booked"


async def test_live_session_failure_ends_call_and_still_persists(db):
    socket = FakeSocket([_start_frame(), None])
    runner = FakeRunner(SCRIPT, fail_after=5)  # greeting done, then the session dies
    await _run(socket, runner)

    assert socket.closed
    assert socket.sent[-1] == {"event": "end", "reason": "error"}
    c = db["conversations"].find_one()
    assert c["channel"] == "voice"
    assert c["callId"] == CALL
    assert c["status"] == "abandoned"  # no tool succeeded
    assert c["messages"][0]["content"] == GREETING
    assert db["transcripts"].find_one({"phone": PHONE}) is not None


async def test_live_session_ending_sends_end(db):
    socket = FakeSocket([_start_frame(), None])
    runner = FakeRunner(SCRIPT, end_after_script=True)
    await _run(socket, runner)

    assert socket.sent[-1] == {"event": "end", "reason": "session_ended"}
    assert socket.closed
    assert db["conversations"].find_one()["status"] == "completed"


@pytest.mark.parametrize("caller", ["", "   ", "12345", "not a number", "+91 98765"])
async def test_invalid_caller_is_rejected(db, caller):
    socket = FakeSocket([_start_frame(caller=caller), None])
    runner = FakeRunner(SCRIPT)
    await _run(socket, runner)

    assert socket.sent == [{"event": "end", "reason": "invalid_caller"}]
    assert socket.closed
    assert runner.requests == []  # no live session
    assert runner.run_kwargs == {}
    assert db["conversations"].count_documents({}) == 0
    assert db["transcripts"].count_documents({}) == 0


async def test_missing_caller_is_rejected(db):
    socket = FakeSocket([json.dumps({"event": "start", "call_id": CALL}), None])
    runner = FakeRunner(SCRIPT)
    await _run(socket, runner)
    assert socket.sent == [{"event": "end", "reason": "invalid_caller"}]
    assert db["conversations"].count_documents({}) == 0


async def test_socket_closes_before_start(db):
    socket = FakeSocket(["not json", json.dumps({"event": "media", "payload": ""})])
    runner = FakeRunner(SCRIPT)
    await _run(socket, runner)
    assert socket.sent == []
    assert db["conversations"].count_documents({}) == 0
    assert runner.requests == []


async def test_no_start_times_out(db, monkeypatch):
    monkeypatch.setattr(bridge, "START_TIMEOUT_SECONDS", 0.05)
    socket = FakeSocket([None])  # never sends start
    runner = FakeRunner(SCRIPT)
    await _run(socket, runner)
    assert socket.sent == [{"event": "end", "reason": protocol.END_NO_START}]
    assert socket.closed
    assert db["conversations"].count_documents({}) == 0


def test_call_transcript_grouping():
    t = bridge.CallTranscript()
    t.assistant("Hello!")
    t.user("Hi")
    t.user("I want to book")  # same turn until the agent answers
    t.tool({"name": "list_doctors"})
    t.assistant("Which doctor?")
    t.user("Asha")
    assert [(x["user"], x["assistant"]) for x in t.turns] == [
        (conversations.CALL_STARTED, "Hello!"),
        ("Hi I want to book", "Which doctor?"),
        ("Asha", ""),
    ]
    assert t.turns[1]["tools"] == [{"name": "list_doctors"}]
