"""Platform-independent ASR values."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .cancellation import CancellationToken
from .errors import InvalidInputError

MAX_CANONICAL_AUDIO_BYTES = 64 * 1024 * 1024


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidInputError(f"{name} must be a number")
    converted = float(value)
    if not math.isfinite(converted):
        raise InvalidInputError(f"{name} must be finite")
    return converted


@dataclass(frozen=True, slots=True)
class CanonicalAudio:
    """An owned, contiguous mono 16 kHz signed PCM16 audio snapshot."""

    pcm16le: bytes
    sample_rate: int = 16_000
    channels: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.pcm16le, (bytes, bytearray, memoryview)):
            raise InvalidInputError("audio must be bytes-like PCM16 data")
        owned = bytes(self.pcm16le)
        if not owned:
            raise InvalidInputError("audio must not be empty")
        if len(owned) > MAX_CANONICAL_AUDIO_BYTES:
            raise InvalidInputError("audio exceeds the canonical byte limit")
        if self.sample_rate != 16_000:
            raise InvalidInputError("audio sample rate must be 16 kHz")
        if self.channels != 1:
            raise InvalidInputError("audio must be mono")
        if len(owned) % 2:
            raise InvalidInputError("PCM16 audio must contain complete samples")
        object.__setattr__(self, "pcm16le", owned)

    @property
    def sample_count(self) -> int:
        return len(self.pcm16le) // 2

    @property
    def byte_length(self) -> int:
        return len(self.pcm16le)

    @property
    def duration(self) -> float:
        return self.sample_count / self.sample_rate


@dataclass(frozen=True, slots=True)
class Timestamp:
    start: float
    end: float

    def __post_init__(self) -> None:
        start = _number(self.start, "timestamp start")
        end = _number(self.end, "timestamp end")
        if start < 0 or end < start:
            raise InvalidInputError("timestamp must satisfy 0 <= start <= end")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


@dataclass(frozen=True, slots=True)
class WordTimestamp:
    word: str
    timestamp: Timestamp

    def __post_init__(self) -> None:
        if not isinstance(self.word, str) or not self.word.strip():
            raise InvalidInputError("word timestamp must contain a word")


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    text: str
    timestamp: Timestamp
    words: tuple[WordTimestamp, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise InvalidInputError("transcript segment must contain text")
        if not isinstance(self.words, (tuple, list)):
            raise InvalidInputError("segment words must be a sequence")
        if not isinstance(self.words, tuple):
            object.__setattr__(self, "words", tuple(self.words))
        if not all(isinstance(word, WordTimestamp) for word in self.words):
            raise InvalidInputError("segment words must be WordTimestamp values")


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    """Transcript value; empty text is valid only for a timestamp-free silence result."""

    text: str
    duration: float
    segments: tuple[TranscriptSegment, ...] = ()
    detected_language: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise InvalidInputError("transcript text must be a string")
        duration = _number(self.duration, "transcript duration")
        if duration < 0:
            raise InvalidInputError("transcript duration must not be negative")
        if not isinstance(self.segments, (tuple, list)):
            raise InvalidInputError("transcript segments must be a sequence")
        if not isinstance(self.segments, tuple):
            object.__setattr__(self, "segments", tuple(self.segments))
        if not all(isinstance(segment, TranscriptSegment) for segment in self.segments):
            raise InvalidInputError("transcript segments must be TranscriptSegment values")
        if not self.text.strip() and self.segments:
            raise InvalidInputError("empty transcript must not contain segments")
        previous_segment_end = 0.0
        for segment in self.segments:
            if segment.timestamp.end > duration:
                raise InvalidInputError("transcript segment exceeds result duration")
            if segment.timestamp.start < previous_segment_end:
                raise InvalidInputError("transcript segments must be ordered")
            previous_word_end = segment.timestamp.start
            for word in segment.words:
                if word.timestamp.start < segment.timestamp.start:
                    raise InvalidInputError("word timestamp starts before its segment")
                if word.timestamp.end > segment.timestamp.end:
                    raise InvalidInputError("word timestamp exceeds its segment")
                if word.timestamp.start < previous_word_end:
                    raise InvalidInputError("word timestamps must be ordered")
                previous_word_end = word.timestamp.end
            previous_segment_end = segment.timestamp.end
        if self.detected_language is not None and not isinstance(self.detected_language, str):
            raise InvalidInputError("detected language must be a string or None")
        object.__setattr__(self, "duration", duration)


@dataclass(frozen=True, slots=True)
class AsrRequest:
    """A batch ASR request; deadline is an absolute ``time.monotonic()`` value."""

    audio: CanonicalAudio
    request_id: str
    language: str | None = None
    include_timestamps: bool = False
    deadline: float | None = None
    cancellation: CancellationToken | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.audio, CanonicalAudio):
            raise InvalidInputError("request audio must be CanonicalAudio")
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise InvalidInputError("request ID must not be empty")
        if self.language is not None and (
            not isinstance(self.language, str) or not self.language.strip()
        ):
            raise InvalidInputError("language must be a non-empty string or None")
        if not isinstance(self.include_timestamps, bool):
            raise InvalidInputError("include_timestamps must be boolean")
        if self.deadline is not None:
            deadline = _number(self.deadline, "request deadline")
            object.__setattr__(self, "deadline", deadline)


class HealthStatus(StrEnum):
    READY = "ready"
    STARTING = "starting"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    status: HealthStatus
    message: str = ""
    backend: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.status is HealthStatus.READY


@dataclass(frozen=True, slots=True)
class AsrCapabilities:
    model_id: str
    backends: tuple[str, ...]
    supports_timestamps: bool
    max_concurrency: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise InvalidInputError("runtime capabilities must identify a model and backend")
        if not isinstance(self.backends, (tuple, list)) or not self.backends:
            raise InvalidInputError("runtime capabilities must identify a model and backend")
        if not isinstance(self.backends, tuple):
            object.__setattr__(self, "backends", tuple(self.backends))
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or self.max_concurrency < 1
        ):
            raise InvalidInputError("runtime max_concurrency must be positive")
        if not all(isinstance(backend, str) and backend.strip() for backend in self.backends):
            raise InvalidInputError("runtime backends must be non-empty strings")
