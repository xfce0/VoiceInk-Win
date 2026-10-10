"""Application orchestration for retained history media actions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from voiceink_win.domain import HistoryAudioArtifactPort, HistoryRecord

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
            capability = HistoryMediaCapability(HistoryMediaState.UNAVAILABLE, missing)
            return HistoryMediaAvailability(capability, capability)
        return HistoryMediaAvailability(
            audio=self._capability(self._audio, artifact),
            reveal=self._capability(self._reveal, artifact),
        )

    def play(self, record: HistoryRecord) -> HistoryMediaActionResult:
        return self._run(record, self._audio, "audio")

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
    ) -> HistoryMediaActionResult:
        artifact, missing = self._resolve(record)
        if missing is not None:
            return HistoryMediaActionResult(missing)
        if port is None:
            return self._unavailable(record, action, HistoryMediaCode.PLATFORM_UNAVAILABLE)
        try:
            if not port.is_available():
                return self._unavailable(record, action, HistoryMediaCode.PLATFORM_UNAVAILABLE)
            if action == "audio":
                port.play(artifact)
            else:
                port.reveal(artifact)
        except Exception:
            return self._failed(record, action)
        return HistoryMediaActionResult(HistoryMediaCode.STARTED)

    def _resolve(self, record: HistoryRecord) -> tuple[Path | None, HistoryMediaCode | None]:
        reference = record.audio_artifact_path
        if not isinstance(reference, str) or not reference.strip():
            return None, HistoryMediaCode.NO_ARTIFACT
        try:
            return self._artifacts.reveal_path(reference), None
        except Exception:
            return None, HistoryMediaCode.ARTIFACT_MISSING_OR_INVALID

    def _capability(
        self,
        port: HistoryAudioPlaybackPort | HistoryArtifactRevealPort | None,
        artifact: Path | None,
    ) -> HistoryMediaCapability:
        if port is None or artifact is None:
            return HistoryMediaCapability(
                HistoryMediaState.UNAVAILABLE, HistoryMediaCode.PLATFORM_UNAVAILABLE
            )
        try:
            available = port.is_available()
        except Exception:
            return HistoryMediaCapability(
                HistoryMediaState.ERROR, HistoryMediaCode.OPERATION_FAILED
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
        logger.info(
            "history media action unavailable",
            extra={"history_id": record.id, "action": action, "reason_code": code.value},
        )
        return HistoryMediaActionResult(code)

    def _failed(self, record: HistoryRecord, action: str) -> HistoryMediaActionResult:
        code = HistoryMediaCode.OPERATION_FAILED
        logger.warning(
            "history media action failed",
            extra={"history_id": record.id, "action": action, "reason_code": code.value},
        )
        return HistoryMediaActionResult(code)


__all__ = [
    "HistoryArtifactRevealPort",
    "HistoryAudioPlaybackPort",
    "HistoryMediaActionResult",
    "HistoryMediaActionService",
    "HistoryMediaAvailability",
    "HistoryMediaCapability",
    "HistoryMediaCode",
    "HistoryMediaState",
]
