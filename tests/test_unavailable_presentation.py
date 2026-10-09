from __future__ import annotations

import os
import time
from threading import Event

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton
except ImportError:
    if os.environ.get("VOICEINK_GUI_TESTS") == "1":
        raise
    pytest.skip("PySide6 is required for presentation tests", allow_module_level=True)

from voiceink_win.domain import ShellState
from voiceink_win.presentation.main_window import MainWindow


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


@pytest.fixture(scope="module")
def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_unavailable_presentation_uses_exact_copy_and_no_active_timers(
    application: QApplication,
) -> None:
    del application
    from voiceink_win.application import ShellController

    window = MainWindow(ShellController.unavailable())
    recorder = window._recorder

    assert window._state_pill.text() == "Recording unavailable"
    assert window._page_subtext.text() == (
        "Recording cannot start because microphone capture and ASR are not included."
    )
    assert window._hero_headline.text() == "Recording is unavailable in this build."
    assert window._hero_detail.text() == "Microphone capture and ASR are not included."
    assert window._transcript_metadata.text() == "Capability unavailable"
    assert window._transcript_text.text() == (
        "Transcripts are unavailable because recording and ASR are not included."
    )
    assert recorder._status.text() == "Unavailable"
    assert recorder._record_button.text() == "Unavailable"
    assert not recorder._record_button.isEnabled()
    assert recorder._record_button.accessibleName() == "Recording unavailable"
    assert "Microphone capture is not connected" in recorder._record_button.accessibleDescription()
    assert recorder._waveform._timer is None
    assert not recorder._waveform._active
    assert recorder._timer is None
    assert not window._open_recorder_button.isEnabled()
    assert window._open_recorder_button.text() == "Recorder unavailable"

    rendered_text = " ".join(
        widget.text()
        for widget_type in (QLabel, QPushButton)
        for widget in window.findChildren(widget_type)
    )
    for forbidden in (
        "Ready",
        "Listening",
        "Transcribing",
        "Transcript ready",
        "Start recording",
        "Start again",
        "Try again",
        "Working...",
    ):
        assert forbidden not in rendered_text

    window.close()


