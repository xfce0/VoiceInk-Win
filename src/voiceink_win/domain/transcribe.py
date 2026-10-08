"""Platform-independent values for the imported transcription page."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .imported_errors import ErrorCode
from .imported_models import Attempt, JobId, Stage, TerminalResult


class MediaFormat(StrEnum):
    WAV = "WAV"
    MP3 = "MP3"
    M4A = "M4A"
    AIFF = "AIFF"
    MP4 = "MP4"
    MOV = "MOV"
    AAC = "AAC"
    FLAC = "FLAC"
    CAF = "CAF"
    AMR = "AMR"
    OGG = "OGG"
    OPUS = "OPUS"
    THREE_GP = "3GP"
    WEBM = "WEBM"


SUPPORTED_MEDIA_FORMATS = tuple(MediaFormat)
SUPPORTED_MEDIA_EXTENSIONS = frozenset(label.value.lower() for label in SUPPORTED_MEDIA_FORMATS)


def media_format_hint(path: str | Path) -> MediaFormat | None:
    extension = Path(path).suffix.removeprefix(".").lower()
    try:
        return MediaFormat(extension.upper())
    except ValueError:
        return None


class QueueState(StrEnum):
    PENDING = "pending"
    VALIDATING = "validating"
    QUEUED = "queued"
    NORMALIZING = "normalizing"
    TRANSCRIBING = "transcribing"
    RETRY_WAITING = "retry_waiting"
    CLEANING_UP = "cleaning_up"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class TranscriptVariant(StrEnum):
    ORIGINAL = "original"
    ENHANCED = "enhanced"


def _clean_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True, slots=True)
class TranscriptDocument:
    source_name: str
    created_at: str
    duration_seconds: float
    original_text: str
    enhanced_text: str | None = None
    selected_variant: TranscriptVariant = TranscriptVariant.ORIGINAL

    def __post_init__(self) -> None:
        source_name = Path(self.source_name).name.strip()
        original = _clean_text(self.original_text).strip()
        enhanced = _clean_text(self.enhanced_text).strip() if self.enhanced_text else None
        if not source_name or not original or not self.created_at:
            raise ValueError("transcript document requires source, date, and original text")
        if not math.isfinite(self.duration_seconds) or self.duration_seconds < 0:
            raise ValueError("transcript duration must be finite and non-negative")
        if not isinstance(self.selected_variant, TranscriptVariant):
            raise ValueError("selected variant must be a TranscriptVariant")
        object.__setattr__(self, "source_name", source_name)
        object.__setattr__(self, "original_text", original)
        object.__setattr__(self, "enhanced_text", enhanced)

    def text_for(self, variant: TranscriptVariant | None = None) -> str:
        selected = resolve_variant(self, variant)
        if selected is TranscriptVariant.ENHANCED and self.enhanced_text:
            return self.enhanced_text
        return self.original_text

    @property
    def has_enhanced(self) -> bool:
        return bool(self.enhanced_text)


def resolve_variant(
    document: TranscriptDocument,
    requested: TranscriptVariant | None = None,
    visible: TranscriptVariant | None = None,
) -> TranscriptVariant:
    candidate = requested or visible
    if candidate is None:
        candidate = (
            TranscriptVariant.ENHANCED if document.enhanced_text else TranscriptVariant.ORIGINAL
        )
    if candidate is TranscriptVariant.ENHANCED and not document.enhanced_text:
        return TranscriptVariant.ORIGINAL
    return candidate


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    stage: Stage
    fraction: float | None = None
    message: str = ""

    def __post_init__(self) -> None:
        if self.fraction is not None:
            if not math.isfinite(self.fraction):
                raise ValueError("progress fraction must be finite")
            object.__setattr__(self, "fraction", max(0.0, min(1.0, self.fraction)))


@dataclass(frozen=True, slots=True)
class ImportObservation:
    job_id: JobId
    attempt: Attempt
    progress: ProgressSnapshot
    terminal: TerminalResult | None = None


@dataclass(frozen=True, slots=True)
class EndOfStream:
    """Marker returned after an observation subscription is closed or consumed."""


@dataclass(frozen=True, slots=True)
class ImportFailure:
    code: ErrorCode
    message: str
    stage: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class QueueAggregate:
    total: int
    pending: int
    active: int
    succeeded: int
    failed: int
    cancelled: int
    rejected: int


@dataclass(frozen=True, slots=True)
class TranscriptionQueueItemSnapshot:
    item_id: str
    job_id: JobId | None
    attempt: int
    source_name: str
    source_format_hint: MediaFormat | None
    state: QueueState
    progress: ProgressSnapshot
    result: TranscriptDocument | None = None
    failure: ImportFailure | None = None
    can_remove: bool = False
    can_cancel: bool = False
    can_retry: bool = False

    @property
    def selected_variant(self) -> TranscriptVariant:
        return self.result.selected_variant if self.result else TranscriptVariant.ORIGINAL


@dataclass(frozen=True, slots=True)
class TranscribePageSnapshot:
    items: tuple[TranscriptionQueueItemSnapshot, ...] = ()
    selected_mode_id: str | None = None
    is_processing: bool = False
    can_start: bool = False
    accepting_files: bool = True
    aggregate: QueueAggregate = QueueAggregate(0, 0, 0, 0, 0, 0, 0)
    page_error: str | None = None


def safe_basename(path: str | Path) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", Path(path).name).strip()
    return name[:255] or "untitled-media"
