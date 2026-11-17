"""Local voice-call simulator: test the voice bridge without a browser.

Connects to our ``/voice/ws`` WebSocket exactly like the browser call page does:
sends ``start`` (fake caller), waits for ``ready``, streams caller audio as
real-time ``media`` frames (100 ms of 16 kHz PCM16, silence in between), records
every ``media`` payload the bridge sends back into ``out.wav`` (at the
``output_rate`` from ``ready``), prints ``transcript`` frames, then sends ``stop``.

Conversation pacing: after ``ready`` it waits for the agent's greeting; after each
utterance it keeps streaming silence until the agent's reply has finished (no new
audio for ``--reply-gap`` seconds AND the received audio would have finished
playing in real time), then plays the next utterance.

Usage (server running locally: ``uv run clinic-agent``)::

    # greeting only
    uv run python scripts/voice_sim.py

    # scripted conversation from WAV files (16-bit mono PCM, any rate)
    uv run python scripts/voice_sim.py --wav hello.wav --wav details.wav

    # or synthesise each caller utterance with macOS `say`
    uv run python scripts/voice_sim.py \\
        --say "Hi, I'd like to book an appointment" \\
        --say "My name is Ravi Kumar, I'm 34, male" ...

Options: --url (default ws://localhost:3000/voice/ws), --token (VOICE_WS_TOKEN),
--caller, --out, --reply-gap, --max-reply, --no-transcripts.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import base64
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from pathlib import Path
from urllib.parse import urlencode

from websockets.asyncio.client import connect

log = logging.getLogger("voice_sim")

INPUT_RATE = 16000  # what the bridge (and Gemini Live) expects from the caller
FRAME_MS = 100
FRAME_BYTES = INPUT_RATE * 2 * FRAME_MS // 1000


class Recorder:
    """Collects the bridge's outbound audio and tracks reply timing."""

    def __init__(self) -> None:
        self.rate = 0  # set from ``ready``
        self.audio = bytearray()
        self.last_audio_at = 0.0
        self.reply_started_at = 0.0
        self.reply_bytes = 0
        self.clears = 0
        self.ready = asyncio.Event()
        self.end_reason = ""

    def start_reply_window(self) -> None:
        self.reply_started_at = 0.0
        self.reply_bytes = 0

    def on_media(self, pcm: bytes) -> None:
        now = time.monotonic()
        if not self.reply_started_at:
            self.reply_started_at = now
        self.audio.extend(pcm)
        self.reply_bytes += len(pcm)
        self.last_audio_at = now

    def reply_done(self, gap: float) -> bool:
        if not self.reply_started_at:
            return False
        now = time.monotonic()
        playback_end = self.reply_started_at + self.reply_bytes / (2 * self.rate)
        return now - self.last_audio_at >= gap and now >= playback_end


def to_16k(pcm: bytes, src_rate: int) -> bytes:
    """Linear-interpolation resample of 16-bit mono PCM to 16 kHz (test input only)."""
    if src_rate == INPUT_RATE:
        return pcm
    src = array.array("h", pcm[: len(pcm) - len(pcm) % 2])
    if sys.byteorder == "big":
        src.byteswap()
    n_out = len(src) * INPUT_RATE // src_rate
    out = array.array("h", bytes(2 * n_out))
    step = src_rate / INPUT_RATE
    last = len(src) - 1
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        frac = pos - j
        a = src[j]
        b = src[j + 1] if j < last else a
        out[i] = int(a + (b - a) * frac)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def load_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            sys.exit(f"{path}: need 16-bit mono PCM WAV")
        pcm = w.readframes(w.getnframes())
        src_rate = w.getframerate()
    return to_16k(pcm, src_rate)


def say_to_wav(text: str, tmpdir: Path) -> Path:
    if shutil.which("say") is None:
        sys.exit("--say needs the macOS `say` command, which isn't available here; use --wav instead")
    out = tmpdir / f"say_{uuid.uuid4().hex[:8]}.wav"
    subprocess.run(
        ["say", "-o", str(out), "--file-format=WAVE", f"--data-format=LEI16@{INPUT_RATE}", text],
        check=True,
    )
    return out


