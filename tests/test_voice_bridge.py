"""Bridge test: fake Exotel socket + fake ADK live runner yielding scripted events."""

from __future__ import annotations

import asyncio
import base64
import json

import mongomock
import numpy as np
import pytest
from google.adk.events import Event
from google.adk.sessions import InMemorySessionService
from google.genai import types
from starlette.websockets import WebSocketDisconnect

from clinic_agent.runtime import conversations, history
from clinic_agent.voice import bridge, exotel

AUTHOR = "clinic_receptionist"
STREAM = "MZtest"
CALL = "CAtest"
CALLER_RAW = "09876543210"  # Exotel national format
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
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * 300 * t) * 6000).astype("<i2").tobytes()


def _start_frame() -> str:
    return json.dumps(
        {
            "event": "start",
            "sequence_number": "1",
            "stream_sid": STREAM,
            "start": {
                "stream_sid": STREAM,
                "call_sid": CALL,
                "account_sid": "ACx",
                "from": CALLER_RAW,
                "to": "+918000000000",
                "custom_parameters": {},
                "media_format": {"encoding": "audio/x-raw", "sample_rate": "16000", "bit_rate": "16"},
            },
        }
    )


def _media_frame(pcm: bytes) -> str:
    return json.dumps(
        {"event": "media", "stream_sid": STREAM, "media": {"payload": base64.b64encode(pcm).decode()}}
    )


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

    def __init__(self, script: list[Event], on_done=None, fail_after: int | None = None) -> None:
        self.session_service = InMemorySessionService()
        self.script = script
        self.on_done = on_done
        self.fail_after = fail_after
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
            await asyncio.Event().wait()  # live sessions stay open until cancelled
        finally:
            drain.cancel()


def _audio(seconds: float) -> Event:
    return Event(
        author=AUTHOR,
        partial=True,
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    inline_data=types.Blob(data=_pcm(24000, seconds), mime_type="audio/pcm;rate=24000")
                )
            ],
        ),
    )


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


SCRIPT = [
    _audio(0.25),
    _said("Hello, ABC Clinic, how can I help?"),
    Event(author=AUTHOR, turn_complete=True),
    _heard("Book me with Doctor Asha Monday 10:30, Ravi Kumar 34 male."),
    _audio(0.3),
    Event(author=AUTHOR, interrupted=True),
    _call(),
    _resp(),
    _audio(0.15),
    _said("You're booked for Monday at 10:30."),
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
        bridge.run_call(socket, sample_rate=16000, runner=runner, run_config=bridge.build_run_config()),
        timeout=5,
    )


async def test_full_call(db):
    inbound = _pcm(16000, 0.1)
    socket = FakeSocket(
        [
            json.dumps({"event": "connected"}),
            _start_frame(),
            _media_frame(inbound),
            _media_frame(inbound),
            None,  # wait until the runner has played its script
            json.dumps({"event": "stop", "stop": {"call_sid": CALL, "reason": "callended"}}),
        ]
    )
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
    assert blobs[0].data == inbound  # 16 kHz in → passthrough
    assert runner.run_kwargs["user_id"] == PHONE
    assert socket.closed

    # --- downstream: Exotel media chunks + clear on barge-in ---
    events = [m["event"] for m in socket.sent]
    assert "clear" in events
    assert all(m["stream_sid"] == STREAM for m in socket.sent)
    media = [base64.b64decode(m["media"]["payload"]) for m in socket.sent if m["event"] == "media"]
    assert media
    assert all(len(p) % 320 == 0 and 3200 <= len(p) <= 100_000 for p in media)
    clear_at = events.index("clear")
    assert "media" in events[:clear_at]  # greeting audio went out before the barge-in
    assert "media" in events[clear_at + 1 :]  # and the post-booking reply after it
    # Before the barge-in: greeting ~0.25 s → ~8000 bytes at 16 kHz, flushed (padded) on
    # turn end to <= 9600, plus the full 3200-byte chunks of the 0.3 s clip (<= 9600)
    # streamed before `interrupted`; its unsent remainder is dropped, not flushed.
    before_clear = sum(len(p) for p in media[: events[:clear_at].count("media")])
    assert 9600 < before_clear <= 19200

    # --- persisted: transcripts (normalised phone) ---
    msgs = db["transcripts"].find_one({"phone": PHONE})["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["content"] == conversations.CALL_STARTED
    assert msgs[1]["content"] == "Hello, ABC Clinic, how can I help?"
    assert msgs[2]["content"].startswith("Book me with Doctor Asha")
    assert msgs[3]["content"] == "You're booked for Monday at 10:30."
    assert msgs[3]["tools"] == [{"name": "book_appointment", "result": BOOK_RESULT}]

    # --- persisted: ONE voice conversation, finalized, derived fields ---
    docs = list(db["conversations"].find())
    assert len(docs) == 1
    c = docs[0]
    assert c["channel"] == "voice"
    assert c["callSid"] == CALL
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


async def test_live_session_failure_still_persists(db):
    socket = FakeSocket([_start_frame(), None])
    runner = FakeRunner(SCRIPT, fail_after=3)  # greeting done, then the session dies
    await _run(socket, runner)

    assert socket.closed
    c = db["conversations"].find_one()
    assert c["channel"] == "voice"
    assert c["status"] == "abandoned"  # no tool succeeded
    assert c["messages"][0]["content"] == "Hello, ABC Clinic, how can I help?"
    assert db["transcripts"].find_one({"phone": PHONE}) is not None


async def test_socket_closes_before_start(db):
    socket = FakeSocket([json.dumps({"event": "connected"})])
    runner = FakeRunner(SCRIPT)
    await _run(socket, runner)
    assert db["conversations"].count_documents({}) == 0
    assert runner.requests == []


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


def test_exotel_helpers(monkeypatch):
    monkeypatch.setattr(exotel.settings, "VOICE_WS_TOKEN", "")
    assert exotel.token_ok(None)
    monkeypatch.setattr(exotel.settings, "VOICE_WS_TOKEN", "s3cret")
    assert exotel.token_ok("s3cret")
    assert not exotel.token_ok("nope")
    assert not exotel.token_ok(None)

    monkeypatch.setattr(exotel.settings, "VOICE_SAMPLE_RATE", 16000)
    assert exotel.requested_rate("8000") == 8000
    assert exotel.requested_rate("24000") == 24000
    assert exotel.requested_rate("44100") == 16000
    assert exotel.requested_rate(None) == 16000
    assert exotel.requested_rate("abc") == 16000


def test_exotel_route_token_and_rate(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    seen = {}

    async def fake_run_call(ws, *, sample_rate):
        seen["rate"] = sample_rate
        await ws.send_text("ok")
        await ws.close()

    monkeypatch.setattr(exotel, "run_call", fake_run_call)
    monkeypatch.setattr(exotel.settings, "VOICE_WS_TOKEN", "s3cret")
    app = FastAPI()
    app.include_router(exotel.router)
    client = TestClient(app)

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/voice/exotel?token=wrong") as ws,
    ):
        ws.receive_text()

    with client.websocket_connect("/voice/exotel?token=s3cret&sample-rate=8000") as ws:
        assert ws.receive_text() == "ok"
    assert seen["rate"] == 8000
