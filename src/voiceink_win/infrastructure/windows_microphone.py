"""Infrastructure boundary for a verified Windows microphone provider."""

from __future__ import annotations

import os
import platform
from enum import StrEnum
from threading import Lock
from typing import Protocol

from voiceink_win.domain import (
    AudioCaptureSession,
    CaptureError,
    CaptureErrorCode,
    InputDevice,
    MicrophoneAvailability,
    MicrophoneCapability,
    MicrophoneStatus,
    MicrophoneUnavailableError,
)


class WindowsCaptureFailureCode(StrEnum):
    """Stable provider failures before they are mapped to domain codes."""

    PERMISSION_DENIED = "permission_denied"
    DEVICE_UNAVAILABLE = "device_unavailable"
    BUSY = "busy"
    UNSUPPORTED_FORMAT = "unsupported_format"
    DEVICE_DISCONNECTED = "device_disconnected"
    CAPTURE_OVERFLOW = "capture_overflow"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    CLEANUP_FAILED = "cleanup_failed"
    FAILED = "failed"


class WindowsCaptureError(Exception):
    """Provider error carrying no native type or native error value."""

    def __init__(self, code: WindowsCaptureFailureCode, message: str = "capture failed") -> None:
        super().__init__(message)
        self.code = code


class WindowsCaptureProvider(Protocol):
    """Future helper-backed provider; native details stay on this side."""

    def status(self) -> MicrophoneStatus: ...

    def enumerate_devices(self, deadline: float | None = None) -> tuple[InputDevice, ...]: ...

    def open(
        self, selection_token: str | None = None, deadline: float | None = None
    ) -> AudioCaptureSession: ...


_SUPPORTED_NATIVE_ARCHITECTURES = frozenset({"AMD64", "X86_64"})
_FAILURE_CODES = {
    WindowsCaptureFailureCode.PERMISSION_DENIED: CaptureErrorCode.PERMISSION_DENIED,
    WindowsCaptureFailureCode.DEVICE_UNAVAILABLE: CaptureErrorCode.DEVICE_UNAVAILABLE,
    WindowsCaptureFailureCode.BUSY: CaptureErrorCode.BUSY,
    WindowsCaptureFailureCode.UNSUPPORTED_FORMAT: CaptureErrorCode.UNSUPPORTED_FORMAT,
    WindowsCaptureFailureCode.DEVICE_DISCONNECTED: CaptureErrorCode.DEVICE_DISCONNECTED,
    WindowsCaptureFailureCode.CAPTURE_OVERFLOW: CaptureErrorCode.CAPTURE_OVERFLOW,
    WindowsCaptureFailureCode.TIMEOUT: CaptureErrorCode.TIMEOUT,
    WindowsCaptureFailureCode.CANCELLED: CaptureErrorCode.CANCELLED,
    WindowsCaptureFailureCode.CLEANUP_FAILED: CaptureErrorCode.CLEANUP_FAILED,
    WindowsCaptureFailureCode.FAILED: CaptureErrorCode.FAILED,
}
_SAFE_FAILURE_MESSAGES = {
    CaptureErrorCode.PERMISSION_DENIED: "Windows microphone permission was denied",
    CaptureErrorCode.DEVICE_UNAVAILABLE: "Windows microphone device is unavailable",
    CaptureErrorCode.BUSY: "Windows microphone device is busy",
    CaptureErrorCode.UNSUPPORTED_FORMAT: "Windows microphone format is unsupported",
    CaptureErrorCode.DEVICE_DISCONNECTED: "Windows microphone device was disconnected",
    CaptureErrorCode.CAPTURE_OVERFLOW: "Windows microphone capture exceeded its bounded queue",
    CaptureErrorCode.TIMEOUT: "Windows microphone operation timed out",
    CaptureErrorCode.CANCELLED: "Windows microphone capture was cancelled",
    CaptureErrorCode.CLEANUP_FAILED: "Windows microphone cleanup failed",
    CaptureErrorCode.FAILED: "Windows microphone capture failed",
}


