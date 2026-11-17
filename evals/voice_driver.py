"""Drive one real voice call in-process for evals.

Plays the part of the browser call page against ``voice.bridge.CallBridge``
(through a fake ``CallSocket``, no server or WebSocket): the caller's lines are
synthesised with macOS ``say`` into 16 kHz PCM16 and streamed as real-time
``media`` frames (100 ms, silence in between), so the whole voice path runs for
real: Gemini Live native audio, its input/output transcription, the tools, and
the bridge's transcript + conversation persistence.

Turn-taking: after connecting it waits for the greeting; after each caller line
it waits until the agent's reply is over (no new audio for ``reply_gap`` seconds,
no tool call in flight, and no tool finished since the last audio), then speaks
the next line.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import uuid
import wave
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from starlette.websockets import WebSocketDisconnect

from clinic_agent.voice import audio, protocol
from clinic_agent.voice.bridge import CallBridge, build_run_config, get_runner

INPUT_RATE = 16000
FRAME_MS = 100
FRAME_BYTES = INPUT_RATE * 2 * FRAME_MS // 1000
SILENCE = b"\x00" * FRAME_BYTES


class FakeCallSocket:
    """In-process stand-in for the call page's WebSocket (``bridge.CallSocket``)."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.last_media_at = 0.0
        self.end_reason = ""

    async def receive_text(self) -> str:
        msg = await self.inbox.get()
        if msg is None:
            raise WebSocketDisconnect()
        return msg

    async def send_text(self, data: str) -> None:
        ev = json.loads(data)
        if ev["event"] == "media":
            self.last_media_at = time.monotonic()
        elif ev["event"] == "end":
            self.end_reason = ev.get("reason", "")

    async def close(self, code: int = 1000) -> None:
        self.inbox.put_nowait(None)


def synthesize(text: str, voice: str) -> bytes:
    """``text`` → 16 kHz mono PCM16 via macOS ``say``."""
    if shutil.which("say") is None:
        raise RuntimeError("voice evals need the macOS `say` command")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "u.wav"
        subprocess.run(
            [
                "say",
                "-v",
                voice,
                "-o",
                str(out),
                "--file-format=WAVE",
                f"--data-format=LEI16@{INPUT_RATE}",
                text,
            ],
            check=True,
        )
        with wave.open(str(out), "rb") as w:
            return w.readframes(w.getnframes())


async def run_voice_call(
    phone: str,
    lines: list[str],
    *,
    voice: str = "Aman",
    reply_gap: float = 3.0,
    max_wait: float = 60.0,
    progress: Callable[[str], None] = lambda _msg: None,
) -> dict[str, Any]:
    """Run one call; return ``{turns, tool_calls, end_reason, timed_out}``.

    ``progress`` gets one line per caller utterance / agent reply as they happen.
    """
    sock = FakeCallSocket()
    start = protocol.CallStart(call_id=uuid.uuid4().hex, caller=phone, transcripts=False)
    bridge = CallBridge(sock, start, runner=get_runner(), run_config=build_run_config())
    frames: deque[bytes] = deque()
    stop = asyncio.Event()
    timed_out: list[str] = []

    async def feeder() -> None:
        # Real-time pacing, like a microphone: one 100 ms frame every 100 ms.
        nxt = time.monotonic()
        while not stop.is_set():
            frame = frames.popleft() if frames else SILENCE
            sock.inbox.put_nowait(protocol.media_event(audio.b64encode(frame)))
            nxt += FRAME_MS / 1000
            await asyncio.sleep(max(0.0, nxt - time.monotonic()))

    async def wait_reply(after: float, label: str) -> None:
        deadline = time.monotonic() + max_wait
        seen_tools = len(bridge.recorder.calls)
        tool_done_at = 0.0
        while time.monotonic() < deadline and not call.done():
            await asyncio.sleep(0.2)
            if len(bridge.recorder.calls) != seen_tools:
                seen_tools = len(bridge.recorder.calls)
                tool_done_at = time.monotonic()
            started = sock.last_media_at > after
            quiet = time.monotonic() - sock.last_media_at >= reply_gap
            settled = not bridge.recorder.has_pending and tool_done_at <= sock.last_media_at
            if started and quiet and settled:
                break
        else:
            if not call.done():
                timed_out.append(label)
                progress(f"  ! no complete reply within {max_wait:.0f}s ({label[:40]})")
        turns = bridge.transcript.turns
        reply = turns[-1]["assistant"] if turns else ""
        tools = [c.get("name") for c in (turns[-1]["tools"] if turns else [])]
        progress(
            f"  agent: {reply[:160] or '(nothing transcribed)'}"
            + (f"  [tools {tools}]" if tools else "")
        )

    call = asyncio.create_task(bridge.run(), name="eval-voice-call")
    feed = asyncio.create_task(feeder(), name="eval-voice-feeder")
    try:
        await wait_reply(0.0, "greeting")
        for line in lines:
            if call.done():
                progress(
                    f"  ! call ended early ({sock.end_reason or bridge.end_reason or 'closed'})"
                )
                break
            progress(f"  caller: {line}")
            pcm = await asyncio.to_thread(synthesize, line, voice)
            pcm += b"\x00" * (-len(pcm) % FRAME_BYTES)
            frames.extend(pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES))
            while frames and not call.done():  # noqa: ASYNC110 (feeder drains the deque)
                await asyncio.sleep(0.05)
            await wait_reply(time.monotonic(), line)
    finally:
        stop.set()
        await feed
        if not call.done():
            sock.inbox.put_nowait(json.dumps({"event": "stop"}))
        await asyncio.wait_for(call, timeout=30)

    return {
        "turns": bridge.transcript.turns,
        "tool_calls": bridge.recorder.calls,
        "end_reason": sock.end_reason or bridge.end_reason,
        "timed_out": timed_out,
    }
