from __future__ import annotations

import pytest

from clinic_agent.voice import audio


def test_b64_round_trip() -> None:
    pcm = b"\x01\x02" * 160
    assert audio.b64decode(audio.b64encode(pcm)) == pcm
    assert audio.b64decode("") == b""


def test_rate_from_mime() -> None:
    assert audio.rate_from_mime("audio/pcm;rate=24000") == 24000
    assert audio.rate_from_mime("audio/pcm; rate=16000") == 16000
    assert audio.rate_from_mime("audio/pcm") == audio.DEFAULT_OUTPUT_RATE
    assert audio.rate_from_mime(None, default=8000) == 8000
    assert audio.rate_from_mime("audio/pcm;rate=abc") == audio.DEFAULT_OUTPUT_RATE


def test_whole_samples() -> None:
    assert audio.whole_samples(b"\x01\x02\x03") == b"\x01\x02"
    assert audio.whole_samples(b"\x01\x02") == b"\x01\x02"
    assert audio.whole_samples(b"") == b""


@pytest.mark.parametrize(
    ("rate", "expected"),
    [(16000, 3200), (24000, 4800), (22050, 4410), (44100, 8820)],
)
def test_chunk_size_for(rate: int, expected: int) -> None:
    size = audio.chunk_size_for(rate)
    assert size == expected  # 100 ms of 16-bit samples
    assert size % audio.SAMPLE_WIDTH == 0


def test_chunker_emits_exact_chunks_and_holds_remainder() -> None:
    c = audio.OutboundChunker(4800)
    assert c.push(b"\x01" * 1000) == []
    assert c.pending == 1000
    out = c.push(b"\x01" * 9000)  # 10000 total → two chunks, 400 left
    assert [len(x) for x in out] == [4800, 4800]
    assert c.pending == 400
    out = c.push(b"\x01" * 4400)  # exactly one more
    assert [len(x) for x in out] == [4800]
    assert c.pending == 0


def test_chunker_flush_emits_remainder_unpadded() -> None:
    c = audio.OutboundChunker(4800)
    c.push(b"\x07" * 5000)  # one chunk out, 200 held
    (tail,) = c.flush()
    assert tail == b"\x07" * 200
    assert c.pending == 0
    assert c.flush() == []


def test_chunker_flush_keeps_whole_samples() -> None:
    c = audio.OutboundChunker(4800)
    c.push(b"\x07" * 201)
    (tail,) = c.flush()
    assert len(tail) == 200


def test_chunker_clear() -> None:
    c = audio.OutboundChunker(3200)
    c.push(b"\x01" * 3000)
    c.clear()
    assert c.pending == 0
    assert c.flush() == []


@pytest.mark.parametrize("bad", [0, -2, 4801])
def test_chunker_rejects_invalid_size(bad: int) -> None:
    with pytest.raises(ValueError):
        audio.OutboundChunker(bad)
