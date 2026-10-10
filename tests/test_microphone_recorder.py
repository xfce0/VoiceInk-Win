from __future__ import annotations

import time
from collections import deque
from threading import Event, Lock, current_thread

import pytest

from voiceink_win.application import (
    AsrApplicationService,
    MicrophoneRecorderController,
    MicrophoneRecordingService,
)
from voiceink_win.domain import (
    CaptureError,
    CaptureErrorCode,
    InputLevel,
    MicrophoneAvailability,
    MicrophoneCapability,
    MicrophoneStatus,
    RecorderState,
)
from voiceink_win.infrastructure import FakeAsrRuntime


class LevelSession:
    def __init__(self, *, wait_for_stop: bool = True) -> None:
        self._chunks = deque([b"\x00\x00" * 4])
        self._stop = Event()
        self._wait_for_stop = wait_for_stop
        self._listeners = []
        self._listener_lock = Lock()
        self.worker_name = ""

    def start(self) -> None:
        self.worker_name = current_thread().name

    def read_chunk(self, deadline=None) -> bytes | None:
        del deadline
        if self._chunks:
            chunk = self._chunks.popleft()
            if self._wait_for_stop:
                self._stop.wait()
            return chunk
        if self._wait_for_stop:
            self._stop.wait()
        return None

    def stop(self) -> None:
        self._stop.set()

    def cancel(self) -> None:
        self._stop.set()

    def close(self) -> None:
        self._stop.set()

    def subscribe_level(self, listener) -> object:
        with self._listener_lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._listener_lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def emit(self, value: float) -> None:
        level = InputLevel(value, time.monotonic())
        with self._listener_lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener(level)


class Input:
    def __init__(self, session: LevelSession, error: Exception | None = None) -> None:
        self.session = session
        self.error = error
        self.open_calls = 0
        self.open_thread = ""

    def status(self) -> MicrophoneStatus:
        return MicrophoneStatus(
            MicrophoneAvailability.AVAILABLE,
            "test microphone",
            MicrophoneCapability.SUPPORTED,
        )

    def enumerate_devices(self, deadline=None):
        del deadline
        return ()

    def open(self, selection_token=None, deadline=None):
        del selection_token, deadline
        self.open_calls += 1
        self.open_thread = current_thread().name
        if self.error is not None:
            raise self.error
        return self.session


def wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def make_controller(
    input_port: Input,
) -> tuple[MicrophoneRecorderController, AsrApplicationService]:
    asr = AsrApplicationService(FakeAsrRuntime())
    service = MicrophoneRecordingService(input_port, asr)
    return MicrophoneRecorderController(service), asr


def test_start_requests_real_open_on_application_worker_and_transitions_to_silent() -> None:
    session = LevelSession()
    input_port = Input(session)
    controller, asr = make_controller(input_port)
    try:
        assert controller.start("selected-token")
        wait_for(lambda: controller.snapshot.state is RecorderState.RECORDING_SILENT)
        assert input_port.open_calls == 1
        assert input_port.open_thread.startswith("microphone-command")
        assert session.worker_name == "microphone-recording"
    finally:
        controller.close()
        asr.close()


def test_permission_denial_is_error_and_never_becomes_idle_or_empty() -> None:
    input_port = Input(
        LevelSession(),
        CaptureError(CaptureErrorCode.PERMISSION_DENIED, "private native detail"),
    )
    controller, asr = make_controller(input_port)
    try:
        assert controller.start()
        wait_for(lambda: controller.snapshot.state is RecorderState.ERROR)
        assert "Windows Privacy" in controller.snapshot.message
        assert "private native detail" not in controller.snapshot.message
    finally:
        controller.close()
        asr.close()


def test_unavailable_controller_does_not_call_microphone_service() -> None:
    controller = MicrophoneRecorderController()

    assert controller.snapshot.state is RecorderState.UNAVAILABLE
    assert not controller.start()
    controller.close()


def test_input_level_drives_sound_state_and_stale_level_returns_to_idle_waveform() -> None:
    session = LevelSession()
    controller, asr = make_controller(Input(session))
    try:
        controller.start()
        wait_for(lambda: controller.snapshot.state is RecorderState.RECORDING_SILENT)

        session.emit(0.1)
        wait_for(lambda: controller.snapshot.state is RecorderState.RECORDING_SOUNDING)
        session.emit(0.02)
        wait_for(lambda: controller.snapshot.state is RecorderState.RECORDING_SILENT)

        session.emit(0.1)
        wait_for(lambda: controller.snapshot.state is RecorderState.RECORDING_SOUNDING)
        wait_for(lambda: controller.snapshot.state is RecorderState.RECORDING_SILENT, 1.0)
    finally:
        controller.close()
        asr.close()


def test_stop_transitions_through_processing_to_success_and_cancel_is_idempotent() -> None:
    session = LevelSession()
    controller, asr = make_controller(Input(session))
    snapshots = []
    controller.subscribe(snapshots.append)
    try:
        controller.start()
        wait_for(lambda: controller.snapshot.state is RecorderState.RECORDING_SILENT)
        assert controller.stop()
        wait_for(lambda: controller.snapshot.state is RecorderState.SUCCEEDED)
        assert RecorderState.STOPPING in {snapshot.state for snapshot in snapshots}
        assert RecorderState.PROCESSING in {snapshot.state for snapshot in snapshots}
        assert not controller.cancel()
    finally:
        controller.close()
        asr.close()


@pytest.mark.parametrize("value", [-0.1, 1.1, float("nan")])
def test_input_level_rejects_values_outside_normalized_contract(value: float) -> None:
    with pytest.raises(ValueError):
        InputLevel(value, time.monotonic())
