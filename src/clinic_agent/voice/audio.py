"""PCM audio helpers for the Exotel ⇄ Gemini Live bridge.

Formats on each side (all raw 16-bit signed little-endian mono PCM):

* Exotel Voicebot: base64 in JSON, at the rate requested via ``?sample-rate=``
  (8000 / 16000 / 24000; we default to ``VOICE_SAMPLE_RATE`` = 16000).
* Gemini Live: input 16 kHz (``audio/pcm;rate=16000``), output 24 kHz.

Python 3.14 has no ``audioop``, so resampling uses ``soxr`` (streaming, so chunk
boundaries don't click). ``OutboundChunker`` enforces Exotel's outbound rule:
each ``media`` payload must be a multiple of 320 bytes, between 3,200 and
100,000 bytes.
"""

from __future__ import annotations

import base64

import numpy as np
import soxr

SAMPLE_WIDTH = 2  # bytes per 16-bit sample
MODEL_INPUT_RATE = 16000
MODEL_OUTPUT_RATE = 24000

# Exotel outbound constraints (developer.exotel.com/docs/agentstream).
EXOTEL_CHUNK_MULTIPLE = 320
EXOTEL_MIN_CHUNK = 3200
EXOTEL_MAX_CHUNK = 100_000


def b64decode(payload: str) -> bytes:
    return base64.b64decode(payload) if payload else b""


def b64encode(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode("ascii")


def rate_from_mime(mime: str | None, default: int = MODEL_OUTPUT_RATE) -> int:
    """``"audio/pcm;rate=24000"`` → 24000 (``default`` if absent/unparseable)."""
    for part in (mime or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == "rate":
            try:
                return int(value)
            except ValueError:
                return default
    return default


class StreamResampler:
    """Stateful int16 mono resampler; a passthrough when the rates match."""

    def __init__(self, in_rate: int, out_rate: int) -> None:
        self.in_rate = in_rate
        self.out_rate = out_rate
        self._carry = b""  # an odd trailing byte from the previous chunk
        self._stream = self._new_stream()

    def _new_stream(self) -> soxr.ResampleStream | None:
        if self.in_rate == self.out_rate:
            return None
        return soxr.ResampleStream(self.in_rate, self.out_rate, 1, dtype="int16")

    def process(self, pcm: bytes, last: bool = False) -> bytes:
        data = self._carry + pcm
        usable = len(data) - (len(data) % SAMPLE_WIDTH)
        data, self._carry = data[:usable], data[usable:]
        if self._stream is None:
            return data
        samples = np.frombuffer(data, dtype="<i2")
        return self._stream.resample_chunk(samples, last=last).astype("<i2").tobytes()

    def reset(self) -> None:
        """Drop internal state (e.g. after a barge-in discards queued audio)."""
        self._carry = b""
        self._stream = self._new_stream()


def resample(pcm: bytes, in_rate: int, out_rate: int) -> bytes:
    """One-shot resample of a complete buffer."""
    return StreamResampler(in_rate, out_rate).process(pcm, last=True)


def chunk_size_for(rate: int, target_ms: int = 100) -> int:
    """~``target_ms`` of audio, rounded down to 320 bytes, clamped to Exotel limits."""
    raw = rate * SAMPLE_WIDTH * target_ms // 1000
    size = raw - raw % EXOTEL_CHUNK_MULTIPLE
    return max(EXOTEL_MIN_CHUNK, min(EXOTEL_MAX_CHUNK, size))


class OutboundChunker:
    """Buffers outbound PCM and emits Exotel-sized chunks.

    ``push`` returns only full chunks and keeps the remainder for the next call.
    ``flush`` (on model turn end) emits what's left, zero-padded (silence) up to a
    multiple of 320 bytes and at least Exotel's 3,200-byte minimum. ``clear``
    drops everything (barge-in).
    """

    def __init__(self, chunk_size: int) -> None:
        if chunk_size % EXOTEL_CHUNK_MULTIPLE or not (
            EXOTEL_MIN_CHUNK <= chunk_size <= EXOTEL_MAX_CHUNK
        ):
            raise ValueError(f"invalid Exotel chunk size: {chunk_size}")
        self.chunk_size = chunk_size
        self._buf = bytearray()

    @property
    def pending(self) -> int:
        return len(self._buf)

    def push(self, pcm: bytes) -> list[bytes]:
        self._buf.extend(pcm)
        out: list[bytes] = []
        while len(self._buf) >= self.chunk_size:
            out.append(bytes(self._buf[: self.chunk_size]))
            del self._buf[: self.chunk_size]
        return out

    def flush(self) -> list[bytes]:
        if not self._buf:
            return []
        m = EXOTEL_CHUNK_MULTIPLE
        size = max(EXOTEL_MIN_CHUNK, -(-len(self._buf) // m) * m)
        chunk = bytes(self._buf) + b"\x00" * (size - len(self._buf))
        self._buf.clear()
        return [chunk]

    def clear(self) -> None:
        self._buf.clear()
