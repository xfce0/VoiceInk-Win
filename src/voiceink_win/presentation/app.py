"""Optional PySide6 shell entrypoint."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Protocol

from voiceink_win.desktop_composition import DesktopComposition, build_desktop_composition


class _Window(Protocol):
    def show(self) -> None: ...


def _run_session(
    composition: DesktopComposition,
    create_window: Callable[[], _Window],
    event_loop: Callable[[], int],
    after_show: Callable[[_Window], None] | None = None,
) -> int:
    try:
        window = create_window()
        window.show()
        if after_show is not None:
            after_show(window)
        return event_loop()
    finally:
        composition.close()


def main(*, smoke: bool = False) -> int:
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtWidgets import QApplication
    except ImportError as error:
        raise SystemExit(
            "PySide6 is optional. Install the GUI extra with `pip install -e '.[gui]'`."
        ) from error

    application = QApplication.instance() or QApplication(sys.argv)
    application.setApplicationName("VoiceInk")
    from .theme import detect_system_theme, theme_for

    theme = theme_for(detect_system_theme(application))
    application.setStyle("Fusion")
    for family in ("Segoe UI", "SF Pro Text", "Arial"):
        if QFontDatabase.hasFamily(family):
            application.setFont(QFont(family))
            break

    from .main_window import MainWindow

    composition = build_desktop_composition()

    def create_window() -> MainWindow:
        window = MainWindow(composition.controller, theme=theme)
        color_scheme_changed = getattr(application.styleHints(), "colorSchemeChanged", None)
        if color_scheme_changed is not None:
            color_scheme_changed.connect(
                lambda *_: window.apply_theme(theme_for(detect_system_theme(application)))
            )
        return window

    def after_show(_window: _Window) -> None:
        if smoke:
            QTimer.singleShot(100, application.quit)

    return _run_session(composition, create_window, application.exec, after_show)


if __name__ == "__main__":
    raise SystemExit(main())
