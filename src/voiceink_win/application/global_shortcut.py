"""Application lifecycle for the single recording toggle shortcut."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Protocol

from voiceink_win.domain import (
    DEFAULT_GLOBAL_TOGGLE_SHORTCUT,
    GlobalShortcut,
    GlobalShortcutPort,
    GlobalShortcutRegistration,
    Settings,
    ShellSnapshot,
    ShellState,
    ShortcutConflictError,
    ShortcutRegistrationError,
    ShortcutUnavailableError,
    ShortcutValidationError,
)

from .persistence import PersistenceService


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
        if self._controller.snapshot.state is ShellState.UNAVAILABLE:
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
        if registration is not None:
            try:
                registration.unregister()
            except ShortcutRegistrationError as error:
                self._registration = registration
                return self._set_status(ShortcutAvailability.UNAVAILABLE, str(error))
            self._registration = None
        return self._set_status(ShortcutAvailability.DISABLED)

    def update_shortcut(self, value: str) -> GlobalShortcutStatus:
        shortcut = GlobalShortcut(value or DEFAULT_GLOBAL_TOGGLE_SHORTCUT)
        return self.replace_shortcut(shortcut)

    def configure_shortcut(self, value: str) -> GlobalShortcutStatus:
        """Set the effective value before the first registration attempt."""
        shortcut = GlobalShortcut(value or DEFAULT_GLOBAL_TOGGLE_SHORTCUT)
        if self._registration is not None:
            return self.replace_shortcut(shortcut)
        self._shortcut = shortcut
        return self._set_status(ShortcutAvailability.DISABLED)

    def replace_shortcut(self, shortcut: GlobalShortcut) -> GlobalShortcutStatus:
        """Replace a registration and restore the previous one on failure."""
        if shortcut == self._shortcut:
            if self._registration is None and self._registration_requested:
                return self.register()
            return self._status
        should_register = self._registration_requested
        previous = self._shortcut
        previous_registration = self._registration
        if previous_registration is not None:
            try:
                previous_registration.unregister()
            except ShortcutRegistrationError as error:
                return self._set_status(ShortcutAvailability.UNAVAILABLE, str(error))
            self._registration = None
        self._shortcut = shortcut
        self._status = GlobalShortcutStatus(shortcut, ShortcutAvailability.DISABLED)
        if not should_register:
            return self._status
        replacement = self.register()
        if replacement.availability is ShortcutAvailability.REGISTERED:
            return replacement
        self._shortcut = previous
        self._status = GlobalShortcutStatus(previous, ShortcutAvailability.DISABLED)
        restored = self.register()
        if restored.availability is ShortcutAvailability.REGISTERED:
            return GlobalShortcutStatus(
                previous,
                replacement.availability,
                replacement.message,
            )
        return self._set_status(
            ShortcutAvailability.UNAVAILABLE,
            "The previous global shortcut could not be restored.",
        )

    def _toggle(self) -> None:
        if self._controller.snapshot.state is ShellState.RECORDING:
            self._controller.stop_recording()
        else:
            self._controller.start_recording()

    def _set_status(
        self, availability: ShortcutAvailability, message: str = ""
    ) -> GlobalShortcutStatus:
        self._status = GlobalShortcutStatus(self._shortcut, availability, message)
        return self._status


class GlobalHotkeySettingsService:
    """Coordinate native replacement and durable hotkey settings."""

    def __init__(
        self,
        shortcut_service: GlobalToggleShortcutService,
        persistence: PersistenceService,
    ) -> None:
        self._shortcut_service = shortcut_service
        self._persistence = persistence
        self._committed = GlobalShortcut()
        self._generation = 0
        self._lock = Lock()

    @property
    def status(self) -> GlobalShortcutStatus:
        return self._shortcut_service.status

    def load(self, settings: Settings) -> GlobalShortcutStatus:
        """Resolve a stored value and register it without writing defaults back."""
        stored = str(settings.hotkeys.get("start_stop", "")).strip()
        warning = ""
        try:
            shortcut = GlobalShortcut(stored or DEFAULT_GLOBAL_TOGGLE_SHORTCUT)
        except ShortcutValidationError:
            shortcut = GlobalShortcut()
            warning = "invalid_stored_value"
        with self._lock:
            self._committed = shortcut
            self._generation += 1
        self._shortcut_service.configure_shortcut(shortcut.value)
        status = self._shortcut_service.register()
        if warning:
            return GlobalShortcutStatus(status.shortcut, status.availability, warning)
        return status

    def update(self, value: str) -> Future[GlobalShortcutStatus]:
        result: Future[GlobalShortcutStatus] = Future()
        try:
            candidate = GlobalShortcut(value)
        except ShortcutValidationError as error:
            result.set_exception(error)
            return result

        with self._lock:
            previous = self._committed
            self._generation += 1
            generation = self._generation
        status = self._shortcut_service.replace_shortcut(candidate)
        if status.availability is not ShortcutAvailability.REGISTERED:
            result.set_result(status)
            return result
        try:
            persistence_future = self._persistence.update_settings(
                {"hotkeys.start_stop": candidate.value}
            )
        except BaseException as error:
            self._finish_failed_update(result, generation, previous, error)
            return result
        persistence_future.add_done_callback(
            lambda future: self._finish_persistence(result, generation, previous, candidate, future)
        )
        return result

    def close(self) -> None:
        self._shortcut_service.unregister()

    def _finish_persistence(
        self,
        result: Future[GlobalShortcutStatus],
        generation: int,
        previous: GlobalShortcut,
        candidate: GlobalShortcut,
        persistence_future: Future[Settings],
    ) -> None:
        try:
            persistence_future.result()
        except BaseException as error:
            self._finish_failed_update(result, generation, previous, error)
            return
        with self._lock:
            if generation == self._generation:
                self._committed = candidate
        if not result.done():
            result.set_result(self._shortcut_service.status)

    def _finish_failed_update(
        self,
        result: Future[GlobalShortcutStatus],
        generation: int,
        previous: GlobalShortcut,
        error: BaseException,
    ) -> None:
        del error
        with self._lock:
            current = generation == self._generation
        if current:
            restored = self._shortcut_service.replace_shortcut(previous)
            if restored.availability is ShortcutAvailability.REGISTERED:
                restored = GlobalShortcutStatus(
                    restored.shortcut,
                    restored.availability,
                    "persistence_error",
                )
            else:
                restored = GlobalShortcutStatus(
                    restored.shortcut,
                    ShortcutAvailability.UNAVAILABLE,
                    "restoration_error",
                )
            if not result.done():
                result.set_result(restored)
            return
        if not result.done():
            result.set_result(self._shortcut_service.status)
