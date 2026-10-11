"""Session ownership for the Windows tray shell."""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Protocol

from voiceink_win.infrastructure.instance_lease import InstanceLease

logger = logging.getLogger(__name__)


class TrayLifecycleState(StrEnum):
    STARTING = "starting"
    VISIBLE = "visible"
    HIDDEN = "hidden"
    SHUTDOWN_REQUESTED = "shutdown_requested"
    SHUTTING_DOWN = "shutting_down"
    TERMINATED = "terminated"
    REJECTED = "rejected"


class _Signal(Protocol):
    def connect(self, callback: Callable[[], None]) -> Any: ...


class _Application(Protocol):
    aboutToQuit: _Signal

    def setQuitOnLastWindowClosed(self, quit: bool) -> None: ...

    def quit(self) -> None: ...


class _CloseEvent(Protocol):
    def accept(self) -> None: ...

    def ignore(self) -> None: ...


class _Window(Protocol):
    def set_close_handler(self, handler: Callable[[_CloseEvent], None]) -> None: ...

    def show(self) -> None: ...

    def hide(self) -> None: ...

    def raise_(self) -> None: ...

    def activateWindow(self) -> None: ...

    def dispose(self) -> None: ...


class _ShortcutService(Protocol):
    def register(self) -> Any: ...

    def unregister(self) -> Any: ...


class _TraySurface(Protocol):
    def show(self) -> None: ...

    def hide(self) -> None: ...

    def dispose(self) -> None: ...


class QtTraySurface:
    """Qt adapter kept behind a small lifecycle-facing surface."""

    @staticmethod
    def is_available() -> bool:
        from PySide6.QtWidgets import QSystemTrayIcon

        return QSystemTrayIcon.isSystemTrayAvailable()

    def __init__(
        self,
        icon: Any,
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
        on_activate: Callable[[], None],
    ) -> None:
        from PySide6.QtWidgets import QAction, QMenu, QSystemTrayIcon

        self._tray = QSystemTrayIcon(icon)
        self._menu = QMenu()
        show_action = QAction("Show VoiceInk", self._menu)
        show_action.triggered.connect(lambda _checked=False: on_show())
        quit_action = QAction("Quit VoiceInk", self._menu)
        quit_action.triggered.connect(lambda _checked=False: on_quit())
        self._menu.addAction(show_action)
        self._menu.addSeparator()
        self._menu.addAction(quit_action)
        self._tray.setContextMenu(self._menu)

        def activate(reason) -> None:
            if reason != QSystemTrayIcon.ActivationReason.Context:
                on_activate()

        self._tray.activated.connect(activate)

    def show(self) -> None:
        self._tray.show()

    def hide(self) -> None:
        self._tray.hide()

    def dispose(self) -> None:
        self._tray.hide()
        self._tray.setContextMenu(None)
        self._menu.deleteLater()
        self._tray.deleteLater()


