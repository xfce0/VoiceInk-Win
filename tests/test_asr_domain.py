from __future__ import annotations

import pytest

from voiceink_win.domain import (
    AsrErrorCode,
    AsrRequest,
    CanonicalAudio,
    InvalidInputError,
    Timestamp,
    TranscriptResult,
    TranscriptSegment,
    WordTimestamp,
)


def test_canonical_audio_owns_pcm_and_exposes_deterministic_metadata() -> None:
    source = bytearray(b"\x01\x00\x02\x00")

    audio = CanonicalAudio(source)
    source[0] = 255

    assert audio.pcm16le == b"\x01\x00\x02\x00"
    assert audio.sample_count == 2
    assert audio.byte_length == 4
    assert audio.duration == pytest.approx(2 / 16_000)


@pytest.mark.parametrize(
    ("data", "sample_rate", "channels"),
    [
        (b"", 16_000, 1),
        (b"\x00", 16_000, 1),
        (b"\x00\x00", 8_000, 1),
        (b"\x00\x00", 16_000, 2),
    ],
)
def test_canonical_audio_rejects_non_canonical_input(
    data: bytes, sample_rate: int, channels: int
) -> None:
    with pytest.raises(InvalidInputError):
        CanonicalAudio(data, sample_rate=sample_rate, channels=channels)


def test_transcript_result_preserves_timestamped_segments() -> None:
    result = TranscriptResult(
        text="hello",
        duration=1.0,
        segments=(TranscriptSegment("hello", Timestamp(0.0, 1.0)),),
    )

    assert result.segments[0].timestamp.start == 0.0
    assert result.segments[0].timestamp.end == 1.0


def test_request_rejects_empty_request_id_with_typed_error() -> None:
    with pytest.raises(InvalidInputError) as error:
        AsrRequest(CanonicalAudio(b"\x00\x00"), request_id=" ")

    assert error.value.code is AsrErrorCode.INVALID_INPUT


def test_canonical_audio_rejects_bytes_above_configured_limit(monkeypatch) -> None:
    import voiceink_win.domain.models as models

    monkeypatch.setattr(models, "MAX_CANONICAL_AUDIO_BYTES", 2)

    with pytest.raises(InvalidInputError):
        CanonicalAudio(b"\x00\x00\x00\x00")


def test_empty_transcript_is_valid_only_without_segments() -> None:
    assert TranscriptResult(text="", duration=1.0).text == ""

    with pytest.raises(InvalidInputError):
        TranscriptResult(
            text="",
            duration=1.0,
            segments=(TranscriptSegment("silence", Timestamp(0.0, 1.0)),),
        )


def test_transcript_result_rejects_out_of_order_nested_timestamps() -> None:
    with pytest.raises(InvalidInputError):
        TranscriptResult(
            text="hello world",
            duration=1.0,
            segments=(
                TranscriptSegment(
                    "hello world",
                    Timestamp(0.0, 1.0),
                    words=(
                        WordTimestamp("world", Timestamp(0.6, 0.8)),
                        WordTimestamp("hello", Timestamp(0.1, 0.5)),
                    ),
                ),
            ),
        )

    with pytest.raises(InvalidInputError):
        TranscriptResult(
            text="late",
            duration=1.0,
            segments=(TranscriptSegment("late", Timestamp(0.5, 1.1)),),
        )
