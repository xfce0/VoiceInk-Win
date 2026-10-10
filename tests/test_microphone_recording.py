from __future__ import annotations

import time
from collections import deque
from threading import Event

import pytest

from voiceink_win.application import AsrApplicationService, MicrophoneRecordingService
from voiceink_win.domain import (
    BoundedPcm16Capture,
    CancellationError,
    CaptureError,
    CaptureErrorCode,
    CaptureLimitError,
    CaptureLimits,
    MicrophoneAvailability,
    MicrophoneCapability,
    MicrophoneStatus,
    MicrophoneUnavailableError,
    QueueFullError,
    RecordingState,
)
from voiceink_win.infrastructure import (
    FakeAsrRuntime,
    WindowsAudioInputAdapter,
    WindowsCaptureError,
    WindowsCaptureFailureCode,
)


class FakeCaptureSession:
    def __init__(self, chunks: list[bytes], *, wait_for_stop: bool = False) -> None:
        self._chunks = deque(chunks)
        self._wait_for_stop = wait_for_stop
        self._stop = Event()
        self._cancel = Event()
        self.started = False
        self.closed = False
        self.close_calls = 0

    def start(self) -> None:
        self.started = True

    def read_chunk(self, deadline: float | None = None) -> bytes | None:
        del deadline
        if self._chunks:
            chunk = self._chunks.popleft()
            if self._wait_for_stop and not self._chunks:
                self._stop.wait()
            return chunk
        if self._wait_for_stop:
            self._stop.wait()
        return None

    def stop(self) -> None:
        self._stop.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._stop.set()

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class FakeAudioInput:
    def __init__(self, session: FakeCaptureSession) -> None:
        self.session = session
        self.opened_token: str | None = None

    def status(self):
        from voiceink_win.domain import MicrophoneStatus

        return MicrophoneStatus(MicrophoneAvailability.AVAILABLE, "test input")

    def enumerate_devices(self, deadline=None):
        del deadline
        return ()

    def open(self, selection_token=None, deadline=None):
        del deadline
        self.opened_token = selection_token
        return self.session


def test_bounded_capture_rejects_chunk_and_aggregate_overflow() -> None:
    limits = CaptureLimits(max_samples=2, max_bytes=4, max_chunk_bytes=4)
    capture = BoundedPcm16Capture(limits)

    capture.append(b"\x01\x00")
    with pytest.raises(CaptureLimitError) as error:
        capture.append(b"\x02\x00\x03\x00")

    assert error.value.code is CaptureErrorCode.RESOURCE_LIMIT_EXCEEDED
    assert capture.finish() is not None


def test_windows_adapter_reports_unavailable_without_native_fallback() -> None:
    adapter = WindowsAudioInputAdapter()

    assert adapter.status().availability is MicrophoneAvailability.UNAVAILABLE
    assert adapter.status().capability is MicrophoneCapability.UNSUPPORTED
    with pytest.raises(MicrophoneUnavailableError):
        adapter.enumerate_devices()
    with pytest.raises(MicrophoneUnavailableError):
        adapter.open()


class Provider:
    def __init__(self, session: FakeCaptureSession) -> None:
        self.session = session
        self.status_calls = 0

    def status(self) -> MicrophoneStatus:
        self.status_calls += 1
        return MicrophoneStatus(
            MicrophoneAvailability.AVAILABLE,
            "provider is ready",
            MicrophoneCapability.SUPPORTED,
        )

    def enumerate_devices(self, deadline=None):
        del deadline
        return ()

    def open(self, selection_token=None, deadline=None):
        del selection_token, deadline
        return self.session


def test_windows_adapter_delegates_supported_provider_without_exposing_native_details() -> None:
    session = FakeCaptureSession([b"\x00\x00"])
    provider = Provider(session)
    adapter = WindowsAudioInputAdapter(provider, platform_name="nt", architecture="AMD64")

    assert adapter.status().capability is MicrophoneCapability.SUPPORTED
    assert adapter.enumerate_devices() == ()
    delegated = adapter.open("opaque-token")
    delegated.start()
    assert delegated.read_chunk() == b"\x00\x00"
    delegated.close()
    delegated.close()

    assert provider.status_calls == 1
    assert session.close_calls == 1


def test_windows_adapter_rejects_native_arm64_without_claiming_support() -> None:
    provider = Provider(FakeCaptureSession([]))
    adapter = WindowsAudioInputAdapter(provider, platform_name="nt", architecture="ARM64")

    assert adapter.status() == MicrophoneStatus(
        MicrophoneAvailability.UNAVAILABLE,
        "Native Windows microphone capture is unsupported on this architecture",
        MicrophoneCapability.UNSUPPORTED,
    )
    with pytest.raises(MicrophoneUnavailableError):
        adapter.open()
    assert provider.status_calls == 0


