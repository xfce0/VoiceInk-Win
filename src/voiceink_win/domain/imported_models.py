"""Immutable domain values for imported-media transcription."""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .errors import InvalidInputError
from .imported_errors import CancellationReason, ErrorCode, WarningCode, safe_message
from .models import CanonicalAudio, TranscriptResult


def _sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdefABCDEF" for char in value)


class Stage(StrEnum):
    NORMALIZATION = "normalization"
    TRANSCRIPTION = "transcription"
    CLEANUP = "cleanup"
    ACCEPTED = "accepted"
    QUEUED = "queued"
    NORMALIZING = "normalizing"
    TRANSCRIBING = "transcribing"
    RETRY_WAITING = "retry_waiting"
    CLEANING_UP = "cleaning_up"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class JobId:
    value: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, str)
            or not self.value.strip()
            or self.value in {".", ".."}
            or any(separator in self.value for separator in ("/", "\\"))
        ):
            raise InvalidInputError("job ID must not be empty")

    @classmethod
    def new(cls) -> JobId:
        return cls(str(uuid.uuid4()))

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class Attempt:
    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 1:
            raise InvalidInputError("attempt must be a positive integer")

    def next(self) -> Attempt:
        return Attempt(self.value + 1)


@dataclass(frozen=True, slots=True)
class ImportOptions:
    language: str | None = None
    include_timestamps: bool = False

    def __post_init__(self) -> None:
        if self.language is not None and not self.language.strip():
            raise InvalidInputError("language must be non-empty or None")
        if not isinstance(self.include_timestamps, bool):
            raise InvalidInputError("include_timestamps must be boolean")


@dataclass(frozen=True, slots=True)
class SourceMedia:
    path: Path
    display_name: str = ""
    identity: str = ""
    size: int = -1
    sha256: str = ""
    admission_handle: object | None = None

    def __post_init__(self) -> None:
        path = Path(self.path)
        name = Path(self.display_name or path.name).name
        name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:255]
        if not name:
            raise InvalidInputError("source display name must not be empty")
        if self.size < -1 or self.sha256 and not _sha256(self.sha256):
            raise InvalidInputError("source descriptor is invalid")
        if bool(self.identity) != (self.size >= 0 and bool(self.sha256)):
            raise InvalidInputError("source identity, size, and hash must be provided together")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "display_name", name)


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    snapshot_identity: str
    snapshot_size: int
    snapshot_sha256: str
    job_identity: str = ""
    attempt_identity: str = ""

    def __post_init__(self) -> None:
        if (
            self.snapshot_size < 0
            or not self.snapshot_identity
            or not _sha256(self.snapshot_sha256)
        ):
            raise InvalidInputError("invalid snapshot manifest")


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    source: SourceMedia
    path: Path
    manifest: SnapshotManifest
    source_identity: str = ""
    source_size: int = -1
    source_sha256: str = ""
    verified_input: VerifiedMediaHandle | None = None
    descriptor: int | None = None
    workspace_leases: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        if self.source_size < 0 or not self.source_identity or not _sha256(self.source_sha256):
            raise InvalidInputError("snapshot source descriptor is invalid")
        if self.verified_input is None and self.descriptor is not None:
            raise InvalidInputError("verified snapshot descriptor requires an input path")
        if self.verified_input is not None and self.verified_input.descriptor != self.descriptor:
            raise InvalidInputError("verified snapshot handle does not match its descriptor")
        if not isinstance(self.workspace_leases, tuple):
            raise InvalidInputError("workspace leases must be a tuple")

    @property
    def is_verified(self) -> bool:
        return self.verified_input is not None


@dataclass(frozen=True, slots=True)
class JobWorkspace:
    path: Path
    job_id: JobId
    attempt: Attempt
    job_identity: str = ""
    attempt_identity: str = ""


@dataclass(frozen=True, slots=True)
class VerifiedMediaHandle:
    input_path: str
    descriptor: int | None
    identity: str
    size: int
    sha256: str
    lease: object | None = None
    revalidate: object | None = None

    def __post_init__(self) -> None:
        if not self.input_path or not self.identity or self.size < 0 or not _sha256(self.sha256):
            raise InvalidInputError("verified media handle is invalid")


