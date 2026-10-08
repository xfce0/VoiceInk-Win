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

from voiceink_win.application import ShellController
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
    assert set(ICON_SCALE_FACTORS) == {1, 2, 3, 4, 5}
    assert icon.pixmap(QSize(SIDEBAR_ICON_SIZE, SIDEBAR_ICON_SIZE)).isNull() is False
    high_dpi = _render(item, SIDEBAR_ICON_SIZE, 2)
    assert high_dpi.size() == QSize(SIDEBAR_ICON_SIZE * 2, SIDEBAR_ICON_SIZE * 2)
    assert high_dpi.devicePixelRatio() == 2
    high_dpi_icon = icon.pixmap(
        QSize(SIDEBAR_ICON_SIZE, SIDEBAR_ICON_SIZE),
        2,
        QIcon.Mode.Disabled,
        QIcon.State.On,
    )
    assert high_dpi_icon.devicePixelRatio() == 2
    svg = sidebar_svg(item, SIDEBAR_ICON_SIZE)
    assert f'fill="{item.tile_color}"' in svg
    assert f'stroke="{item.icon_foreground}"' in svg
    for mode in (QIcon.Mode.Normal, QIcon.Mode.Active, QIcon.Mode.Selected, QIcon.Mode.Disabled):
        for state in (QIcon.State.Off, QIcon.State.On):
            assert not icon.pixmap(
                QSize(SIDEBAR_ICON_SIZE, SIDEBAR_ICON_SIZE), mode, state
            ).isNull()


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
