"""Application lifecycle for the single recording toggle shortcut."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from voiceink_win.domain import (
    DEFAULT_GLOBAL_TOGGLE_SHORTCUT,
    GlobalShortcut,
    GlobalShortcutPort,
    GlobalShortcutRegistration,
    ShellSnapshot,
    ShellState,
    ShortcutConflictError,
    ShortcutRegistrationError,
    ShortcutUnavailableError,
)


class RecordingTogglePort(Protocol):
    @property
    def snapshot(self) -> ShellSnapshot: ...

    def start_recording(self) -> bool: ...

    def stop_recording(self) -> bool: ...


class ShortcutAvailability(StrEnum):
    DISABLED = "disabled"
    REGISTERED = "registered"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class GlobalShortcutStatus:
    shortcut: GlobalShortcut
    availability: ShortcutAvailability
    message: str = ""


class GlobalToggleShortcutService:
    """Own exactly one registration and route its callback to recording state."""

    def __init__(
        self,
        controller: RecordingTogglePort,
        shortcut_port: GlobalShortcutPort,
        shortcut: GlobalShortcut | None = None,
    ) -> None:
        self._controller = controller
        self._shortcut_port = shortcut_port
        self._shortcut = shortcut or GlobalShortcut()
        self._registration: GlobalShortcutRegistration | None = None
        self._registration_requested = False
        self._status = GlobalShortcutStatus(self._shortcut, ShortcutAvailability.DISABLED)

    @property
    def status(self) -> GlobalShortcutStatus:
        return self._status

    def register(self) -> GlobalShortcutStatus:
        if self._registration is not None:
            return self._status
        self._registration_requested = True
        if self._state_value() == ShellState.UNAVAILABLE.value:
            return self._set_status(
                ShortcutAvailability.UNAVAILABLE,
                "Recording is unavailable in this build.",
            )
        try:
            registration = self._shortcut_port.register(self._shortcut, self._toggle)
        except ShortcutConflictError as error:
            return self._set_status(ShortcutAvailability.CONFLICT, str(error))
        except ShortcutUnavailableError as error:
            return self._set_status(ShortcutAvailability.UNAVAILABLE, str(error))
        except ShortcutRegistrationError as error:
            return self._set_status(ShortcutAvailability.UNAVAILABLE, str(error))
        self._registration = registration
        return self._set_status(ShortcutAvailability.REGISTERED)

    def unregister(self) -> GlobalShortcutStatus:
        self._registration_requested = False
        registration = self._registration
        self._registration = None
        if registration is not None:
            try:
                registration.unregister()
            except ShortcutRegistrationError as error:
                return self._set_status(ShortcutAvailability.UNAVAILABLE, str(error))
        return self._set_status(ShortcutAvailability.DISABLED)

    def update_shortcut(self, value: str) -> GlobalShortcutStatus:
        shortcut = GlobalShortcut(value or DEFAULT_GLOBAL_TOGGLE_SHORTCUT)
        should_register = self._registration_requested
        self.unregister()
        self._shortcut = shortcut
        self._status = GlobalShortcutStatus(shortcut, ShortcutAvailability.DISABLED)
        if should_register:
            self._registration_requested = True
            return self.register()
        return self._status

    def _toggle(self) -> None:
        if self._state_value() in {
            ShellState.RECORDING.value,
            "recording_silent",
            "recording_sounding",
        }:
            self._controller.stop_recording()
        else:
            self._controller.start_recording()

    def _state_value(self) -> str:
        return self._controller.snapshot.state.value

    def _set_status(
        self, availability: ShortcutAvailability, message: str = ""
    ) -> GlobalShortcutStatus:
        self._status = GlobalShortcutStatus(self._shortcut, availability, message)
        return self._status
