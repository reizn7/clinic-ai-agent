"""PCM audio helpers for the browser call ⇄ Gemini Live bridge.

All audio is raw 16-bit signed little-endian mono PCM. The browser page captures
at 16 kHz, which is exactly what Gemini Live takes as input, and plays back at
whatever rate the model outputs (announced to the page in ``ready``), so the
server never resamples: it only base64-codes audio and re-chunks the model's
output into ~100 ms frames.
"""

from __future__ import annotations

import base64

SAMPLE_WIDTH = 2  # bytes per 16-bit sample
MODEL_INPUT_RATE = 16000
# Gemini Live's native output rate. Used only until the first audio blob tells us
# the real rate (its MIME type carries ``rate=``).
DEFAULT_OUTPUT_RATE = 24000


def b64decode(payload: str) -> bytes:
    return base64.b64decode(payload) if payload else b""


def b64encode(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode("ascii")


def rate_from_mime(mime: str | None, default: int = DEFAULT_OUTPUT_RATE) -> int:
    """``"audio/pcm;rate=24000"`` → 24000 (``default`` if absent/unparseable)."""
    for part in (mime or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == "rate":
            try:
                return int(value)
            except ValueError:
                return default
    return default


def whole_samples(pcm: bytes) -> bytes:
    """Drop a trailing odd byte so the buffer holds whole 16-bit samples."""
    return pcm[: len(pcm) - len(pcm) % SAMPLE_WIDTH]


def chunk_size_for(rate: int, target_ms: int = 100) -> int:
    """Bytes in ~``target_ms`` of audio at ``rate``, a whole number of samples."""
    return max(1, rate * target_ms // 1000) * SAMPLE_WIDTH


class OutboundChunker:
    """Buffers outbound PCM and emits fixed-size, sample-aligned chunks.

    ``push`` returns only full chunks and keeps the remainder for the next call.
    ``flush`` (on model turn end) emits what's left as-is. ``clear`` drops
    everything (barge-in).
    """

    def __init__(self, chunk_size: int) -> None:
        if chunk_size <= 0 or chunk_size % SAMPLE_WIDTH:
            raise ValueError(f"chunk size must be a positive whole number of samples: {chunk_size}")
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
        tail = whole_samples(bytes(self._buf))
        self._buf.clear()
        return [tail] if tail else []

    def clear(self) -> None:
        self._buf.clear()
