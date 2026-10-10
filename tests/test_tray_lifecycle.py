from __future__ import annotations

import pytest

from voiceink_win.infrastructure import NoopInstanceLease
from voiceink_win.presentation.tray_lifecycle import (
    TrayLifecycleCoordinator,
    TrayLifecycleState,
)


class _Signal:
    def __init__(self) -> None:
        self.callbacks = []

    def connect(self, callback) -> None:
        self.callbacks.append(callback)

    def emit(self) -> None:
        for callback in tuple(self.callbacks):
            callback()


class _Application:
    def __init__(self) -> None:
        self.aboutToQuit = _Signal()
        self.quit_on_last_window_closed = True
        self.quit_calls = 0

    def setQuitOnLastWindowClosed(self, quit: bool) -> None:
        self.quit_on_last_window_closed = quit

    def quit(self) -> None:
        self.quit_calls += 1
        self.aboutToQuit.emit()


class _Event:
    def __init__(self) -> None:
        self.accepted = 0
        self.ignored = 0

    def accept(self) -> None:
        self.accepted += 1

    def ignore(self) -> None:
        self.ignored += 1


class _Window:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.close_handler = None
        self.show_calls = 0
        self.hide_calls = 0
        self.raise_calls = 0
        self.activate_calls = 0
        self.dispose_calls = 0

    def set_close_handler(self, handler) -> None:
        self.close_handler = handler

    def show(self) -> None:
        self.show_calls += 1

    def hide(self) -> None:
        self.hide_calls += 1

    def raise_(self) -> None:
        self.raise_calls += 1

    def activateWindow(self) -> None:
        self.activate_calls += 1

    def dispose(self) -> None:
        self.dispose_calls += 1
        self.events.append("window.dispose")

    def close(self) -> _Event:
        event = _Event()
        assert self.close_handler is not None
        self.close_handler(event)
        return event


class _Shortcut:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.register_calls = 0
        self.unregister_calls = 0

    def register(self) -> None:
        self.register_calls += 1

    def unregister(self) -> None:
        self.unregister_calls += 1
        self.events.append("shortcut.unregister")


class _Composition:
    def __init__(self, events: list[str], close_error: Exception | None = None) -> None:
        self.events = events
        self.close_calls = 0
        self.close_error = close_error

    def close(self) -> None:
        self.close_calls += 1
        self.events.append("composition.close")
        if self.close_error is not None:
            raise self.close_error


class _Tray:
    def __init__(self, events: list[str], on_show, on_quit) -> None:
        self.events = events
        self.on_show = on_show
        self.on_quit = on_quit
        self.show_calls = 0
        self.hide_calls = 0
        self.dispose_calls = 0

    def show(self) -> None:
        self.show_calls += 1

    def hide(self) -> None:
        self.hide_calls += 1

    def dispose(self) -> None:
        self.dispose_calls += 1
        self.events.append("tray.dispose")


class _Lease(NoopInstanceLease):
    def __init__(self, events: list[str], acquired: bool = True) -> None:
        super().__init__()
        self.events = events
        self.acquired = acquired
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self) -> bool:
        self.acquire_calls += 1
        if not self.acquired:
            return False
        return super().acquire()

    def release(self) -> None:
        self.release_calls += 1
        self.events.append("lease.release")
        super().release()


def _coordinator(*, tray_available: bool = True, close_error: Exception | None = None):
    events: list[str] = []
    application = _Application()
    window = _Window(events)
    shortcut = _Shortcut(events)
    composition = _Composition(events, close_error)
    lease = _Lease(events)
    tray_holder: list[_Tray] = []

    def create_tray(_icon, on_show, on_quit, _on_activate) -> _Tray:
        tray = _Tray(events, on_show, on_quit)
        tray_holder.append(tray)
        return tray

    coordinator = TrayLifecycleCoordinator(
        application,
        composition,
        lambda: window,
        shortcut,
        lease,
        create_tray=create_tray,
        tray_available=lambda: tray_available,
    )
    return coordinator, application, window, shortcut, composition, lease, tray_holder, events


def test_close_hides_same_window_and_keeps_background_resources_alive() -> None:
    coordinator, application, window, shortcut, composition, _, _, _ = _coordinator()

    assert coordinator.start()
    assert not application.quit_on_last_window_closed
    event = window.close()

    assert event.ignored == 1
    assert event.accepted == 0
    assert coordinator.state is TrayLifecycleState.HIDDEN
    assert coordinator.window is window
    assert window.hide_calls == 1
    assert window.dispose_calls == 0
    assert composition.close_calls == 0
    assert shortcut.register_calls == 1
    assert shortcut.unregister_calls == 0
    assert application.quit_calls == 0

    coordinator.show_window()

    assert coordinator.state is TrayLifecycleState.VISIBLE
    assert coordinator.window is window
    assert window.show_calls == 2
    assert window.raise_calls == 1
    assert window.activate_calls == 1


@pytest.mark.parametrize("hide_first", [False, True])
def test_tray_quit_from_visible_or_hidden_cleans_everything_once(hide_first: bool) -> None:
    coordinator, application, window, shortcut, composition, lease, trays, events = _coordinator()

    assert coordinator.start()
    if hide_first:
        window.close()
    trays[0].on_quit()
    trays[0].on_quit()
    application.aboutToQuit.emit()
    coordinator.shutdown()

    assert coordinator.state is TrayLifecycleState.TERMINATED
    assert shortcut.unregister_calls == 1
    assert window.dispose_calls == 1
    assert trays[0].dispose_calls == 1
    assert composition.close_calls == 1
    assert lease.release_calls == 1
    assert events == [
        "shortcut.unregister",
        "window.dispose",
        "tray.dispose",
        "composition.close",
        "lease.release",
    ]


def test_event_loop_error_is_preserved_when_cleanup_also_fails() -> None:
    session_error = ValueError("event loop failed")
    cleanup_error = RuntimeError("composition close failed")
    coordinator, _, _, _, composition, lease, _, _ = _coordinator(close_error=cleanup_error)

    with pytest.raises(ValueError) as raised:
        coordinator.run(lambda: (_ for _ in ()).throw(session_error))

    assert raised.value is session_error
    assert composition.close_calls == 1
    assert lease.release_calls == 1


def test_missing_tray_takes_safe_terminal_path_on_window_close() -> None:
    coordinator, application, window, _, composition, lease, trays, _ = _coordinator(
        tray_available=False
    )

    assert coordinator.start()
    event = window.close()

    assert event.accepted == 1
    assert event.ignored == 0
    assert not trays
    assert application.quit_calls == 1
    assert coordinator.state is TrayLifecycleState.TERMINATED
    assert composition.close_calls == 1
    assert lease.release_calls == 1


def test_rejected_instance_does_not_create_a_window_or_background_session() -> None:
    events: list[str] = []
    application = _Application()
    lease = _Lease(events, acquired=False)
    created = False

    def create_window():
        nonlocal created
        created = True
        raise AssertionError("window must not be created")

    coordinator = TrayLifecycleCoordinator(
        application,
        _Composition(events),
        create_window,
        _Shortcut(events),
        lease,
        tray_available=lambda: True,
    )

    assert coordinator.start() is False
    assert coordinator.state is TrayLifecycleState.REJECTED
    assert not created
    assert lease.release_calls == 0
