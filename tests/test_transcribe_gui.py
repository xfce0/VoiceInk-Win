from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.support.fake_shell import FakeShellBackend
from voiceink_win.application import ShellController, TranscribePageController
from voiceink_win.presentation.main_window import MainWindow
from voiceink_win.presentation.transcribe_page import TranscribePage

try:
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDropEvent
    from PySide6.QtWidgets import QApplication, QLabel
except ImportError:  # pragma: no cover - exercised by the dependency-free test lane
    pytestmark = pytest.mark.skip(reason="PySide6 is not installed")


@pytest.fixture
def qt_app():
    application = QApplication.instance() or QApplication([])
    yield application
    application.processEvents()


def wait_for(application, predicate) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_transcribe_destination_is_enabled_and_renders_real_page(qt_app) -> None:
    from tests.test_transcribe_application import FakeImportedMedia

    controller = TranscribePageController(FakeImportedMedia())
    window = MainWindow(ShellController(FakeShellBackend()), transcribe_controller=controller)
    window.show()
    try:
        transcribe_button = window._nav_buttons["Transcribe"]
        assert transcribe_button.isEnabled()
        transcribe_button.click()
        qt_app.processEvents()
        assert window._pages.currentIndex() == 1
        page = window._transcribe_page
        assert isinstance(page, TranscribePage)
        assert page._add_button.isEnabled()
        assert any(
            label.text().startswith("Supports WAV, MP3, M4A") for label in page.findChildren(QLabel)
        )
    finally:
        window.close()
        qt_app.processEvents()


def test_drag_drop_adds_supported_matrix_labels_without_extension_acceptance(
    qt_app, tmp_path: Path
) -> None:
    from tests.test_transcribe_application import FakeImportedMedia

    media = FakeImportedMedia()
    controller = TranscribePageController(media)
    page = TranscribePage(controller)
    page.show()
    try:
        paths = []
        for extension in (
            "wav",
            "mp3",
            "m4a",
            "aiff",
            "mp4",
            "mov",
            "aac",
            "flac",
            "caf",
            "amr",
            "ogg",
            "opus",
            "3gp",
            "webm",
        ):
            path = tmp_path / f"media.{extension}"
            path.write_bytes(b"not decoded here")
            paths.append(QUrl.fromLocalFile(str(path)))
        data = QMimeData()
        data.setUrls(paths)
        event = QDropEvent(
            QPointF(20, 20),
            Qt.DropAction.CopyAction,
            data,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        page._drop_zone.dropEvent(event)
        qt_app.processEvents()

        assert [item.source_format_hint.value for item in controller.snapshot.items] == [
            "WAV",
            "MP3",
            "M4A",
            "AIFF",
            "MP4",
            "MOV",
            "AAC",
            "FLAC",
            "CAF",
            "AMR",
            "OGG",
            "OPUS",
            "3GP",
            "WEBM",
        ]
        assert page._count_label.text() == "14 files"
    finally:
        page.dispose()
        page.close()
        qt_app.processEvents()
