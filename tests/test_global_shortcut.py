from __future__ import annotations

import pytest

from tests.support.fake_shell import FakeShellBackend
from voiceink_win.application import (
    GlobalToggleShortcutService,
    ShellController,
    ShortcutAvailability,
)
from voiceink_win.domain import (
    GlobalShortcut,
    ShellState,
    ShortcutConflictError,
    ShortcutUnavailableError,
)
from voiceink_win.infrastructure.global_shortcut import (
    UnavailableGlobalShortcutPort,
    _windows_hotkey_parts,
)


class _Registration:
    def __init__(self) -> None:
        self.unregister_calls = 0

    def unregister(self) -> None:
        self.unregister_calls += 1


class _ShortcutPort:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.callbacks = []
        self.shortcuts = []
        self.registrations = []

    def register(self, shortcut, callback):
        if self.error is not None:
            raise self.error
        registration = _Registration()
        self.shortcuts.append(shortcut)
        self.callbacks.append(callback)
        self.registrations.append(registration)
        return registration


def test_global_shortcut_toggles_recording_through_application_port() -> None:
    controller = ShellController(FakeShellBackend())
    port = _ShortcutPort()
    service = GlobalToggleShortcutService(controller, port)

    assert service.register().availability is ShortcutAvailability.REGISTERED
    port.callbacks[0]()
    assert controller.snapshot.state is ShellState.RECORDING
    port.callbacks[0]()
    assert controller.snapshot.state is ShellState.PROCESSING


def test_global_shortcut_unregisters_exactly_once_and_is_idempotent() -> None:
    port = _ShortcutPort()
    service = GlobalToggleShortcutService(ShellController(FakeShellBackend()), port)

    service.register()
    service.unregister()
    service.unregister()

    assert port.registrations[0].unregister_calls == 1
    assert service.status.availability is ShortcutAvailability.DISABLED


@pytest.mark.parametrize(
    ("error", "availability"),
    [
        (ShortcutConflictError("already used"), ShortcutAvailability.CONFLICT),
        (ShortcutUnavailableError("not supported"), ShortcutAvailability.UNAVAILABLE),
    ],
)
def test_global_shortcut_exposes_registration_failure_without_raising(
    error: BaseException, availability: ShortcutAvailability
) -> None:
    service = GlobalToggleShortcutService(
        ShellController(FakeShellBackend()), _ShortcutPort(error=error)
    )

    status = service.register()

    assert status.availability is availability
    assert status.message == str(error)


def test_unavailable_controller_does_not_register_global_shortcut() -> None:
    port = _ShortcutPort()
    service = GlobalToggleShortcutService(ShellController.unavailable(), port)

    status = service.register()

    assert status.availability is ShortcutAvailability.UNAVAILABLE
    assert port.shortcuts == []


def test_updating_registered_shortcut_replaces_the_single_registration() -> None:
    port = _ShortcutPort()
    service = GlobalToggleShortcutService(ShellController(FakeShellBackend()), port)

    service.register()
    status = service.update_shortcut("Ctrl+Space")

    assert status.availability is ShortcutAvailability.REGISTERED
    assert [shortcut.value for shortcut in port.shortcuts] == ["Ctrl+Alt+Space", "Ctrl+Space"]
    assert port.registrations[0].unregister_calls == 1


def test_updating_shortcut_retries_after_an_initial_conflict() -> None:
    port = _ShortcutPort(error=ShortcutConflictError("already used"))
    service = GlobalToggleShortcutService(ShellController(FakeShellBackend()), port)

    assert service.register().availability is ShortcutAvailability.CONFLICT
    port.error = None

    assert service.update_shortcut("Ctrl+Space").availability is ShortcutAvailability.REGISTERED


def test_unavailable_port_is_a_safe_registration_boundary() -> None:
    service = GlobalToggleShortcutService(
        ShellController(FakeShellBackend()), UnavailableGlobalShortcutPort()
    )

    assert service.register().availability is ShortcutAvailability.UNAVAILABLE


def test_windows_shortcut_parser_supports_default_and_rejects_unmodified_keys() -> None:
    assert _windows_hotkey_parts(GlobalShortcut()) == (0x0002 | 0x0001, 0x20)

    with pytest.raises(ShortcutUnavailableError, match="requires a modifier"):
        _windows_hotkey_parts(GlobalShortcut("Space"))
