"""PortAudio-backed microphone capture for Windows desktop builds."""

from __future__ import annotations

import platform
import queue
import secrets
import time
from array import array
from collections.abc import Callable
from math import sqrt
from threading import Lock
from typing import Any

from voiceink_win.domain import (
    AudioCaptureSession,
    InputDevice,
    InputLevel,
    MicrophoneAvailability,
    MicrophoneCapability,
    MicrophoneStatus,
)

from .windows_microphone import WindowsCaptureError, WindowsCaptureFailureCode

CANONICAL_SAMPLE_RATE = 16_000
CAPTURE_BLOCK_SIZE = 1024
CAPTURE_QUEUE_SIZE = 128
SUPPORTED_ARCHITECTURES = frozenset({"AMD64", "X86_64", "X86", "i386", "i686"})


class SoundDeviceCaptureProvider:
    """Adapt sounddevice's callback API to the application capture boundary."""

    def __init__(
        self,
        *,
        sounddevice_module: Any | None = None,
        platform_name: str | None = None,
        architecture: str | None = None,
        queue_size: int = CAPTURE_QUEUE_SIZE,
    ) -> None:
        if queue_size <= 0:
            raise ValueError("capture queue size must be positive")
        self._sounddevice = sounddevice_module
        self._platform_name = platform_name or __import__("os").name
        self._architecture = architecture or platform.machine()
        self._queue_size = queue_size
        self._devices: dict[str, int] = {}
        self._device_signatures: dict[str, tuple[str, int | None]] = {}
        self._devices_lock = Lock()

    def status(self) -> MicrophoneStatus:
        if self._platform_name != "nt":
            return MicrophoneStatus(
                MicrophoneAvailability.UNAVAILABLE,
                "Windows microphone capture is unsupported on this platform",
                MicrophoneCapability.UNSUPPORTED,
            )
        if self._architecture.upper() not in SUPPORTED_ARCHITECTURES:
            return MicrophoneStatus(
                MicrophoneAvailability.UNAVAILABLE,
                "Windows microphone capture is unsupported on this architecture",
                MicrophoneCapability.UNSUPPORTED,
            )
        try:
            devices = self._refresh_devices()
        except WindowsCaptureError:
            return MicrophoneStatus(
                MicrophoneAvailability.UNAVAILABLE,
                "Windows microphone audio backend is unavailable",
                MicrophoneCapability.SUPPORTED,
            )
        if not devices:
            return MicrophoneStatus(
                MicrophoneAvailability.UNAVAILABLE,
                "No Windows microphone input device is available",
                MicrophoneCapability.SUPPORTED,
            )
        return MicrophoneStatus(
            MicrophoneAvailability.AVAILABLE,
            "Windows microphone capture is ready",
            MicrophoneCapability.SUPPORTED,
        )

    def enumerate_devices(self, deadline: float | None = None) -> tuple[InputDevice, ...]:
        return tuple(device for device, _ in self._refresh_devices(deadline))

    def open(
        self, selection_token: str | None = None, deadline: float | None = None
    ) -> AudioCaptureSession:
        _check_deadline(deadline)
        devices = self._refresh_devices(deadline)
        with self._devices_lock:
            device_index = self._devices.get(selection_token) if selection_token else None
        if selection_token is None:
            device_index = self._default_input_device()
        if device_index is None:
            raise WindowsCaptureError(WindowsCaptureFailureCode.DEVICE_UNAVAILABLE)
        if devices and not any(index == device_index for _, index in devices):
            raise WindowsCaptureError(WindowsCaptureFailureCode.DEVICE_UNAVAILABLE)
        sounddevice = self._modules()
        return SoundDeviceCaptureSession(
            sounddevice,
            device_index,
            queue_size=self._queue_size,
        )

    def _refresh_devices(self, deadline: float | None = None) -> list[tuple[InputDevice, int]]:
        _check_deadline(deadline)
        sounddevice = self._modules()
        try:
            raw_devices = sounddevice.query_devices()
        except Exception as error:
            raise WindowsCaptureError(WindowsCaptureFailureCode.DEVICE_UNAVAILABLE) from error

        default_index = self._default_input_device()
        devices: list[tuple[InputDevice, int]] = []
        tokens: dict[str, int] = {}
        signatures: dict[str, tuple[str, int | None]] = {}
        with self._devices_lock:
            previous_tokens = {
                signature: token for token, signature in self._device_signatures.items()
            }
        for index, raw in enumerate(raw_devices):
            _check_deadline(deadline)
            try:
                channels = int(raw.get("max_input_channels", 0))
                if channels <= 0:
                    continue
                sounddevice.check_input_settings(
                    device=index,
                    channels=1,
                    dtype="int16",
                    samplerate=CANONICAL_SAMPLE_RATE,
                )
                name = str(raw.get("name", "Windows microphone")).strip()[:128]
                if not name:
                    name = "Windows microphone"
                hostapi = raw.get("hostapi")
                hostapi_value = int(hostapi) if hostapi is not None else None
                signature = (name, hostapi_value)
                token = previous_tokens.get(signature) or secrets.token_urlsafe(18)
                tokens[token] = index
                signatures[token] = signature
                devices.append(
                    (
                        InputDevice(
                            selection_token=token,
                            display_name=name,
                            is_default_console=index == default_index,
                            channels=1,
                            sample_rates=(CANONICAL_SAMPLE_RATE,),
                            sample_types=("int16",),
                        ),
                        index,
                    )
                )
            except WindowsCaptureError:
                raise
            except Exception:
                continue
        with self._devices_lock:
            self._devices = tokens
            self._device_signatures = signatures
        return devices

    def _default_input_device(self) -> int | None:
        sounddevice = self._modules()
        try:
            value = sounddevice.default.device[0]
            return int(value) if value is not None and int(value) >= 0 else None
        except (AttributeError, IndexError, TypeError, ValueError):
            return None

    def _modules(self) -> Any:
        if self._sounddevice is None:
            try:
                import sounddevice
            except ImportError as error:
                raise WindowsCaptureError(WindowsCaptureFailureCode.FAILED) from error
            self._sounddevice = sounddevice
        return self._sounddevice


