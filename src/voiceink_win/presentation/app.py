"""Optional PySide6 shell entrypoint."""

from __future__ import annotations

import sys
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError
from threading import Lock
from time import monotonic
from typing import Protocol

from voiceink_win.desktop_composition import (
    DesktopComposition,
    build_desktop_composition,
    build_package_smoke_desktop_composition,
)
from voiceink_win.domain import TranscribeAvailability
from voiceink_win.infrastructure import create_instance_lease

from .tray_lifecycle import TrayLifecycleCoordinator

PACKAGE_SMOKE_TIMEOUT_SECONDS = 300
PACKAGE_SMOKE_READINESS_TIMEOUT_SECONDS = 240.0


class _Window(Protocol):
    def show(self) -> None: ...


def _run_session(
    composition: DesktopComposition,
    create_window: Callable[[], _Window],
    event_loop: Callable[[], int],
    after_show: Callable[[_Window], None] | None = None,
    cleanup_window: Callable[[_Window], None] | None = None,
    close_composition: Callable[[], None] | None = None,
) -> int:
    close = composition.close if close_composition is None else close_composition
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
            close()
        except BaseException:
            pass
        raise
    else:
        try:
            if window is not None and cleanup_window is not None:
                cleanup_window(window)
        finally:
            close()
        return result


def main(*, smoke: bool = False, package_smoke: bool = False) -> int:
    try:
        from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtWidgets import QApplication
    except ImportError as error:
        raise SystemExit(
            "PySide6 is optional. Install the GUI extra with `pip install -e '.[gui]'`."
        ) from error

    from .app_icon import application_icon, set_windows_app_user_model_id

    set_windows_app_user_model_id()
    application = QApplication.instance() or QApplication(sys.argv)
    application.setQuitOnLastWindowClosed(False)
    application.setApplicationName("VoiceInk")
    application.setWindowIcon(application_icon())
    from .theme import detect_system_theme, theme_for

    theme = theme_for(detect_system_theme(application))
    application.setStyle("Fusion")
    for family in ("Segoe UI", "SF Pro Text", "Arial"):
        if QFontDatabase.hasFamily(family):
            application.setFont(QFont(family))
            break

    from .main_window import MainWindow

    class _RecorderAvailabilityBridge(QObject):
        ready = Signal()

    class _DispatchRequest:
        def __init__(self, operation) -> None:
            self.operation = operation
            self.result = Future()
            self._lock = Lock()
            self._claimed = False
            self._cancelled = False

        def claim(self) -> bool:
            with self._lock:
                if self._cancelled:
                    return False
                self._claimed = True
                return True

        def cancel(self) -> bool:
            with self._lock:
                if self._claimed:
                    return False
                self._cancelled = True
                return True

        def abandon(self) -> None:
            with self._lock:
                self._cancelled = True

    class _QtOperationBridge(QObject):
        requested = Signal(object)

        def __init__(self) -> None:
            super().__init__()
            self.requested.connect(self._execute, Qt.ConnectionType.QueuedConnection)

        def dispatch(self, operation):
            if QThread.currentThread() == self.thread():
                return operation()
            request = _DispatchRequest(operation)
            self.requested.emit(request)
            try:
                return request.result.result(timeout=5.0)
            except TimeoutError:
                if request.cancel():
                    request.abandon()
                    raise
                try:
                    return request.result.result(timeout=5.0)
                except TimeoutError:
                    request.abandon()
                    raise

        __call__ = dispatch

        def _execute(self, payload) -> None:
            if not payload.claim():
                return
            try:
                payload.result.set_result(payload.operation())
            except BaseException as error:
                payload.result.set_exception(error)

        def invalidate(self) -> None:
            self.requested.disconnect(self._execute)

    lease = create_instance_lease()
    if not lease.acquire():
        return 0
    try:
        composition = (
            build_package_smoke_desktop_composition(PACKAGE_SMOKE_READINESS_TIMEOUT_SECONDS)
            if package_smoke
            else build_desktop_composition()
        )
    except BaseException:
        lease.release()
        raise
    global_shortcut = composition.global_shortcut
    operation_bridge = _QtOperationBridge()
    composition.global_hotkey_settings.set_dispatcher(operation_bridge)
    recorder_bridge = _RecorderAvailabilityBridge()
    recorder_bridge.ready.connect(
        global_shortcut.register,
        Qt.ConnectionType.QueuedConnection,
    )
    recorder_controller = getattr(composition, "recorder", None)
    if recorder_controller is not None:
        recorder_controller.subscribe(lambda _snapshot: recorder_bridge.ready.emit())
    color_scheme_changed = getattr(application.styleHints(), "colorSchemeChanged", None)

    def create_window() -> MainWindow:
        window = MainWindow(
            composition.controller,
            theme=theme,
            transcribe_controller=composition.transcribe_controller,
            persistence=getattr(composition, "persistence", None),
            artifact_cleanup=getattr(composition, "artifact_cleanup", None),
            history_deletion=getattr(composition, "history_deletion", None),
            media_actions=getattr(composition, "media_actions", None),
            global_shortcut=composition.global_shortcut,
            global_hotkey_settings=composition.global_hotkey_settings,
            recorder_controller=recorder_controller,
        )
        window.setWindowIcon(application.windowIcon())
        if color_scheme_changed is not None:
            window.connect_theme_signal(
                color_scheme_changed,
                lambda *_: window.apply_system_theme(detect_system_theme(application)),
            )
        return window

    def after_show(_window: _Window) -> None:
        if package_smoke:
            deadline = monotonic() + PACKAGE_SMOKE_TIMEOUT_SECONDS

            def wait_for_backend() -> None:
                now = monotonic()
                availability = composition.transcribe_controller.snapshot.availability
                if now >= deadline or availability is TranscribeAvailability.UNAVAILABLE:
                    application.exit(1)
                elif availability is TranscribeAvailability.AVAILABLE:
                    application.quit()
                else:
                    QTimer.singleShot(100, wait_for_backend)

            QTimer.singleShot(0, wait_for_backend)
        elif smoke:
            QTimer.singleShot(100, application.quit)

    coordinator = TrayLifecycleCoordinator(
        application,
        composition,
        create_window,
        global_shortcut,
        lease,
        icon=application.windowIcon(),
        after_show=after_show,
    )
    return coordinator.run(application.exec)


if __name__ == "__main__":
    raise SystemExit(main())