@pytest.mark.parametrize(
    ("provider_code", "domain_code"),
    [
        (WindowsCaptureFailureCode.PERMISSION_DENIED, CaptureErrorCode.PERMISSION_DENIED),
        (WindowsCaptureFailureCode.DEVICE_UNAVAILABLE, CaptureErrorCode.DEVICE_UNAVAILABLE),
        (WindowsCaptureFailureCode.BUSY, CaptureErrorCode.BUSY),
        (WindowsCaptureFailureCode.UNSUPPORTED_FORMAT, CaptureErrorCode.UNSUPPORTED_FORMAT),
        (WindowsCaptureFailureCode.DEVICE_DISCONNECTED, CaptureErrorCode.DEVICE_DISCONNECTED),
        (WindowsCaptureFailureCode.CAPTURE_OVERFLOW, CaptureErrorCode.CAPTURE_OVERFLOW),
        (WindowsCaptureFailureCode.TIMEOUT, CaptureErrorCode.TIMEOUT),
        (WindowsCaptureFailureCode.CANCELLED, CaptureErrorCode.CANCELLED),
        (WindowsCaptureFailureCode.CLEANUP_FAILED, CaptureErrorCode.CLEANUP_FAILED),
        (WindowsCaptureFailureCode.FAILED, CaptureErrorCode.FAILED),
    ],
)
def test_windows_adapter_maps_provider_failures_to_safe_domain_codes(
    provider_code, domain_code
) -> None:
    class FailingProvider(Provider):
        def enumerate_devices(self, deadline=None):
            del deadline
            raise WindowsCaptureError(provider_code, "native HRESULT must not escape")

    adapter = WindowsAudioInputAdapter(
        FailingProvider(FakeCaptureSession([])), platform_name="nt", architecture="AMD64"
    )

    with pytest.raises(CaptureError) as error:
        adapter.enumerate_devices()

    assert error.value.code is domain_code
    assert "HRESULT" not in error.value.message
    assert error.value.__cause__ is None


def test_windows_adapter_session_rejects_double_start_and_retries_failed_cleanup() -> None:
    class CleanupFailingSession(FakeCaptureSession):
        def __init__(self) -> None:
            super().__init__([])
            self.fail_cleanup = True

        def close(self) -> None:
            self.close_calls += 1
            if self.fail_cleanup:
                raise WindowsCaptureError(
                    WindowsCaptureFailureCode.CLEANUP_FAILED, "native cleanup detail"
                )
            self.closed = True

    session = CleanupFailingSession()
    adapter = WindowsAudioInputAdapter(Provider(session), platform_name="nt", architecture="AMD64")
    delegated = adapter.open()
    delegated.start()

    with pytest.raises(CaptureError) as start_error:
        delegated.start()
    assert start_error.value.code is CaptureErrorCode.FAILED

    with pytest.raises(CaptureError) as cleanup_error:
        delegated.close()
    assert cleanup_error.value.code is CaptureErrorCode.CLEANUP_FAILED

    session.fail_cleanup = False
    delegated.close()
    delegated.close()
    assert session.close_calls == 2
    assert session.closed


def test_recording_stop_builds_pcm_and_uses_shared_asr_admission() -> None:
    session = FakeCaptureSession([b"\x01\x00" * 8_000], wait_for_stop=True)
    audio_input = FakeAudioInput(session)
    asr = AsrApplicationService(FakeAsrRuntime())
    recording = MicrophoneRecordingService(audio_input, asr).start("selection-token")

    recording.stop()
    result = recording.wait(2.0)

    assert result.state is RecordingState.SUCCEEDED
    assert result.audio is not None
    assert result.audio.sample_count == 8_000
    assert result.transcript is not None
    assert audio_input.opened_token == "selection-token"
    assert session.closed
    asr.close()


def test_recording_cancellation_during_capture_does_not_call_asr() -> None:
    session = FakeCaptureSession([], wait_for_stop=True)
    audio_input = FakeAudioInput(session)
    asr = AsrApplicationService(FakeAsrRuntime())
    recording = MicrophoneRecordingService(audio_input, asr).start()

    recording.cancel()
    result = recording.wait(2.0)

    assert result.state is RecordingState.CANCELLED
    assert result.audio is None
    assert asr.admitted_count == 0
    asr.close()


def test_recording_cleanup_failure_is_failed_and_does_not_call_asr() -> None:
    class CleanupFailureSession(FakeCaptureSession):
        def close(self) -> None:
            raise CaptureError(CaptureErrorCode.CLEANUP_FAILED, "cleanup failed")

    session = CleanupFailureSession([b"\x00\x00"])
    asr = AsrApplicationService(FakeAsrRuntime())
    recording = MicrophoneRecordingService(FakeAudioInput(session), asr).start()

    result = recording.wait(2.0)

    assert result.state is RecordingState.FAILED
    assert isinstance(result.error, CaptureError)
    assert result.error.code is CaptureErrorCode.CLEANUP_FAILED
    assert asr.admitted_count == 0
    asr.close()


def test_recording_returns_rejected_when_shared_asr_queue_is_full() -> None:
    class FullScheduler:
        def try_admit(self, request):
            del request
            raise QueueFullError("shared ASR queue is full")

    session = FakeCaptureSession([b"\x00\x00" * 4])
    recording = MicrophoneRecordingService(FakeAudioInput(session), FullScheduler()).start()

    result = recording.wait(2.0)

    assert result.state is RecordingState.REJECTED
    assert isinstance(result.error, QueueFullError)


def test_recording_cancellation_reaches_request_scoped_asr_fence() -> None:
    started = Event()

    class CooperativeRuntime(FakeAsrRuntime):
        def transcribe(self, request):
            started.set()
            while not request.cancellation.is_cancelled():
                time.sleep(0.005)
            raise CancellationError("cooperative cancellation")

    asr = AsrApplicationService(CooperativeRuntime())
    session = FakeCaptureSession([b"\x00\x00" * 4])
    recording = MicrophoneRecordingService(FakeAudioInput(session), asr).start()
    assert started.wait(1.0)

    recording.cancel()
    result = recording.wait(2.0)

    assert result.state is RecordingState.CANCELLED
    assert isinstance(result.error, CancellationError)
    asr.close()