class TrayLifecycleCoordinator:
    """Own the one interactive session and its terminal cleanup ordering."""

    def __init__(
        self,
        application: _Application,
        composition: Any,
        create_window: Callable[[], _Window],
        shortcut: _ShortcutService,
        lease: InstanceLease,
        *,
        icon: Any = None,
        create_tray: Callable[..., _TraySurface] = QtTraySurface,
        tray_available: Callable[[], bool] = QtTraySurface.is_available,
        after_show: Callable[[_Window], None] | None = None,
    ) -> None:
        self._application = application
        self._composition = composition
        self._create_window = create_window
        self._shortcut = shortcut
        self._lease = lease
        self._icon = icon
        self._create_tray = create_tray
        self._tray_available = tray_available
        self._after_show = after_show
        self._window: _Window | None = None
        self._tray: _TraySurface | None = None
        self._lease_acquired = False
        self._about_to_quit_connected = False
        self._shutdown_intent = False
        self._state = TrayLifecycleState.STARTING

    @property
    def state(self) -> TrayLifecycleState:
        return self._state

    @property
    def window(self) -> _Window | None:
        return self._window

    @property
    def tray(self) -> _TraySurface | None:
        return self._tray

    def run(self, event_loop: Callable[[], int]) -> int:
        try:
            if not self.start():
                return 0
            result = event_loop()
        except BaseException:
            self.shutdown()
            raise
        self.shutdown()
        return result

    def start(self) -> bool:
        if self._state is not TrayLifecycleState.STARTING:
            return self._state is not TrayLifecycleState.REJECTED
        if not self._lease.acquire():
            self._state = TrayLifecycleState.REJECTED
            return False
        self._lease_acquired = True
        self._application.setQuitOnLastWindowClosed(False)
        self._application.aboutToQuit.connect(self._on_about_to_quit)
        self._about_to_quit_connected = True
        try:
            self._shortcut.register()
            self._window = self._create_window()
            self._window.set_close_handler(self._on_window_close)
            if self._tray_available():
                self._tray = self._create_tray(
                    self._icon, self.show_window, self.request_shutdown, self.show_window
                )
                self._tray.show()
            self._window.show()
            self._state = TrayLifecycleState.VISIBLE
            if self._after_show is not None:
                self._after_show(self._window)
            return True
        except BaseException:
            self.shutdown()
            raise

    def show_window(self) -> None:
        if self._state not in (TrayLifecycleState.VISIBLE, TrayLifecycleState.HIDDEN):
            return
        window = self._window
        if window is None:
            return
        window.show()
        window.raise_()
        window.activateWindow()
        self._state = TrayLifecycleState.VISIBLE

    def request_shutdown(self) -> None:
        if self._state in (
            TrayLifecycleState.SHUTDOWN_REQUESTED,
            TrayLifecycleState.SHUTTING_DOWN,
            TrayLifecycleState.TERMINATED,
            TrayLifecycleState.REJECTED,
        ):
            return
        self._shutdown_intent = True
        self._state = TrayLifecycleState.SHUTDOWN_REQUESTED
        self._application.quit()

    def shutdown(self) -> None:
        if self._state in (TrayLifecycleState.SHUTTING_DOWN, TrayLifecycleState.TERMINATED):
            return
        if self._state is TrayLifecycleState.REJECTED:
            return
        self._shutdown_intent = True
        self._state = TrayLifecycleState.SHUTTING_DOWN
        self._cleanup(self._shortcut.unregister, "global shortcut unregister")
        self._cleanup(self._dispose_window, "main window disposal")
        self._cleanup(self._dispose_tray, "tray disposal")
        self._cleanup(self._composition.close, "desktop composition close")
        if self._lease_acquired:
            self._cleanup(self._release_lease, "instance lease release")
        self._state = TrayLifecycleState.TERMINATED

    def _on_window_close(self, event: _CloseEvent) -> None:
        if self._shutdown_intent or self._tray is None:
            self.request_shutdown()
            event.accept()
            return
        event.ignore()
        if self._window is not None:
            self._window.hide()
        self._state = TrayLifecycleState.HIDDEN

    def _on_about_to_quit(self) -> None:
        if self._state is TrayLifecycleState.VISIBLE:
            self._state = TrayLifecycleState.SHUTDOWN_REQUESTED
        self.shutdown()

    def _dispose_window(self) -> None:
        if self._window is not None:
            self._window.dispose()

    def _dispose_tray(self) -> None:
        if self._tray is not None:
            self._tray.dispose()

    def _release_lease(self) -> None:
        self._lease.release()
        self._lease_acquired = False

    @staticmethod
    def _cleanup(action: Callable[[], Any], label: str) -> None:
        try:
            action()
        except Exception:
            logger.exception("failed during %s", label)


__all__ = ["QtTraySurface", "TrayLifecycleCoordinator", "TrayLifecycleState"]
