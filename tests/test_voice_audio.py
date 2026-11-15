from __future__ import annotations

import numpy as np
import pytest

from clinic_agent.voice import audio


def _tone(rate: int, seconds: float, freq: float = 440.0) -> bytes:
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * freq * t) * 8000).astype("<i2").tobytes()


def test_b64_round_trip() -> None:
    pcm = b"\x01\x02" * 160
    assert audio.b64decode(audio.b64encode(pcm)) == pcm
    assert audio.b64decode("") == b""


def test_rate_from_mime() -> None:
    assert audio.rate_from_mime("audio/pcm;rate=24000") == 24000
    assert audio.rate_from_mime("audio/pcm; rate=16000") == 16000
    assert audio.rate_from_mime("audio/pcm") == 24000
    assert audio.rate_from_mime(None, default=8000) == 8000
    assert audio.rate_from_mime("audio/pcm;rate=abc") == 24000


@pytest.mark.parametrize(
    ("rate", "expected"),
    [(8000, 3200), (16000, 3200), (24000, 4800), (48000, 9600)],
)
def test_chunk_size_for(rate: int, expected: int) -> None:
    size = audio.chunk_size_for(rate)
    assert size == expected
    assert size % 320 == 0
    assert audio.EXOTEL_MIN_CHUNK <= size <= audio.EXOTEL_MAX_CHUNK


def test_chunker_emits_exact_chunks_and_holds_remainder() -> None:
    c = audio.OutboundChunker(3200)
    assert c.push(b"\x01" * 1000) == []
    assert c.pending == 1000
    out = c.push(b"\x01" * 6000)  # 7000 total → two chunks, 600 left
    assert [len(x) for x in out] == [3200, 3200]
    assert c.pending == 600
    out = c.push(b"\x01" * 2600)  # exactly one more
    assert [len(x) for x in out] == [3200]
    assert c.pending == 0


def test_chunker_flush_pads_to_multiple_and_minimum() -> None:
    c = audio.OutboundChunker(4800)
    c.push(b"\x07" * 5000)  # one chunk out, 200 held
    (tail,) = c.flush()
    assert len(tail) == 3200  # padded up to the Exotel minimum
    assert tail[:200] == b"\x07" * 200
    assert set(tail[200:]) == {0}
    assert c.flush() == []

    c.push(b"\x07" * 4000)  # under one chunk; 4000 → next multiple of 320 = 4160
    (tail,) = c.flush()
    assert len(tail) == 4160
    assert len(tail) % 320 == 0


def test_chunker_clear() -> None:
    c = audio.OutboundChunker(3200)
    c.push(b"\x01" * 3000)
    c.clear()
    assert c.pending == 0
    assert c.flush() == []


def test_chunker_rejects_invalid_size() -> None:
    for bad in (3000, 3210, 320, 100_160):
        with pytest.raises(ValueError):
            audio.OutboundChunker(bad)


@pytest.mark.parametrize(("src", "dst"), [(24000, 16000), (24000, 8000), (8000, 16000)])
def test_resample_length(src: int, dst: int) -> None:
    pcm = _tone(src, 1.0)
    out = audio.resample(pcm, src, dst)
    n_out = len(out) // 2
    assert abs(n_out - dst) <= dst * 0.01  # 1 s in → ~1 s out


def test_stream_resampler_chunked_matches_total_length() -> None:
    r = audio.StreamResampler(24000, 16000)
    pcm = _tone(24000, 1.0)
    out = b""
    # odd-sized chunks, including split samples, must not lose data
    step = 4801
    for i in range(0, len(pcm), step):
        out += r.process(pcm[i : i + step])
    out += r.process(b"", last=True)
    assert len(out) % 2 == 0
    assert abs(len(out) // 2 - 16000) <= 160


def test_stream_resampler_passthrough_and_reset() -> None:
    r = audio.StreamResampler(16000, 16000)
    assert r.process(b"\x01\x02\x03") == b"\x01\x02"  # odd byte carried
    assert r.process(b"\x04") == b"\x03\x04"
    r.reset()
    assert r.process(b"\x05\x06") == b"\x05\x06"
