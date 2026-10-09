"""Durable application values and ports for local persistence."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from .errors import InvalidInputError
from .transcribe import TranscriptVariant


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalise_datetime(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise InvalidInputError(f"{name} must be a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _json_mapping(value: Mapping[str, object], name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidInputError(f"{name} must be a mapping")
    copied = dict(value)
    if not all(isinstance(key, str) for key in copied):
        raise InvalidInputError(f"{name} keys must be strings")
    try:
        json.dumps(copied, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise InvalidInputError(f"{name} must contain JSON values") from error
    return copied


class TranscriptionSource(StrEnum):
    MICROPHONE = "microphone"
    IMPORTED_FILE = "imported_file"
    PASTE = "paste"


class HistoryStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class PersistenceError(RuntimeError):
    """Base error for the asynchronous persistence boundary."""


class PersistenceClosedError(PersistenceError):
    """An operation was submitted after the persistence worker started closing."""


class InvalidAudioArtifactPathError(PersistenceError, ValueError):
    """An audio reference is not a safe relative path."""


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    id: str = field(default_factory=lambda: uuid4().hex)
    source: TranscriptionSource | str = TranscriptionSource.MICROPHONE
    created_at: datetime = field(default_factory=_utc_now)
    duration: float = 0.0
    original_text: str = ""
    enhanced_text: str | None = None
    selected_variant: TranscriptVariant = TranscriptVariant.ORIGINAL
    status: HistoryStatus = HistoryStatus.COMPLETED
    error: str | None = None
    audio_artifact_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise InvalidInputError("history ID must not be empty")
        if not isinstance(self.source, str) or not self.source.strip():
            raise InvalidInputError("history source must not be empty")
        if isinstance(self.source, TranscriptionSource):
            object.__setattr__(self, "source", self.source.value)
        if not isinstance(self.original_text, str):
            raise InvalidInputError("original transcript must be a string")
        if self.enhanced_text is not None and not isinstance(self.enhanced_text, str):
            raise InvalidInputError("enhanced transcript must be a string or None")
        if isinstance(self.duration, bool) or not isinstance(self.duration, int | float):
            raise InvalidInputError("history duration must be a number")
        duration = float(self.duration)
        if not math.isfinite(duration) or duration < 0:
            raise InvalidInputError("history duration must be finite and non-negative")
        if not isinstance(self.selected_variant, TranscriptVariant):
            raise InvalidInputError("selected variant must be a TranscriptVariant")
        if not isinstance(self.status, HistoryStatus):
            raise InvalidInputError("history status must be a HistoryStatus")
        if self.error is not None and not isinstance(self.error, str):
            raise InvalidInputError("history error must be a string or None")
        if self.audio_artifact_path is not None and not isinstance(self.audio_artifact_path, str):
            raise InvalidInputError("audio artifact path must be a string or None")
        object.__setattr__(self, "created_at", _normalise_datetime(self.created_at, "created_at"))
        object.__setattr__(self, "duration", duration)


@dataclass(frozen=True, slots=True)
class HistoryPage:
    records: tuple[HistoryRecord, ...]
    offset: int
    limit: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class DictionaryEntry:
    id: str = field(default_factory=lambda: uuid4().hex)
    phrase: str = ""
    replacement: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise InvalidInputError("dictionary ID must not be empty")
        if not isinstance(self.phrase, str) or not self.phrase.strip():
            raise InvalidInputError("dictionary phrase must not be empty")
        if not isinstance(self.replacement, str):
            raise InvalidInputError("dictionary replacement must be a string")
        if not isinstance(self.enabled, bool):
            raise InvalidInputError("dictionary enabled must be boolean")
        object.__setattr__(self, "created_at", _normalise_datetime(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _normalise_datetime(self.updated_at, "updated_at"))


@dataclass(frozen=True, slots=True)
class Settings:
    language: str = "en"
    selected_mode: str = "default"
    hotkeys: Mapping[str, object] = field(default_factory=dict)
    auto_copy: bool = False
    model_preferences: Mapping[str, object] = field(default_factory=dict)
    audio_preferences: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.language, str) or not self.language.strip():
            raise InvalidInputError("settings language must not be empty")
        if not isinstance(self.selected_mode, str) or not self.selected_mode.strip():
            raise InvalidInputError("settings selected mode must not be empty")
        if not isinstance(self.auto_copy, bool):
            raise InvalidInputError("settings auto_copy must be boolean")
        object.__setattr__(self, "hotkeys", _json_mapping(self.hotkeys, "settings hotkeys"))
        object.__setattr__(
            self,
            "model_preferences",
            _json_mapping(self.model_preferences, "settings model preferences"),
        )
        object.__setattr__(
            self,
            "audio_preferences",
            _json_mapping(self.audio_preferences, "settings audio preferences"),
        )


class HistoryPort(Protocol):
    def upsert_history(self, record: HistoryRecord) -> Future[None]: ...

    def list_history(self, *, offset: int = 0, limit: int = 50) -> Future[HistoryPage]: ...

    def delete_history(self, record_id: str) -> Future[None]: ...


class DictionaryPort(Protocol):
    def upsert_dictionary(self, entry: DictionaryEntry) -> Future[None]: ...

    def list_dictionary(self) -> Future[tuple[DictionaryEntry, ...]]: ...

    def delete_dictionary(self, entry_id: str) -> Future[None]: ...


class SettingsPort(Protocol):
    def get_settings(self) -> Future[Settings | None]: ...

    def save_settings(self, settings: Settings) -> Future[None]: ...


class PersistencePort(HistoryPort, DictionaryPort, SettingsPort, Protocol):
    def ready(self) -> Future[None]: ...

    def close(self) -> Future[None]: ...
