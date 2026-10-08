"""Optional PySide6 shell entrypoint."""

from __future__ import annotations

import sys

from voiceink_win.application import ShellController
from voiceink_win.infrastructure import FakeShellBackend


def main(*, smoke: bool = False) -> int:
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtWidgets import QApplication
    except ImportError as error:
        raise SystemExit(
            "PySide6 is optional. Install the GUI extra with `pip install -e '.[gui]'`."
        ) from error

    application = QApplication(sys.argv)
    application.setApplicationName("VoiceInk")
    from .theme import detect_system_theme, theme_for

    theme = theme_for(detect_system_theme(application))
    application.setStyle("Fusion")
    for family in ("Segoe UI", "SF Pro Text", "Arial"):
        if QFontDatabase.hasFamily(family):
            application.setFont(QFont(family))
            break

    from .main_window import MainWindow

    controller = ShellController(FakeShellBackend())
    window = MainWindow(controller, theme=theme)
    color_scheme_changed = getattr(application.styleHints(), "colorSchemeChanged", None)
    if color_scheme_changed is not None:
        color_scheme_changed.connect(
            lambda *_: window.apply_theme(theme_for(detect_system_theme(application)))
        )
    window.show()
    if smoke:
        QTimer.singleShot(100, application.quit)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
