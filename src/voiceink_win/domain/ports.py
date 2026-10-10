"""Ports owned by the domain and implemented by outer layers."""

from __future__ import annotations

from typing import Protocol

from .audio_capture import InputDevice, MicrophoneStatus
from .models import AsrCapabilities, AsrRequest, RuntimeHealth, TranscriptResult


class AsrRuntime(Protocol):
    def capabilities(self) -> AsrCapabilities: ...

    def health(self) -> RuntimeHealth: ...

    def transcribe(self, request: AsrRequest) -> TranscriptResult: ...

    def close(self, deadline: float | None = None) -> None: ...


class AsrRequestHandle(Protocol):
    """Request-scoped ownership and quiescence boundary for admitted ASR work."""

    def cancel(self, deadline: float | None = None) -> None: ...

    def await_result(self, deadline: float | None = None) -> TranscriptResult: ...

    def await_quiescence(self, deadline: float | None = None) -> None: ...

    def release(self) -> None: ...


class AudioCaptureSession(Protocol):
    """Single-use capture session owned by the infrastructure adapter."""

    def start(self) -> None: ...

    def read_chunk(self, deadline: float | None = None) -> bytes | None: ...

    def stop(self) -> None: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


class AudioInputPort(Protocol):
    """Explicit microphone boundary; native endpoint details stay outside it."""

    def status(self) -> MicrophoneStatus: ...

    def enumerate_devices(self, deadline: float | None = None) -> tuple[InputDevice, ...]: ...

    def open(
        self, selection_token: str | None = None, deadline: float | None = None
    ) -> AudioCaptureSession: ...
