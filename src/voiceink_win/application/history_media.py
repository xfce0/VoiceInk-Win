"""Application orchestration for retained history media actions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from voiceink_win.domain import HistoryAudioArtifactPort, HistoryRecord
from voiceink_win.domain.persistence import InvalidAudioArtifactPathError

logger = logging.getLogger(__name__)


class HistoryMediaState(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class HistoryMediaCode(StrEnum):
    NO_ARTIFACT = "NO_ARTIFACT"
    ARTIFACT_MISSING_OR_INVALID = "ARTIFACT_MISSING_OR_INVALID"
    PLATFORM_UNAVAILABLE = "PLATFORM_UNAVAILABLE"
    STARTED = "STARTED"
    OPERATION_FAILED = "OPERATION_FAILED"


@dataclass(frozen=True, slots=True)
class HistoryMediaCapability:
    state: HistoryMediaState
    code: HistoryMediaCode | None = None

    @property
    def available(self) -> bool:
        return self.state is HistoryMediaState.AVAILABLE


@dataclass(frozen=True, slots=True)
class HistoryMediaAvailability:
    audio: HistoryMediaCapability
    reveal: HistoryMediaCapability


@dataclass(frozen=True, slots=True)
class HistoryMediaActionResult:
    code: HistoryMediaCode

    @property
    def state(self) -> HistoryMediaState:
        if self.code is HistoryMediaCode.STARTED:
            return HistoryMediaState.AVAILABLE
        if self.code is HistoryMediaCode.OPERATION_FAILED:
            return HistoryMediaState.ERROR
        return HistoryMediaState.UNAVAILABLE


class HistoryAudioPlaybackPort(Protocol):
    def is_available(self) -> bool: ...

    def play(self, resolved_artifact: Path) -> None: ...


class HistoryAudioPlaybackEventsPort(Protocol):
    def set_failure_callback(self, callback: Callable[[Exception], None] | None) -> None: ...


class HistoryArtifactRevealPort(Protocol):
    def is_available(self) -> bool: ...

    def reveal(self, resolved_artifact: Path) -> None: ...


class HistoryMediaActionService:
    """Resolve a retained artifact and delegate one media action safely."""

    def __init__(
        self,
        artifacts: HistoryAudioArtifactPort,
        audio: HistoryAudioPlaybackPort | None = None,
        reveal: HistoryArtifactRevealPort | None = None,
    ) -> None:
        self._artifacts = artifacts
        self._audio = audio
        self._reveal = reveal

    def inspect(self, record: HistoryRecord) -> HistoryMediaAvailability:
        artifact, missing = self._resolve(record)
        if missing is not None:
            state = (
                HistoryMediaState.ERROR
                if missing is HistoryMediaCode.OPERATION_FAILED
                else HistoryMediaState.UNAVAILABLE
            )
            capability = HistoryMediaCapability(state, missing)
            return HistoryMediaAvailability(capability, capability)
        return HistoryMediaAvailability(
            audio=self._capability(record, self._audio, artifact, "audio"),
            reveal=self._capability(record, self._reveal, artifact, "reveal"),
        )

    def play(
        self,
        record: HistoryRecord,
        on_result: Callable[[HistoryMediaActionResult], None] | None = None,
    ) -> HistoryMediaActionResult:
        return self._run(record, self._audio, "audio", on_result)

    def reveal(self, record: HistoryRecord) -> HistoryMediaActionResult:
        return self._run(record, self._reveal, "reveal")

    def close(self) -> None:
        for port in (self._audio, self._reveal):
            close = getattr(port, "close", None)
            if close is not None:
                close()

    def _run(
        self,
        record: HistoryRecord,
        port: HistoryAudioPlaybackPort | HistoryArtifactRevealPort | None,
        action: str,
        on_result: Callable[[HistoryMediaActionResult], None] | None = None,
    ) -> HistoryMediaActionResult:
        artifact, missing = self._resolve(record)
        if missing is not None:
            return HistoryMediaActionResult(missing)
        if port is None:
            return self._unavailable(record, action, HistoryMediaCode.PLATFORM_UNAVAILABLE)
        try:
            if not port.is_available():
                return self._unavailable(record, action, HistoryMediaCode.PLATFORM_UNAVAILABLE)
        except Exception as error:
            self._log(
                record,
                action,
                HistoryMediaCode.PLATFORM_UNAVAILABLE,
                logging.INFO,
                failure_stage="capability",
                exception_type=type(error).__name__,
            )
            return HistoryMediaActionResult(HistoryMediaCode.PLATFORM_UNAVAILABLE)
        try:
            if action == "audio":
                set_failure_callback = getattr(port, "set_failure_callback", None)
                if callable(set_failure_callback):
                    set_failure_callback(self._async_failure(record, action, on_result))
                port.play(artifact)
            else:
                port.reveal(artifact)
        except Exception as error:
            return self._failed(record, action, error)
        return HistoryMediaActionResult(HistoryMediaCode.STARTED)

    def _async_failure(
        self,
        record: HistoryRecord,
        action: str,
        on_result: Callable[[HistoryMediaActionResult], None] | None,
    ) -> Callable[[Exception], None]:
        def report(error: Exception) -> None:
            result = self._failed(record, action, error)
            if on_result is not None:
                on_result(result)

        return report

    def _resolve(self, record: HistoryRecord) -> tuple[Path | None, HistoryMediaCode | None]:
        reference = record.audio_artifact_path
        if not isinstance(reference, str) or not reference.strip():
            self._log(record, "resolve", HistoryMediaCode.NO_ARTIFACT, logging.INFO)
            return None, HistoryMediaCode.NO_ARTIFACT
        try:
            return self._artifacts.reveal_path(reference), None
        except (FileNotFoundError, InvalidAudioArtifactPathError) as error:
            self._log(
                record,
                "resolve",
                HistoryMediaCode.ARTIFACT_MISSING_OR_INVALID,
                logging.INFO,
                failure_stage="resolve",
                exception_type=type(error).__name__,
            )
            return None, HistoryMediaCode.ARTIFACT_MISSING_OR_INVALID
        except Exception as error:
            self._log(
                record,
                "resolve",
                HistoryMediaCode.ARTIFACT_MISSING_OR_INVALID,
                logging.WARNING,
                failure_stage="resolve",
                exception_type=type(error).__name__,
            )
            return None, HistoryMediaCode.ARTIFACT_MISSING_OR_INVALID

    def _capability(
        self,
        record: HistoryRecord,
        port: HistoryAudioPlaybackPort | HistoryArtifactRevealPort | None,
        artifact: Path | None,
        action: str,
    ) -> HistoryMediaCapability:
        if port is None or artifact is None:
            return HistoryMediaCapability(
                HistoryMediaState.UNAVAILABLE, HistoryMediaCode.PLATFORM_UNAVAILABLE
            )
        try:
            available = port.is_available()
        except Exception as error:
            self._log(
                record,
                action,
                HistoryMediaCode.PLATFORM_UNAVAILABLE,
                logging.INFO,
                failure_stage="capability",
                exception_type=type(error).__name__,
            )
            return HistoryMediaCapability(
                HistoryMediaState.UNAVAILABLE, HistoryMediaCode.PLATFORM_UNAVAILABLE
            )
        return (
            HistoryMediaCapability(HistoryMediaState.AVAILABLE)
            if available
            else HistoryMediaCapability(
                HistoryMediaState.UNAVAILABLE, HistoryMediaCode.PLATFORM_UNAVAILABLE
            )
        )

    def _unavailable(
        self, record: HistoryRecord, action: str, code: HistoryMediaCode
    ) -> HistoryMediaActionResult:
        self._log(record, action, code, logging.INFO)
        return HistoryMediaActionResult(code)

    def _failed(
        self, record: HistoryRecord, action: str, error: Exception
    ) -> HistoryMediaActionResult:
        code = HistoryMediaCode.OPERATION_FAILED
        self._log(
            record,
            action,
            code,
            logging.WARNING,
            failure_stage="operation",
            exception_type=type(error).__name__,
        )
        return HistoryMediaActionResult(code)

    @staticmethod
    def _log(
        record: HistoryRecord,
        action: str,
        code: HistoryMediaCode,
        level: int,
        *,
        failure_stage: str | None = None,
        exception_type: str | None = None,
    ) -> None:
        details = {"history_id": record.id, "action": action, "reason_code": code.value}
        if failure_stage is not None:
            details["failure_stage"] = failure_stage
        if exception_type is not None:
            details["exception_type"] = exception_type
        logger.log(
            level,
            "history media outcome",
            extra=details,
        )


__all__ = [
    "HistoryArtifactRevealPort",
    "HistoryAudioPlaybackPort",
    "HistoryAudioPlaybackEventsPort",
    "HistoryMediaActionResult",
    "HistoryMediaActionService",
    "HistoryMediaAvailability",
    "HistoryMediaCapability",
    "HistoryMediaCode",
    "HistoryMediaState",
]
