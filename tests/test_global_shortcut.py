from __future__ import annotations

from concurrent.futures import Future

import pytest

from tests.support.fake_shell import FakeShellBackend
from voiceink_win.application import (
    GlobalHotkeySettingsService,
    GlobalToggleShortcutService,
    ShellController,
    ShortcutAvailability,
)
from voiceink_win.domain import (
    GlobalShortcut,
    Settings,
    ShellState,
    ShortcutConflictError,
    ShortcutUnavailableError,
    ShortcutValidationError,
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


class _Persistence:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.changes = []

    def update_settings(self, changes):
        self.changes.append(changes)
        future = Future()
        if self.error is None:
            future.set_result(Settings(hotkeys=changes))
        else:
            future.set_exception(self.error)
        return future


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
    status = service.update_shortcut("Ctrl+Alt+Space")

    assert status.availability is ShortcutAvailability.REGISTERED
    assert [shortcut.value for shortcut in port.shortcuts] == ["Ctrl+Space", "Ctrl+Alt+Space"]
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
    assert _windows_hotkey_parts(GlobalShortcut()) == (0x0002, 0x20)

    with pytest.raises(ShortcutValidationError, match="requires a modifier"):
        GlobalShortcut("Space")


def test_global_shortcut_canonicalizes_modifier_order_and_key_aliases() -> None:
    assert GlobalShortcut("alt+control+space").value == "Ctrl+Alt+Space"
    assert GlobalShortcut("shift+ctrl+escape").value == "Ctrl+Shift+Escape"


@pytest.mark.parametrize("value", ["Ctrl+Ctrl+A", "A", "Ctrl+Mouse1", "Ctrl+"])
def test_global_shortcut_rejects_unsupported_chords(value: str) -> None:
    with pytest.raises(ShortcutValidationError):
        GlobalShortcut(value)


def test_hotkey_settings_persists_only_after_registration() -> None:
    port = _ShortcutPort()
    shortcut = GlobalToggleShortcutService(ShellController(FakeShellBackend()), port)
    persistence = _Persistence()
    settings = GlobalHotkeySettingsService(shortcut, persistence)

    assert settings.load(Settings()).availability is ShortcutAvailability.REGISTERED
    result = settings.update("Alt+Space").result(timeout=1)

    assert result.availability is ShortcutAvailability.REGISTERED
    assert persistence.changes == [{"hotkeys.start_stop": "Alt+Space"}]
    assert shortcut.status.shortcut.value == "Alt+Space"


def test_hotkey_settings_restores_previous_registration_when_persistence_fails() -> None:
    port = _ShortcutPort()
    shortcut = GlobalToggleShortcutService(ShellController(FakeShellBackend()), port)
    persistence = _Persistence(RuntimeError("database unavailable"))
    settings = GlobalHotkeySettingsService(shortcut, persistence)
    settings.load(Settings())

    result = settings.update("Alt+Space").result(timeout=1)

    assert result.availability is ShortcutAvailability.REGISTERED
    assert result.shortcut.value == "Ctrl+Space"
    assert shortcut.status.shortcut.value == "Ctrl+Space"
    assert [item.value for item in port.shortcuts] == [
        "Ctrl+Space",
        "Alt+Space",
        "Ctrl+Space",
    ]
