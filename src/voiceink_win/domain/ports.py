"""Ports owned by the domain and implemented by outer layers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .audio_capture import InputDevice, InputLevel, MicrophoneStatus
from .models import AsrCapabilities, AsrRequest, RuntimeHealth, TranscriptResult
from .shortcuts import GlobalShortcut


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


class GlobalShortcutRegistration(Protocol):
    """A registered shortcut that owns its native registration lifetime."""

    def unregister(self) -> None: ...


class GlobalShortcutPort(Protocol):
    """Register one process callback without exposing platform event details."""

    def register(
        self, shortcut: GlobalShortcut, callback: Callable[[], None]
    ) -> GlobalShortcutRegistration: ...


class AudioCaptureSession(Protocol):
    """Single-use capture session owned by the infrastructure adapter."""

    def start(self) -> None: ...

    def read_chunk(self, deadline: float | None = None) -> bytes | None: ...

    def stop(self) -> None: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...

    def subscribe_level(self, listener: Callable[[InputLevel], None]) -> Callable[[], None]: ...


class AudioInputPort(Protocol):
    """Explicit microphone boundary; native endpoint details stay outside it."""

    def status(self) -> MicrophoneStatus: ...

    def enumerate_devices(self, deadline: float | None = None) -> tuple[InputDevice, ...]: ...

    def open(
        self, selection_token: str | None = None, deadline: float | None = None
    ) -> AudioCaptureSession: ...
