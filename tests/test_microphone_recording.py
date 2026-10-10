from __future__ import annotations

import time
from collections import deque
from threading import Event

import pytest

from voiceink_win.application import AsrApplicationService, MicrophoneRecordingService
from voiceink_win.domain import (
    BoundedPcm16Capture,
    CancellationError,
    CaptureErrorCode,
    CaptureLimitError,
    CaptureLimits,
    MicrophoneAvailability,
    MicrophoneUnavailableError,
    QueueFullError,
    RecordingState,
)
from voiceink_win.infrastructure import FakeAsrRuntime, WindowsAudioInputAdapter


class FakeCaptureSession:
    def __init__(self, chunks: list[bytes], *, wait_for_stop: bool = False) -> None:
        self._chunks = deque(chunks)
        self._wait_for_stop = wait_for_stop
        self._stop = Event()
        self._cancel = Event()
        self.started = False
        self.closed = False

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
    with pytest.raises(MicrophoneUnavailableError):
        adapter.enumerate_devices()
    with pytest.raises(MicrophoneUnavailableError):
        adapter.open()


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
