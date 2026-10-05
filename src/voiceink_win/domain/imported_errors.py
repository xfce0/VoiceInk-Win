"""Typed failures for imported-media admission and processing."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar

from .errors import QueueFullError as AsrQueueFullError


class ErrorCode(StrEnum):
    SOURCE_CHANGED = "SourceChanged"
    RESOURCE_LIMIT_EXCEEDED = "ResourceLimitExceeded"
    NORMALIZATION_FAILED = "NormalizationFailed"
    NO_AUDIO_STREAM = "NoAudioStream"
    UNSUPPORTED_MEDIA = "UnsupportedMedia"
    RUNTIME_UNAVAILABLE = "RuntimeUnavailable"
    RUNTIME_TIMEOUT = "RuntimeTimeout"
    DEADLINE_EXCEEDED = "DeadlineExceeded"
    RUNTIME_PROTOCOL_FAILURE = "RuntimeProtocolFailure"
    TRANSCRIPTION_FAILED = "TranscriptionFailed"
    CANCELLED = "Cancelled"
    CLEANUP_WARNING = "CleanupWarning"
    INVALID_SOURCE = "InvalidSource"
    QUEUE_FULL = "QueueFull"
    MALFORMED_WAV = "MalformedWav"


SAFE_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.SOURCE_CHANGED: "The source changed before processing.",
    ErrorCode.RESOURCE_LIMIT_EXCEEDED: "The media exceeds a configured resource limit.",
    ErrorCode.NORMALIZATION_FAILED: "The media could not be normalized.",
    ErrorCode.NO_AUDIO_STREAM: "The media does not contain an audio stream.",
    ErrorCode.UNSUPPORTED_MEDIA: "The media format is not supported.",
    ErrorCode.RUNTIME_UNAVAILABLE: "The transcription runtime is unavailable.",
    ErrorCode.RUNTIME_TIMEOUT: "Transcription timed out.",
    ErrorCode.DEADLINE_EXCEEDED: "The processing deadline was exceeded.",
    ErrorCode.RUNTIME_PROTOCOL_FAILURE: "The transcription runtime returned invalid data.",
    ErrorCode.TRANSCRIPTION_FAILED: "Transcription failed.",
    ErrorCode.CANCELLED: "Processing was cancelled.",
    ErrorCode.CLEANUP_WARNING: "Temporary processing data could not be fully removed.",
    ErrorCode.INVALID_SOURCE: "The source is not a permitted local file.",
    ErrorCode.QUEUE_FULL: "The import queue is full.",
    ErrorCode.MALFORMED_WAV: "The normalizer returned invalid WAV data.",
}


def safe_message(code: ErrorCode) -> str:
    return SAFE_MESSAGES[code]


class WarningCode(StrEnum):
    CLEANUP_WARNING = "CleanupWarning"


class CancellationReason(StrEnum):
    USER = "user"
    DEADLINE = "deadline"
    SHUTDOWN = "shutdown"


class ImportedProcessingError(Exception):
    """A safe, classified error from an admitted job."""

    code: ClassVar[ErrorCode] = ErrorCode.NORMALIZATION_FAILED
    retryable: ClassVar[bool] = False
    stage: ClassVar[str] = "normalization"

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause


class SourceChangedError(ImportedProcessingError):
    code = ErrorCode.SOURCE_CHANGED


class ResourceLimitExceededError(ImportedProcessingError):
    code = ErrorCode.RESOURCE_LIMIT_EXCEEDED


class NormalizationFailedError(ImportedProcessingError):
    code = ErrorCode.NORMALIZATION_FAILED


class NoAudioStreamError(ImportedProcessingError):
    code = ErrorCode.NO_AUDIO_STREAM


class UnsupportedMediaError(ImportedProcessingError):
    code = ErrorCode.UNSUPPORTED_MEDIA


class MalformedWavError(ImportedProcessingError):
    code = ErrorCode.MALFORMED_WAV


class ImportedRuntimeUnavailableError(ImportedProcessingError):
    code = ErrorCode.RUNTIME_UNAVAILABLE
    retryable = True
    stage = "transcription"


class ImportedRuntimeTimeoutError(ImportedProcessingError):
    code = ErrorCode.RUNTIME_TIMEOUT
    retryable = True
    stage = "transcription"


class ImportedDeadlineExceededError(ImportedProcessingError):
    code = ErrorCode.DEADLINE_EXCEEDED


class StageTimeoutError(ImportedProcessingError):
    code = ErrorCode.DEADLINE_EXCEEDED


class RuntimeProtocolFailureError(ImportedProcessingError):
    code = ErrorCode.RUNTIME_PROTOCOL_FAILURE
    stage = "transcription"


class TranscriptionFailedError(ImportedProcessingError):
    code = ErrorCode.TRANSCRIPTION_FAILED
    stage = "transcription"


class CleanupWarningError(ImportedProcessingError):
    code = ErrorCode.CLEANUP_WARNING
    stage = "cleanup"


class ImportShutdownError(RuntimeError):
    """The import service could not finish shutdown within its caller budget."""


class ImportRecoveryPendingError(ImportShutdownError):
    """Shutdown returned while independently recoverable resources remain."""


class RejectedRequestError(Exception):
    """A pre-admission rejection; it never owns a job or a workspace."""

    code: ClassVar[ErrorCode] = ErrorCode.INVALID_SOURCE

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause


class InvalidSourceError(RejectedRequestError):
    code = ErrorCode.INVALID_SOURCE


class QueueFullRejectedError(RejectedRequestError, AsrQueueFullError):
    code = ErrorCode.QUEUE_FULL


class ResourceLimitRejectedError(RejectedRequestError):
    code = ErrorCode.RESOURCE_LIMIT_EXCEEDED


RejectedRequest = InvalidSourceError | QueueFullRejectedError | ResourceLimitRejectedError
