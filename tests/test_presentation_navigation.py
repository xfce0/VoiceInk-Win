from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QFrame, QPushButton
except ImportError:
    if os.environ.get("VOICEINK_GUI_TESTS") == "1":
        raise
    pytest.skip("PySide6 is required for presentation tests", allow_module_level=True)

from tests.support.fake_shell import FakeShellBackend
from voiceink_win.application import ShellController
from voiceink_win.domain import TranscriptResult
from voiceink_win.presentation.icon_registry import SIDEBAR_ITEMS
from voiceink_win.presentation.main_window import (
    SIDEBAR_ICON_SIZE,
    SIDEBAR_ITEM_HEIGHT,
    SIDEBAR_WIDTH,
    MainWindow,
)
from voiceink_win.presentation.qt_icons import (
    ICON_SCALE_FACTORS,
    _render,
    sidebar_icon,
    sidebar_svg,
)
from voiceink_win.presentation.theme import DARK_THEME, LIGHT_THEME, stylesheet_for


@pytest.fixture(scope="module")
def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_sidebar_is_expanded_with_visible_labels_at_minimum_window(
    application: QApplication,
) -> None:
    window = MainWindow(ShellController.unavailable())
    window.resize(window.minimumSize())
    window.show()
    application.processEvents()
    sidebar = window.findChild(QFrame, "sidebar")
    buttons = window.findChildren(QPushButton, "navButton")

    assert sidebar is not None
    assert window.size() == window.minimumSize()
    assert sidebar.width() == SIDEBAR_WIDTH
    assert [button.text() for button in buttons] == [item.label for item in SIDEBAR_ITEMS]
    assert all(button.height() >= SIDEBAR_ITEM_HEIGHT for button in buttons)
    assert all(
        button.iconSize() == QSize(SIDEBAR_ICON_SIZE, SIDEBAR_ICON_SIZE) for button in buttons
    )
    assert buttons[0].isChecked()
    assert all(
        button.isEnabled() is item.enabled
        for button, item in zip(buttons, SIDEBAR_ITEMS, strict=True)
    )
    assert window.minimumWidth() >= SIDEBAR_WIDTH + 600

    window.close()


def test_sidebar_icons_are_repository_svg_rendered_and_high_dpi(application: QApplication) -> None:
    del application
    item = SIDEBAR_ITEMS[0]
    icon = sidebar_icon(item, SIDEBAR_ICON_SIZE)

    assert not icon.isNull()
    assert ICON_SCALE_FACTORS == (1, 1.25, 1.5, 2, 2.5, 3)
    assert icon.pixmap(QSize(SIDEBAR_ICON_SIZE, SIDEBAR_ICON_SIZE)).isNull() is False
    for scale in (1.25, 1.5, 2, 2.5):
        high_dpi = _render(item, SIDEBAR_ICON_SIZE, scale)
        physical_size = round(SIDEBAR_ICON_SIZE * scale)
        assert high_dpi.size() == QSize(physical_size, physical_size)
        assert high_dpi.devicePixelRatio() == scale
        image = high_dpi.toImage()
        assert any(
            image.pixelColor(x, y).alpha() > 0
            for x in range(image.width())
            for y in range(image.height())
        )
    svg = sidebar_svg(item, SIDEBAR_ICON_SIZE)
    assert f'fill="{item.tile_color}"' in svg
    assert f'stroke="{item.icon_foreground}"' in svg
    for mode in (QIcon.Mode.Normal, QIcon.Mode.Active, QIcon.Mode.Selected, QIcon.Mode.Disabled):
        for state in (QIcon.State.Off, QIcon.State.On):
            assert not icon.pixmap(
                QSize(SIDEBAR_ICON_SIZE, SIDEBAR_ICON_SIZE), mode, state
            ).isNull()


