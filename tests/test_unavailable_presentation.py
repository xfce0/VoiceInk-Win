from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from voiceink_win.presentation.main_window import MainWindow


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

    rendered_text = " ".join(
        label.text() for label in window.findChildren(type(window._state_pill))
    )
    assert "Ready" not in rendered_text
    assert "Transcript ready" not in rendered_text
    assert "Start recording" not in rendered_text

    window.close()
