from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="PySide6 is required for overlay tests")

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QApplication, QWidget

from voiceink_win.application import ShellController
from voiceink_win.domain import ACTIVE_RECORDER_STATES, RecorderSnapshot, RecorderState
from voiceink_win.presentation.main_window import FloatingRecorderWindow, MainWindow
from voiceink_win.presentation.widgets import WaveformWidget


class RecorderStub:
    def __init__(self) -> None:
        self.snapshot = RecorderSnapshot()
        self.listeners = []
        self.cancel_calls = 0
        self.dismiss_calls = 0
        self.closed = False

    def subscribe(self, listener):
        self.listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self.listeners:
                self.listeners.remove(listener)

        return unsubscribe

    def start(self) -> bool:
        return True

    def stop(self) -> bool:
        return True

    def cancel(self) -> bool:
        self.cancel_calls += 1
        return True

    def dismiss(self) -> bool:
        self.dismiss_calls += 1
        return True

    def close(self, deadline=0.0) -> None:
        del deadline
        self.closed = True

    def publish(self, snapshot: RecorderSnapshot) -> None:
        self.snapshot = snapshot
        for listener in tuple(self.listeners):
            listener(snapshot)


@pytest.fixture(scope="module")
def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_waveform_timer_is_idle_without_sound_and_runs_only_when_sounding(application) -> None:
    waveform = WaveformWidget()
    try:
        waveform.set_active(False)
        assert waveform._timer is None
        waveform.set_active(True)
        assert waveform._timer is not None
        assert waveform._timer.isActive()
        waveform.set_active(False)
        assert not waveform._timer.isActive()
    finally:
        waveform.deleteLater()
        application.processEvents()


def test_overlay_is_independent_topmost_fixed_size_and_clamped(application) -> None:
    stub = RecorderStub()
    anchor = QWidget()
    overlay = FloatingRecorderWindow(stub)
    anchor.resize(100, 100)
    anchor.show()
    overlay.show_near(anchor)
    application.processEvents()
    try:
        flags = overlay.windowFlags()
        assert flags & Qt.WindowType.Tool
        assert flags & Qt.WindowType.FramelessWindowHint
        assert flags & Qt.WindowType.WindowStaysOnTopHint
        assert overlay.parentWidget() is None
        assert overlay.size() == QSize(300, 92)
        screen = anchor.screen()
        assert screen is not None
        assert screen.availableGeometry().contains(overlay.frameGeometry())
        anchor.hide()
        application.processEvents()
        assert overlay.isVisible()
    finally:
        overlay.close()
        anchor.close()
        application.processEvents()


def test_close_button_cancels_active_and_dismisses_idle(application) -> None:
    stub = RecorderStub()
    overlay = FloatingRecorderWindow(stub)
    overlay.show()
    application.processEvents()
    try:
        stub.publish(RecorderSnapshot(RecorderState.RECORDING_SILENT))
        application.processEvents()
        overlay._close_button.click()
        assert stub.cancel_calls == 1
        assert overlay.isVisible()

        stub.publish(RecorderSnapshot(RecorderState.IDLE))
        overlay._close_button.click()
        assert stub.dismiss_calls == 1
        assert not overlay.isVisible()
    finally:
        overlay.dispose()
        application.processEvents()


def test_main_window_hiding_does_not_hide_recorder_overlay(application) -> None:
    stub = RecorderStub()
    window = MainWindow(ShellController.unavailable(), recorder_controller=stub)
    window.show()
    window._recorder.show_near(window)
    application.processEvents()
    try:
        window.hide()
        application.processEvents()
        assert window._recorder.isVisible()
        assert window._recorder._record_button.isEnabled() is False
        assert stub.snapshot.state is RecorderState.UNAVAILABLE
        assert ACTIVE_RECORDER_STATES
    finally:
        window.close()
        application.processEvents()