async def run(args: argparse.Namespace) -> None:
    silence = b"\x00" * FRAME_BYTES
    url = args.url + (f"?{urlencode({'token': args.token})}" if args.token else "")
    call_id = uuid.uuid4().hex
    rec = Recorder()

    with tempfile.TemporaryDirectory() as td:
        utterances: list[tuple[str, bytes]] = []
        for kind, value in args.steps or []:
            path = say_to_wav(value, Path(td)) if kind == "say" else Path(value)
            utterances.append((value, load_wav(path)))

    async with connect(url, max_size=None) as ws:

        async def receiver() -> None:
            async for raw in ws:
                msg = json.loads(raw)
                event = msg.get("event")
                if event == "media":
                    rec.on_media(base64.b64decode(msg["payload"]))
                elif event == "ready":
                    rec.rate = int(msg["output_rate"])
                    log.info("<- ready (input %s Hz, output %s Hz)", msg["input_rate"], rec.rate)
                    rec.ready.set()
                elif event == "clear":
                    rec.clears += 1
                    log.info("<- clear (barge-in)")
                elif event == "transcript":
                    if msg.get("final"):
                        who = "caller" if msg["role"] == "user" else "agent"
                        log.info("<- transcript %-6s: %s", who, msg["text"])
                elif event == "end":
                    rec.end_reason = msg.get("reason", "")
                    log.info("<- end (%s)", rec.end_reason)
                else:
                    log.info("<- %s", event)

        recv_task = asyncio.create_task(receiver())
        await ws.send(
            json.dumps(
                {
                    "event": "start",
                    "call_id": call_id,
                    "caller": args.caller,
                    "transcripts": args.transcripts,
                }
            )
        )
        log.info("call %s from %s started", call_id, args.caller)

        ready = asyncio.create_task(rec.ready.wait())
        await asyncio.wait({ready, recv_task}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        if not rec.ready.is_set():
            ready.cancel()
            recv_task.cancel()
            sys.exit(f"no ready from the server (end reason: {rec.end_reason or 'none'})")

        next_at = time.monotonic()

        async def paced(pcm: bytes) -> None:
            nonlocal next_at
            for i in range(0, len(pcm), FRAME_BYTES):
                frame = pcm[i : i + FRAME_BYTES].ljust(FRAME_BYTES, b"\x00")
                await ws.send(json.dumps({"event": "media", "payload": base64.b64encode(frame).decode()}))
                next_at += FRAME_MS / 1000
                await asyncio.sleep(max(0.0, next_at - time.monotonic()))

        async def wait_for_reply(label: str) -> None:
            deadline = time.monotonic() + args.max_reply
            while not rec.reply_done(args.reply_gap):
                if recv_task.done() or time.monotonic() > deadline:
                    log.warning("no complete reply after %s (timeout or socket closed)", label)
                    return
                await paced(silence)
            log.info(
                "agent replied (%.1fs of audio) after %s", rec.reply_bytes / (2 * rec.rate), label
            )

        rec.start_reply_window()
        await wait_for_reply("connect (greeting)")
        for text, pcm in utterances:
            if recv_task.done():
                break
            log.info("-> caller: %s", text)
            rec.start_reply_window()
            await paced(pcm)
            await wait_for_reply(repr(text[:40]))

        if not recv_task.done():
            await ws.send(json.dumps({"event": "stop"}))
            await asyncio.sleep(0.5)
        recv_task.cancel()

    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rec.rate)
        w.writeframes(bytes(rec.audio))
    log.info(
        "wrote %s: %.1fs of agent audio at %d Hz (%d clear events), call_id=%s",
        args.out,
        len(rec.audio) / (2 * rec.rate),
        rec.rate,
        rec.clears,
        call_id,
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--url", default="ws://localhost:3000/voice/ws")
    p.add_argument("--token", default="")
    p.add_argument("--caller", default="+919876500001")
    p.add_argument("--out", default="out.wav")
    p.add_argument("--wav", dest="steps", action="append", type=lambda v: ("wav", v))
    p.add_argument("--say", dest="steps", action="append", type=lambda v: ("say", v))
    p.add_argument("--reply-gap", type=float, default=2.5, help="quiet seconds = reply done")
    p.add_argument("--max-reply", type=float, default=30.0, help="max seconds to wait per reply")
    p.add_argument(
        "--no-transcripts",
        dest="transcripts",
        action="store_false",
        help="don't ask the server for transcript frames",
    )
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
