"""Optional PySide6 shell entrypoint."""

from __future__ import annotations

import sys
from collections.abc import Callable
from time import monotonic
from typing import Protocol

from voiceink_win.desktop_composition import DesktopComposition, build_desktop_composition
from voiceink_win.domain import TranscribeAvailability

PACKAGE_SMOKE_TIMEOUT_SECONDS = 120


class _Window(Protocol):
    def show(self) -> None: ...


def _run_session(
    composition: DesktopComposition,
    create_window: Callable[[], _Window],
    event_loop: Callable[[], int],
    after_show: Callable[[_Window], None] | None = None,
    cleanup_window: Callable[[_Window], None] | None = None,
) -> int:
    window: _Window | None = None
    try:
        window = create_window()
        window.show()
        if after_show is not None:
            after_show(window)
        result = event_loop()
    except BaseException:
        # Window cleanup must not replace the original session failure.
        try:
            if window is not None and cleanup_window is not None:
                cleanup_window(window)
        except BaseException:
            pass
        try:
            composition.close()
        except BaseException:
            pass
        raise
    else:
        try:
            if window is not None and cleanup_window is not None:
                cleanup_window(window)
        finally:
            composition.close()
        return result


def main(*, smoke: bool = False, package_smoke: bool = False) -> int:
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
    color_scheme_changed = getattr(application.styleHints(), "colorSchemeChanged", None)

    def create_window() -> MainWindow:
        window = MainWindow(
            composition.controller,
            theme=theme,
            transcribe_controller=composition.transcribe_controller,
            persistence=getattr(composition, "persistence", None),
            artifact_cleanup=getattr(composition, "artifact_cleanup", None),
            history_deletion=getattr(composition, "history_deletion", None),
        )
        if color_scheme_changed is not None:
            window.connect_theme_signal(
                color_scheme_changed,
                lambda *_: window.apply_theme(theme_for(detect_system_theme(application))),
            )
        return window

    def after_show(_window: _Window) -> None:
        if package_smoke:
            deadline = monotonic() + PACKAGE_SMOKE_TIMEOUT_SECONDS

            def wait_for_backend() -> None:
                availability = composition.transcribe_controller.snapshot.availability
                if availability is TranscribeAvailability.AVAILABLE:
                    application.quit()
                elif availability is TranscribeAvailability.UNAVAILABLE or monotonic() >= deadline:
                    application.exit(1)
                else:
                    QTimer.singleShot(100, wait_for_backend)

            QTimer.singleShot(0, wait_for_backend)
        elif smoke:
            QTimer.singleShot(100, application.quit)

    return _run_session(
        composition,
        create_window,
        application.exec,
        after_show,
        cleanup_window=lambda window: window.dispose(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
