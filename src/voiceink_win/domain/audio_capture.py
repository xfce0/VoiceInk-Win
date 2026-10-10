"""Platform-independent microphone capture values and bounded PCM assembly."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from .models import MAX_CANONICAL_AUDIO_BYTES, CanonicalAudio, TranscriptResult

CANONICAL_SAMPLE_RATE = 16_000
CANONICAL_CHANNELS = 1
DEFAULT_CAPTURE_SECONDS = 32 * 60
DEFAULT_CAPTURE_SAMPLES = DEFAULT_CAPTURE_SECONDS * CANONICAL_SAMPLE_RATE
DEFAULT_CAPTURE_BYTES = DEFAULT_CAPTURE_SAMPLES * 2
DEFAULT_CAPTURE_CHUNK_BYTES = 32 * 1024


class MicrophoneAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class CaptureErrorCode(StrEnum):
    UNAVAILABLE = "unavailable"
    BUSY = "busy"
    DEVICE_UNAVAILABLE = "device_unavailable"
    INVALID_CHUNK = "invalid_chunk"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    FAILED = "failed"


class RecordingState(StrEnum):
    SUCCEEDED = "succeeded"
    EMPTY = "empty"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class CaptureError(Exception):
    """Typed microphone failure that must not become empty success."""

    def __init__(self, code: CaptureErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MicrophoneUnavailableError(CaptureError):
    def __init__(self, message: str) -> None:
        super().__init__(CaptureErrorCode.UNAVAILABLE, message)


class CaptureLimitError(CaptureError):
    def __init__(self, message: str) -> None:
        super().__init__(CaptureErrorCode.RESOURCE_LIMIT_EXCEEDED, message)


class InvalidCaptureChunkError(CaptureError):
    def __init__(self, message: str) -> None:
        super().__init__(CaptureErrorCode.INVALID_CHUNK, message)


class CaptureBusyError(CaptureError):
    def __init__(self, message: str = "microphone recording is busy") -> None:
        super().__init__(CaptureErrorCode.BUSY, message)


class CaptureTimeoutError(CaptureError):
    def __init__(self, message: str) -> None:
        super().__init__(CaptureErrorCode.TIMEOUT, message)


class CaptureCancelledError(CaptureError):
    def __init__(self, message: str = "microphone capture was cancelled") -> None:
        super().__init__(CaptureErrorCode.CANCELLED, message)


@dataclass(frozen=True, slots=True)
class CaptureLimits:
    """Approved-at-runtime bounds for the internal canonical capture buffer."""

    max_duration_seconds: float = DEFAULT_CAPTURE_SECONDS
    max_samples: int = DEFAULT_CAPTURE_SAMPLES
    max_bytes: int = DEFAULT_CAPTURE_BYTES
    max_chunk_bytes: int = DEFAULT_CAPTURE_CHUNK_BYTES

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.max_duration_seconds)
            or self.max_duration_seconds <= 0
            or self.max_samples <= 0
            or self.max_bytes <= 0
            or self.max_chunk_bytes <= 0
        ):
            raise ValueError("capture limits must be positive")
        if self.max_bytes % 2 or self.max_bytes > MAX_CANONICAL_AUDIO_BYTES:
            raise ValueError("capture byte limit must be an even canonical-sized value")
        if self.max_bytes != self.max_samples * 2:
            raise ValueError("capture sample and byte limits must agree")


@dataclass(frozen=True, slots=True)
class InputDevice:
    """Safe device metadata; native endpoint IDs never cross this boundary."""

    selection_token: str
    display_name: str
    is_default_console: bool
    channels: int
    sample_rates: tuple[int, ...]
    sample_types: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.selection_token.strip():
            raise ValueError("selection token must not be empty")
        if not self.display_name.strip() or len(self.display_name) > 128:
            raise ValueError("device display name must contain at most 128 characters")
        if self.channels not in (1, 2) or not self.sample_rates or not self.sample_types:
            raise ValueError("device format metadata is invalid")
        if not all(isinstance(rate, int) and rate > 0 for rate in self.sample_rates):
            raise ValueError("device sample rates must be positive integers")


@dataclass(frozen=True, slots=True)
class MicrophoneStatus:
    availability: MicrophoneAvailability
    message: str


DEFAULT_CAPTURE_LIMITS = CaptureLimits()


@dataclass(frozen=True, slots=True)
class RecordingResult:
    state: RecordingState
    audio: CanonicalAudio | None = None
    transcript: TranscriptResult | None = None
    error: Exception | None = None


class BoundedPcm16Capture:
    """Accumulate canonical PCM16 without exceeding the capture budget."""

    def __init__(self, limits: CaptureLimits | None = None) -> None:
        self._limits = limits or DEFAULT_CAPTURE_LIMITS
        self._pcm16le = bytearray()

    @property
    def byte_length(self) -> int:
        return len(self._pcm16le)

    def append(self, chunk: bytes | bytearray | memoryview) -> None:
        if not isinstance(chunk, bytes | bytearray | memoryview):
            raise InvalidCaptureChunkError("capture chunk must be bytes-like PCM16 data")
        owned = bytes(chunk)
        if not owned or len(owned) % 2:
            raise InvalidCaptureChunkError("capture chunk must contain complete PCM16 samples")
        if len(owned) > self._limits.max_chunk_bytes:
            raise CaptureLimitError("capture chunk exceeds the bounded transport size")
        new_length = len(self._pcm16le) + len(owned)
        if new_length > self._limits.max_bytes:
            raise CaptureLimitError("capture exceeds the bounded PCM16 byte limit")
        if new_length // 2 > self._limits.max_samples:
            raise CaptureLimitError("capture exceeds the bounded PCM16 sample limit")
        self._pcm16le.extend(owned)

    def finish(self) -> CanonicalAudio | None:
        if not self._pcm16le:
            return None
        return CanonicalAudio(bytes(self._pcm16le))
