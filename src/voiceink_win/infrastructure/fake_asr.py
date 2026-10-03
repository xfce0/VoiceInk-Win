"""Deterministic ASR runtime used by platform-independent tests."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import cast

from voiceink_win.domain import (
    AsrCapabilities,
    AsrRequest,
    AsrTimeoutError,
    BackendUnavailableError,
    CancellationError,
    ExecutionError,
    HealthStatus,
    InvalidInputError,
    MissingModelError,
    ProcessCrashedError,
    RuntimeHealth,
    RuntimeUnavailableError,
    Timestamp,
    TranscriptResult,
    TranscriptSegment,
)


class FakeAsrScenario(StrEnum):
    SUCCESS = "success"
    INVALID_INPUT = "invalid_input"
    UNAVAILABLE = "unavailable"
    MISSING_MODEL = "missing_model"
    TIMEOUT = "timeout"
    CANCELLATION = "cancellation"
    MALFORMED_RESULT = "malformed_result"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    CRASH = "crash"


class FakeAsrRuntime:
    def __init__(
        self,
        scenario: FakeAsrScenario = FakeAsrScenario.SUCCESS,
        *,
        backend: str = "cpu",
        max_concurrency: int = 1,
    ) -> None:
        self.scenario = scenario
        self.backend = backend
        self.max_concurrency = max_concurrency
        self._closed = False

    def capabilities(self) -> AsrCapabilities:
        return AsrCapabilities(
            model_id="fake-parakeet-tdt-v3",
            backends=(self.backend,),
            supports_timestamps=True,
            max_concurrency=self.max_concurrency,
        )

    def health(self) -> RuntimeHealth:
        if self._closed:
            return RuntimeHealth(HealthStatus.CLOSED, "fake runtime is closed", self.backend)
        if self.scenario is FakeAsrScenario.CRASH:
            return RuntimeHealth(HealthStatus.FAILED, "simulated process crash", self.backend)
        if self.scenario is not FakeAsrScenario.SUCCESS:
            return RuntimeHealth(HealthStatus.UNAVAILABLE, self.scenario.value, self.backend)
        return RuntimeHealth(HealthStatus.READY, "fake runtime ready", self.backend)

    def transcribe(self, request: AsrRequest) -> TranscriptResult:
        if self._closed:
            raise RuntimeUnavailableError("fake runtime is closed")
        if request.deadline is not None and time.monotonic() >= request.deadline:
            raise AsrTimeoutError("fake runtime observed deadline")
        if request.cancellation is not None and request.cancellation.is_cancelled():
            raise CancellationError("fake runtime observed cancellation")
        scenario = self.scenario
        if scenario is FakeAsrScenario.SUCCESS:
            segment = (
                (TranscriptSegment("fake transcript", Timestamp(0.0, request.audio.duration)),)
                if request.include_timestamps
                else ()
            )
            return TranscriptResult(
                text="fake transcript",
                duration=request.audio.duration,
                segments=segment,
                detected_language=request.language or "en",
            )
        if scenario is FakeAsrScenario.INVALID_INPUT:
            raise InvalidInputError("simulated invalid audio")
        if scenario is FakeAsrScenario.UNAVAILABLE:
            raise RuntimeUnavailableError("simulated unavailable runtime")
        if scenario is FakeAsrScenario.MISSING_MODEL:
            raise MissingModelError("simulated missing model")
        if scenario is FakeAsrScenario.TIMEOUT:
            raise AsrTimeoutError("simulated runtime timeout")
        if scenario is FakeAsrScenario.CANCELLATION:
            raise CancellationError("simulated runtime cancellation")
        if scenario is FakeAsrScenario.BACKEND_UNAVAILABLE:
            raise BackendUnavailableError("simulated unavailable backend")
        if scenario is FakeAsrScenario.CRASH:
            raise ProcessCrashedError("simulated sidecar process crash")
        if scenario is FakeAsrScenario.MALFORMED_RESULT:
            return cast(TranscriptResult, {"text": "not a TranscriptResult"})
        raise ExecutionError(f"unsupported fake scenario: {scenario}")

    def close(self) -> None:
        self._closed = True
