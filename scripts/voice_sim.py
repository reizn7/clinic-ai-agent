"""Local Exotel Voicebot simulator: test the voice bridge without a phone call.

Connects to our ``/voice/exotel`` WebSocket exactly like Exotel's Voicebot
applet would: sends ``connected`` + ``start`` (fake caller), streams caller audio
as real-time ``media`` events (100 ms frames, silence in between), records every
``media`` payload the bridge sends back into ``out.wav``, then sends ``stop``.

Conversation pacing: after connecting it waits for the agent's greeting; after
each utterance it keeps streaming silence until the agent's reply has finished
(no new audio for ``--reply-gap`` seconds AND the received audio would have
finished playing in real time), then plays the next utterance.

Usage (server running locally: ``uv run clinic-agent``)::

    # greeting only
    uv run python scripts/voice_sim.py

    # scripted conversation from WAV files (16-bit mono PCM; resampled if needed)
    uv run python scripts/voice_sim.py --wav hello.wav --wav details.wav

    # or synthesise each caller utterance with macOS `say`
    uv run python scripts/voice_sim.py \\
        --say "Hi, I'd like to book an appointment" \\
        --say "My name is Ravi Kumar, I'm 34, male" ...

Options: --url (default ws://localhost:3000/voice/exotel), --rate (8000/16000/
24000, default 16000), --token (VOICE_WS_TOKEN), --caller, --out.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from pathlib import Path
from urllib.parse import urlencode

from websockets.asyncio.client import connect

from clinic_agent.voice.audio import resample

log = logging.getLogger("voice_sim")

FRAME_MS = 100


class Recorder:
    """Collects the bridge's outbound audio and tracks reply timing."""

    def __init__(self, rate: int) -> None:
        self.rate = rate
        self.audio = bytearray()
        self.last_audio_at = 0.0
        self.reply_started_at = 0.0
        self.reply_bytes = 0
        self.clears = 0

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


def load_wav(path: Path, rate: int) -> bytes:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            sys.exit(f"{path}: need 16-bit mono PCM WAV")
        pcm = w.readframes(w.getnframes())
        src_rate = w.getframerate()
    return pcm if src_rate == rate else resample(pcm, src_rate, rate)


def say_to_wav(text: str, rate: int, tmpdir: Path) -> Path:
    if sys.platform != "darwin":
        sys.exit("--say needs macOS `say`; pass --wav files instead")
    out = tmpdir / f"say_{uuid.uuid4().hex[:8]}.wav"
    subprocess.run(
        ["say", "-o", str(out), "--file-format=WAVE", f"--data-format=LEI16@{rate}", text],
        check=True,
    )
    return out


async def run(args: argparse.Namespace) -> None:
    rate = args.rate
    frame_bytes = rate * 2 * FRAME_MS // 1000
    silence = b"\x00" * frame_bytes
    query = {"sample-rate": rate}
    if args.token:
        query["token"] = args.token
    url = f"{args.url}?{urlencode(query)}"
    stream_sid = f"MZ{uuid.uuid4().hex[:24]}"
    call_sid = f"CA{uuid.uuid4().hex[:24]}"
    rec = Recorder(rate)

    with tempfile.TemporaryDirectory() as td:
        utterances: list[tuple[str, bytes]] = []
        for kind, value in args.steps or []:
            path = say_to_wav(value, rate, Path(td)) if kind == "say" else Path(value)
            utterances.append((value, load_wav(path, rate)))

    async with connect(url, max_size=None) as ws:
        seq = 0

        async def send(event: dict) -> None:
            nonlocal seq
            seq += 1
            await ws.send(json.dumps({"sequence_number": str(seq), **event}))

        async def send_frame(pcm: bytes, chunk: int) -> None:
            await send(
                {
                    "event": "media",
                    "stream_sid": stream_sid,
                    "media": {
                        "chunk": str(chunk),
                        "timestamp": str(chunk * FRAME_MS),
                        "payload": base64.b64encode(pcm).decode(),
                    },
                }
            )

        async def receiver() -> None:
            async for raw in ws:
                msg = json.loads(raw)
                event = msg.get("event")
                if event == "media":
                    rec.on_media(base64.b64decode(msg["media"]["payload"]))
                elif event == "clear":
                    rec.clears += 1
                    log.info("<- clear (barge-in)")
                else:
                    log.info("<- %s", event)

        recv_task = asyncio.create_task(receiver())
        await ws.send(json.dumps({"event": "connected"}))
        await send(
            {
                "event": "start",
                "stream_sid": stream_sid,
                "start": {
                    "stream_sid": stream_sid,
                    "call_sid": call_sid,
                    "account_sid": "ACsimulator",
                    "from": args.caller,
                    "to": "+918000000000",
                    "custom_parameters": {},
                    "media_format": {
                        "encoding": "audio/x-raw",
                        "sample_rate": str(rate),
                        "bit_rate": "16",
                    },
                },
            }
        )
        log.info("call %s from %s started", call_sid, args.caller)

        chunk = 0
        next_at = time.monotonic()

        async def paced(pcm: bytes) -> None:
            nonlocal chunk, next_at
            for i in range(0, len(pcm), frame_bytes):
                frame = pcm[i : i + frame_bytes].ljust(frame_bytes, b"\x00")
                chunk += 1
                await send_frame(frame, chunk)
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
                "agent replied (%.1fs of audio) after %s", rec.reply_bytes / (2 * rate), label
            )

        rec.start_reply_window()
        await wait_for_reply("connect (greeting)")
        for text, pcm in utterances:
            log.info("-> caller: %s", text)
            rec.start_reply_window()
            await paced(pcm)
            await wait_for_reply(repr(text[:40]))

        await send(
            {
                "event": "stop",
                "stream_sid": stream_sid,
                "stop": {"call_sid": call_sid, "account_sid": "ACsimulator", "reason": "callended"},
            }
        )
        await asyncio.sleep(0.5)
        recv_task.cancel()

    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(rec.audio))
    log.info(
        "wrote %s: %.1fs of agent audio (%d clear events), call_sid=%s",
        args.out,
        len(rec.audio) / (2 * rate),
        rec.clears,
        call_sid,
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--url", default="ws://localhost:3000/voice/exotel")
    p.add_argument("--rate", type=int, default=16000, choices=[8000, 16000, 24000])
    p.add_argument("--token", default="")
    p.add_argument("--caller", default="+919876500001")
    p.add_argument("--out", default="out.wav")
    p.add_argument("--wav", dest="steps", action="append", type=lambda v: ("wav", v))
    p.add_argument("--say", dest="steps", action="append", type=lambda v: ("say", v))
    p.add_argument("--reply-gap", type=float, default=2.5, help="quiet seconds = reply done")
    p.add_argument("--max-reply", type=float, default=30.0, help="max seconds to wait per reply")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