@dataclass(frozen=True, slots=True)
class NormalizedAudio:
    audio: CanonicalAudio

    @property
    def sample_count(self) -> int:
        return self.audio.sample_count

    @property
    def duration(self) -> float:
        return self.audio.duration


@dataclass(frozen=True, slots=True)
class RuntimeDiagnostics:
    runtime_version: str | None = None
    model_revision: str | None = None
    backend: str | None = None
    hardware: str | None = None
    request_id: str | None = None
    failure_code: ErrorCode | None = None
    ffmpeg_version: str | None = None
    ffmpeg_sha256: str | None = None
    ffmpeg_license: str | None = None
    ffmpeg_provenance_url: str | None = None


@dataclass(frozen=True, slots=True)
class StageTiming:
    stage: Stage
    elapsed_seconds: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.elapsed_seconds) or self.elapsed_seconds < 0:
            raise InvalidInputError("stage timing must be finite and non-negative")
        if self.stage not in {Stage.NORMALIZING, Stage.TRANSCRIBING, Stage.CLEANING_UP}:
            raise InvalidInputError("stage timing contains a non-processing stage")


@dataclass(frozen=True, slots=True)
class ProcessingMetadata:
    normalized_duration: float | None
    attempt_count: int
    stage_timings: tuple[StageTiming, ...]
    total_duration: float
    terminal_status: Stage

    def __post_init__(self) -> None:
        if self.normalized_duration is not None and (
            not math.isfinite(self.normalized_duration) or self.normalized_duration < 0
        ):
            raise InvalidInputError("normalized duration must not be negative")
        if (
            isinstance(self.attempt_count, bool)
            or self.attempt_count < 1
            or not math.isfinite(self.total_duration)
            or self.total_duration < 0
        ):
            raise InvalidInputError("processing metadata contains invalid duration/count")
        if self.terminal_status is not Stage.SUCCEEDED:
            raise InvalidInputError("success metadata must have succeeded terminal status")


@dataclass(frozen=True, slots=True)
class ImportedTranscriptionResult:
    job_id: JobId
    source_name: str
    transcription: TranscriptResult
    processing: ProcessingMetadata
    diagnostics: RuntimeDiagnostics


@dataclass(frozen=True, slots=True)
class Success:
    status: str
    job_id: JobId
    attempt: Attempt
    transcription: ImportedTranscriptionResult
    warnings: tuple[WarningCode, ...] = ()

    def __post_init__(self) -> None:
        if self.status != "succeeded":
            raise InvalidInputError("success status must be succeeded")
        if not isinstance(self.warnings, tuple) or not all(
            isinstance(warning, WarningCode) for warning in self.warnings
        ):
            raise InvalidInputError("success warnings must be typed warning codes")


@dataclass(frozen=True, slots=True)
class Failed:
    status: str
    job_id: JobId
    attempt: Attempt
    stage: Stage
    code: ErrorCode
    safe_message: str
    retryable: bool
    warnings: tuple[WarningCode, ...] = ()

    def __post_init__(self) -> None:
        if self.status != "failed" or self.stage not in {
            Stage.NORMALIZATION,
            Stage.TRANSCRIPTION,
            Stage.CLEANUP,
        }:
            raise InvalidInputError("invalid failed result")
        if self.safe_message != safe_message(self.code):
            raise InvalidInputError("failed result contains an unsafe message")
        if not isinstance(self.warnings, tuple) or not all(
            isinstance(warning, WarningCode) for warning in self.warnings
        ):
            raise InvalidInputError("failed warnings must be typed warning codes")


@dataclass(frozen=True, slots=True)
class Cancelled:
    status: str
    job_id: JobId
    attempt: Attempt
    stage: Stage
    reason: CancellationReason

    def __post_init__(self) -> None:
        if self.status != "cancelled":
            raise InvalidInputError("cancelled status must be cancelled")
        if self.stage not in {
            Stage.QUEUED,
            Stage.NORMALIZING,
            Stage.TRANSCRIBING,
            Stage.RETRY_WAITING,
            Stage.CLEANING_UP,
        }:
            raise InvalidInputError("invalid cancelled stage")


TerminalResult = Success | Failed | Cancelled