class WindowsAudioInputAdapter:
    """Expose a verified provider through the domain audio-input port.

    The default adapter is intentionally unavailable until a native provider is
    supplied. This prevents an unverified executable or Python fallback from
    becoming an accidental production capture path.
    """

    def __init__(
        self,
        provider: WindowsCaptureProvider | None = None,
        *,
        platform_name: str | None = None,
        architecture: str | None = None,
    ) -> None:
        self._provider = provider
        self._status = self._build_status(
            provider,
            platform_name=platform_name or os.name,
            architecture=architecture or platform.machine(),
        )

    def status(self) -> MicrophoneStatus:
        return self._status

    def enumerate_devices(self, deadline: float | None = None) -> tuple[InputDevice, ...]:
        provider = self._require_provider()
        try:
            return provider.enumerate_devices(deadline)
        except WindowsCaptureError as error:
            raise _map_provider_error(error) from None

    def open(
        self, selection_token: str | None = None, deadline: float | None = None
    ) -> AudioCaptureSession:
        provider = self._require_provider()
        try:
            session = provider.open(selection_token, deadline)
        except WindowsCaptureError as error:
            raise _map_provider_error(error) from None
        return _MappedCaptureSession(session)

    def _require_provider(self) -> WindowsCaptureProvider:
        if self._provider is None or self._status.capability is MicrophoneCapability.UNSUPPORTED:
            raise MicrophoneUnavailableError(self._status.message)
        return self._provider

    @staticmethod
    def _build_status(
        provider: WindowsCaptureProvider | None,
        *,
        platform_name: str,
        architecture: str,
    ) -> MicrophoneStatus:
        if platform_name != "nt":
            return MicrophoneStatus(
                MicrophoneAvailability.UNAVAILABLE,
                "Windows microphone capture is unsupported on this platform",
                MicrophoneCapability.UNSUPPORTED,
            )
        if architecture.upper() not in _SUPPORTED_NATIVE_ARCHITECTURES:
            return MicrophoneStatus(
                MicrophoneAvailability.UNAVAILABLE,
                "Native Windows microphone capture is unsupported on this architecture",
                MicrophoneCapability.UNSUPPORTED,
            )
        if provider is None:
            return MicrophoneStatus(
                MicrophoneAvailability.UNAVAILABLE,
                "Windows microphone capture is unavailable because "
                "no native provider is configured",
                MicrophoneCapability.UNSUPPORTED,
            )
        return provider.status()


class _MappedCaptureSession:
    def __init__(self, session: AudioCaptureSession) -> None:
        self._session = session
        self._lock = Lock()
        self._started = False
        self._closed = False

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise CaptureError(CaptureErrorCode.FAILED, "microphone session is closed")
            if self._started:
                raise CaptureError(CaptureErrorCode.FAILED, "microphone session already started")
            try:
                self._session.start()
            except WindowsCaptureError as error:
                raise _map_provider_error(error) from None
            self._started = True

    def read_chunk(self, deadline: float | None = None) -> bytes | None:
        with self._lock:
            if self._closed:
                raise CaptureError(CaptureErrorCode.FAILED, "microphone session is closed")
        try:
            return self._session.read_chunk(deadline)
        except WindowsCaptureError as error:
            raise _map_provider_error(error) from None

    def stop(self) -> None:
        with self._lock:
            if self._closed:
                return
        try:
            self._session.stop()
        except WindowsCaptureError as error:
            raise _map_provider_error(error) from None

    def cancel(self) -> None:
        with self._lock:
            if self._closed:
                return
        try:
            self._session.cancel()
        except WindowsCaptureError as error:
            raise _map_provider_error(error) from None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._session.close()
            except WindowsCaptureError as error:
                raise _map_provider_error(error) from None
            self._closed = True


def _map_provider_error(error: WindowsCaptureError) -> CaptureError:
    code = _FAILURE_CODES[error.code]
    if code is CaptureErrorCode.UNAVAILABLE:
        return MicrophoneUnavailableError(_SAFE_FAILURE_MESSAGES[code])
    return CaptureError(code, _SAFE_FAILURE_MESSAGES[code])