def test_disabled_sidebar_icon_uses_muted_tile_and_foreground(application: QApplication) -> None:
    window = MainWindow(ShellController.unavailable())
    window.show()
    application.processEvents()
    buttons = window.findChildren(QPushButton, "navButton")

    for button, item in zip(buttons, SIDEBAR_ITEMS, strict=True):
        normal = button.icon().pixmap(
            QSize(SIDEBAR_ICON_SIZE, SIDEBAR_ICON_SIZE), QIcon.Mode.Normal
        )
        disabled = button.icon().pixmap(
            QSize(SIDEBAR_ICON_SIZE, SIDEBAR_ICON_SIZE), QIcon.Mode.Disabled
        )

        assert button.isEnabled() is item.enabled
        assert not normal.isNull()
        assert not disabled.isNull()
        assert normal.cacheKey() != disabled.cacheKey()

    assert f'fill="{SIDEBAR_ITEMS[1].tile_color}"' in sidebar_svg(SIDEBAR_ITEMS[1])
    assert f'fill="{SIDEBAR_ITEMS[1].tile_color}"' not in sidebar_svg(
        SIDEBAR_ITEMS[1], disabled=True
    )
    assert f'fill="none" stroke="{SIDEBAR_ITEMS[1].icon_foreground}"' not in sidebar_svg(
        SIDEBAR_ITEMS[1], disabled=True
    )

    window.close()


def test_main_window_clears_transcript_content_when_recording_again(
    application: QApplication,
) -> None:
    controller = ShellController(
        FakeShellBackend(TranscriptResult("A finished thought.", duration=1.0))
    )
    window = MainWindow(controller)
    window.show()
    application.processEvents()

    assert controller.start_recording()
    assert controller.stop_recording()
    assert controller.complete_processing()
    assert window._transcript_body.objectName() == "transcriptCard"
    assert window._transcript_text.text() == "A finished thought."

    assert controller.start_recording()
    assert window._transcript_body.objectName() == "emptyCard"
    assert window._transcript_metadata.text() == "Recording in progress"
    assert window._transcript_text.text() == (
        "Your transcript will appear here when recording is complete."
    )

    assert controller.stop_recording()
    assert window._transcript_body.objectName() == "emptyCard"
    assert window._transcript_metadata.text() == "Transcription in progress"
    assert window._transcript_text.text() == "VoiceInk is preparing your transcript."

    window.close()


def test_main_window_disconnects_theme_callback_during_dispose() -> None:
    class SignalSpy:
        def __init__(self) -> None:
            self.callbacks = []

        def connect(self, callback) -> None:
            self.callbacks.append(callback)

        def disconnect(self, callback) -> None:
            self.callbacks.remove(callback)

        def emit(self) -> None:
            for callback in tuple(self.callbacks):
                callback()

    window = MainWindow(ShellController.unavailable())
    signal = SignalSpy()
    calls = []
    window.connect_theme_signal(signal, lambda: calls.append("theme"))

    signal.emit()
    assert calls == ["theme"]
    assert len(signal.callbacks) == 1

    window.dispose()
    window.dispose()
    signal.emit()

    assert calls == ["theme"]
    assert signal.callbacks == []
    window.close()


def test_sidebar_styles_preserve_state_specific_readability_for_both_themes() -> None:
    for theme in (LIGHT_THEME, DARK_THEME):
        stylesheet = stylesheet_for(theme)
        assert "QPushButton#navButton:hover" in stylesheet
        assert "QPushButton#navButton:checked" in stylesheet
        assert "QPushButton#navButton:checked:hover" in stylesheet
        assert "QPushButton#navButton:disabled" in stylesheet
        assert "QPushButton#navButton:disabled:hover" in stylesheet
        assert f"background: {theme.nav_selected};" in stylesheet
        assert f"color: {theme.disabled};" in stylesheet
        disabled_hover = stylesheet.split("QPushButton#navButton:disabled:hover", 1)[1].split(
            "}", 1
        )[0]
        assert "background: transparent;" in disabled_hover
        assert theme.nav_hover not in disabled_hover