class SoundDeviceCaptureSession:
    """Bounded callback-to-reader bridge for one sounddevice input stream."""

    def __init__(self, sounddevice: Any, device_index: int, *, queue_size: int) -> None:
        self._sounddevice = sounddevice
        self._device_index = device_index
        self._chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=queue_size)
        self._stream: Any | None = None
        self._lock = Lock()
        self._native_lock = Lock()
        self._closed = False
        self._stopped = False
        self._failure: WindowsCaptureError | None = None
        self._listeners: list[Callable[[InputLevel], None]] = []

    def start(self) -> None:
        with self._lock:
            if self._closed or self._stopped:
                raise WindowsCaptureError(WindowsCaptureFailureCode.FAILED)
        stream = None
        try:
            stream = self._sounddevice.RawInputStream(
                samplerate=CANONICAL_SAMPLE_RATE,
                blocksize=CAPTURE_BLOCK_SIZE,
                channels=1,
                dtype="int16",
                device=self._device_index,
                callback=self._callback,
            )
            with self._native_lock:
                with self._lock:
                    if self._closed or self._stopped:
                        stream.close()
                        raise WindowsCaptureError(WindowsCaptureFailureCode.CANCELLED)
                    self._stream = stream
                stream.start()
        except WindowsCaptureError:
            raise
        except Exception as error:
            with self._native_lock:
                with self._lock:
                    owns_stream = stream is not None and self._stream is stream
                    if owns_stream:
                        self._stream = None
                if owns_stream and stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            raise _map_sounddevice_error(error) from error

    def read_chunk(self, deadline: float | None = None) -> bytes | None:
        while True:
            with self._lock:
                failure = self._failure
                stopped = self._stopped
            if failure is not None:
                raise failure
            if deadline is not None and time.monotonic() >= deadline:
                raise WindowsCaptureError(WindowsCaptureFailureCode.TIMEOUT)
            if stopped and self._chunks.empty():
                return None
            timeout = 0.1 if deadline is None else max(0.0, min(0.1, deadline - time.monotonic()))
            try:
                chunk = self._chunks.get(timeout=timeout)
            except queue.Empty:
                continue
            if chunk is None:
                with self._lock:
                    failure = self._failure
                if failure is not None:
                    raise failure
                return None
            self._publish_level(chunk)
            return chunk

    def stop(self) -> None:
        self._finish_capture(cancelled=False)

    def cancel(self) -> None:
        self._finish_capture(cancelled=True)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._finish_capture(cancelled=False)
        except WindowsCaptureError:
            with self._lock:
                self._closed = False
            raise

    def subscribe_level(self, listener: Callable[[InputLevel], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        del frames, time_info
        if status and getattr(status, "input_overflow", False):
            self._set_failure(WindowsCaptureFailureCode.CAPTURE_OVERFLOW)
            return
        try:
            chunk = bytes(indata)
            if not chunk:
                return
            self._chunks.put_nowait(chunk)
        except queue.Full:
            self._set_failure(WindowsCaptureFailureCode.CAPTURE_OVERFLOW)
        except Exception:
            self._set_failure(WindowsCaptureFailureCode.FAILED)

    def _publish_level(self, chunk: bytes) -> None:
        if len(chunk) % 2:
            return
        samples = array("h")
        samples.frombytes(chunk)
        if not samples:
            return
        rms = sqrt(sum(sample * sample for sample in samples) / len(samples))
        peak = max(abs(sample) for sample in samples)
        level = InputLevel(
            value=min(1.0, max(rms, peak) / 32767.0),
            timestamp=time.monotonic(),
        )
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(level)
            except Exception:
                continue

    def _finish_capture(self, *, cancelled: bool) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            stream = self._stream
        if stream is not None:
            try:
                with self._native_lock:
                    stream.stop()
                    stream.close()
                    with self._lock:
                        if self._stream is stream:
                            self._stream = None
            except Exception as error:
                with self._lock:
                    self._stopped = False
                raise _map_sounddevice_error(error) from error
        if cancelled:
            self._discard_queued_chunks()
        self._signal_end()

    def _discard_queued_chunks(self) -> None:
        while True:
            try:
                self._chunks.get_nowait()
            except queue.Empty:
                return

    def _signal_end(self) -> None:
        try:
            self._chunks.put_nowait(None)
        except queue.Full:
            self._discard_queued_chunks()
            self._chunks.put_nowait(None)

    def _set_failure(self, code: WindowsCaptureFailureCode) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = WindowsCaptureError(code)


def _map_sounddevice_error(error: Exception) -> WindowsCaptureError:
    message = str(error).casefold()
    if any(token in message for token in ("permission", "access denied", "not permitted")):
        code = WindowsCaptureFailureCode.PERMISSION_DENIED
    elif any(token in message for token in ("invalid sample", "sample rate", "format")):
        code = WindowsCaptureFailureCode.UNSUPPORTED_FORMAT
    elif any(token in message for token in ("busy", "in use")):
        code = WindowsCaptureFailureCode.BUSY
    elif any(token in message for token in ("device", "input")):
        code = WindowsCaptureFailureCode.DEVICE_UNAVAILABLE
    else:
        code = WindowsCaptureFailureCode.FAILED
    return WindowsCaptureError(code)


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise WindowsCaptureError(WindowsCaptureFailureCode.TIMEOUT)


__all__ = ["SoundDeviceCaptureProvider", "SoundDeviceCaptureSession"]
