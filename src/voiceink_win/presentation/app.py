"""Optional PySide6 shell entrypoint."""

from __future__ import annotations

import sys

from voiceink_win.application import ShellController
from voiceink_win.infrastructure import FakeShellBackend


def main() -> int:
    try:
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtWidgets import QApplication
    except ImportError as error:
        raise SystemExit(
            "PySide6 is optional. Install the GUI extra with `pip install -e '.[gui]'`."
        ) from error

    application = QApplication(sys.argv)
    application.setApplicationName("VoiceInk")
    application.setStyle("Fusion")
    for family in ("Segoe UI", "SF Pro Text", "Arial"):
        if QFontDatabase.hasFamily(family):
            application.setFont(QFont(family))
            break

    from .main_window import MainWindow

    controller = ShellController(FakeShellBackend())
    window = MainWindow(controller)
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