def test_production_entrypoint_runs_real_composition_session(
    application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del application
    import voiceink_win.presentation.app as presentation_app
    import voiceink_win.presentation.main_window as main_window_module

    events: list[str] = []
    real_builder = presentation_app.build_desktop_composition

    class CompositionSpy:
        def __init__(self) -> None:
            self._composition = real_builder()
            self.controller = self._composition.controller
            self.transcribe_controller = self._composition.transcribe_controller

        def close(self) -> None:
            events.append("close")
            self._composition.close()

    def build_composition() -> CompositionSpy:
        events.append("build")
        return CompositionSpy()

    real_window = main_window_module.MainWindow

    class RecordingWindow(real_window):
        def __init__(self, *args, **kwargs) -> None:
            events.append("construct")
            super().__init__(*args, **kwargs)

        def show(self) -> None:
            events.append("show")
            super().show()

    monkeypatch.setattr(presentation_app, "build_desktop_composition", build_composition)
    monkeypatch.setattr(main_window_module, "MainWindow", RecordingWindow)
    monkeypatch.setattr(presentation_app.sys, "argv", ["voiceink-gui-test"])
    real_exec = QApplication.exec

    def record_exec(application: QApplication) -> int:
        events.append("exec")
        del application
        result = real_exec()
        events.append("exec-return")
        return result

    monkeypatch.setattr(QApplication, "exec", record_exec)

    assert presentation_app.main(smoke=True) == 0
    assert events == ["build", "construct", "show", "exec", "exec-return", "close"]


def test_production_entrypoint_closes_composition_on_event_loop_failure(
    application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del application
    import voiceink_win.presentation.app as presentation_app
    import voiceink_win.presentation.main_window as main_window_module

    events: list[str] = []
    real_builder = presentation_app.build_desktop_composition

    class CompositionSpy:
        def __init__(self) -> None:
            self._composition = real_builder()
            self.controller = self._composition.controller
            self.transcribe_controller = self._composition.transcribe_controller

        def close(self) -> None:
            events.append("close")
            self._composition.close()

    def build_composition() -> CompositionSpy:
        events.append("build")
        return CompositionSpy()

    real_window = main_window_module.MainWindow

    class RecordingWindow(real_window):
        def __init__(self, *args, **kwargs) -> None:
            events.append("construct")
            super().__init__(*args, **kwargs)

        def show(self) -> None:
            events.append("show")
            super().show()

    error = RuntimeError("event loop failed")

    def fail_exec(_application: QApplication) -> int:
        events.append("exec")
        raise error

    monkeypatch.setattr(presentation_app, "build_desktop_composition", build_composition)
    monkeypatch.setattr(main_window_module, "MainWindow", RecordingWindow)
    monkeypatch.setattr(presentation_app.sys, "argv", ["voiceink-gui-test"])
    monkeypatch.setattr(QApplication, "exec", fail_exec)

    with pytest.raises(RuntimeError) as raised:
        presentation_app.main()

    assert raised.value is error
    assert events == ["build", "construct", "show", "exec", "close"]


def test_composition_wires_real_imported_media_adapter_when_runtime_is_configured(
    application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import voiceink_win.composition as composition_module
    import voiceink_win.desktop_composition as desktop_composition

    class Backend:
        imported_media_available = True

        def __init__(self) -> None:
            self.started = False
            self.closed = False

        def start(self) -> None:
            self.started = True

        def close(self) -> None:
            self.closed = True

    backend = Backend()
    for name in (
        "VOICEINK_RUNTIME_MANIFEST",
        "VOICEINK_ARTIFACT_LOCK",
        "VOICEINK_ARTIFACT_LOCK_SHA256",
        "VOICEINK_FFMPEG_PATH",
        "VOICEINK_IMPORT_WORKSPACE_ROOT",
        "VOICEINK_IMPORT_ROOTS",
        "VOICEINK_FFMPEG_VERSION",
        "VOICEINK_FFMPEG_PROVENANCE_URL",
        "VOICEINK_FFMPEG_SHA256",
        "VOICEINK_FFMPEG_LICENSE",
    ):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setattr(composition_module, "build_application_from_environment", lambda: backend)

    composed = desktop_composition.build_desktop_composition()
    try:
        _wait_for(
            lambda: backend.started and composed.transcribe_controller._imported_media is backend
        )
        assert composed.controller.snapshot.state is ShellState.UNAVAILABLE
        assert composed.transcribe_controller._imported_media is backend
    finally:
        composed.close()
    assert backend.closed


def test_configured_composition_keeps_history_wiring_for_builder_with_keyword(
    application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del application
    import voiceink_win.composition as composition_module
    import voiceink_win.desktop_composition as desktop_composition

    class Backend:
        imported_media_available = True

        def start(self) -> None:
            return None

        def close(self) -> None:
            return None

    backend = Backend()
    received: list[object] = []

    def builder(*, history_port) -> Backend:
        received.append(history_port)
        return backend

    for name in (
        "VOICEINK_RUNTIME_MANIFEST",
        "VOICEINK_ARTIFACT_LOCK",
        "VOICEINK_ARTIFACT_LOCK_SHA256",
        "VOICEINK_FFMPEG_PATH",
        "VOICEINK_IMPORT_WORKSPACE_ROOT",
        "VOICEINK_IMPORT_ROOTS",
        "VOICEINK_FFMPEG_VERSION",
        "VOICEINK_FFMPEG_PROVENANCE_URL",
        "VOICEINK_FFMPEG_SHA256",
        "VOICEINK_FFMPEG_LICENSE",
    ):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setattr(composition_module, "build_application_from_environment", builder)

    composed = desktop_composition.build_desktop_composition()
    try:
        _wait_for(lambda: composed.transcribe_controller._imported_media is backend)
        assert received == [composed.persistence]
    finally:
        composed.close()


def test_composition_returns_loading_state_before_backend_readiness(
    application: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del application
    import voiceink_win.composition as composition_module
    import voiceink_win.desktop_composition as desktop_composition
    from voiceink_win.domain import TranscribeAvailability

    entered = Event()
    release = Event()

    class Backend:
        imported_media_available = True

        def start(self) -> None:
            entered.set()
            release.wait(2)

        def close(self) -> None:
            return None

    backend = Backend()
    for name in (
        "VOICEINK_RUNTIME_MANIFEST",
        "VOICEINK_ARTIFACT_LOCK",
        "VOICEINK_ARTIFACT_LOCK_SHA256",
        "VOICEINK_FFMPEG_PATH",
        "VOICEINK_IMPORT_WORKSPACE_ROOT",
        "VOICEINK_IMPORT_ROOTS",
        "VOICEINK_FFMPEG_VERSION",
        "VOICEINK_FFMPEG_PROVENANCE_URL",
        "VOICEINK_FFMPEG_SHA256",
        "VOICEINK_FFMPEG_LICENSE",
    ):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setattr(composition_module, "build_application_from_environment", lambda: backend)

    started = time.monotonic()
    composed = desktop_composition.build_desktop_composition()
    try:
        assert time.monotonic() - started < 0.5
        assert (
            composed.transcribe_controller.snapshot.availability is TranscribeAvailability.LOADING
        )
        assert not composed.transcribe_controller.snapshot.accepting_files
        assert entered.wait(1)
        release.set()
        _wait_for(
            lambda: (
                composed.transcribe_controller.snapshot.availability
                is TranscribeAvailability.AVAILABLE
            )
        )
    finally:
        release.set()
        composed.close()
