"""Application lifecycle for the single recording toggle shortcut."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from enum import StrEnum
from threading import Event, Lock, Thread
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

HOTKEY_PERSISTENCE_TIMEOUT_SECONDS = 5.0
HOTKEY_RECONCILIATION_TIMEOUT_SECONDS = 30.0


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
        self._closed = False
        self._status = GlobalShortcutStatus(self._shortcut, ShortcutAvailability.DISABLED)

    @property
    def status(self) -> GlobalShortcutStatus:
        return self._status

    def register(self) -> GlobalShortcutStatus:
        if self._closed:
            return self._set_status(ShortcutAvailability.DISABLED, "closed")
        if self._registration is not None:
            return self._status
        self._registration_requested = True
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

    def close(self) -> GlobalShortcutStatus:
        self._closed = True
        return self.unregister()

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
        if self._closed:
            return self._set_status(ShortcutAvailability.DISABLED, "closed")
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


class GlobalHotkeySettingsService:
    """Coordinate native replacement and durable hotkey settings."""

    def __init__(
        self,
        shortcut_service: GlobalToggleShortcutService,
        persistence: PersistenceService,
        dispatcher: Callable[[Callable[[], GlobalShortcutStatus]], GlobalShortcutStatus]
        | None = None,
    ) -> None:
        self._shortcut_service = shortcut_service
        self._persistence = persistence
        self._committed = GlobalShortcut()
        self._lock = Lock()
        self._operation_lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hotkey-settings")
        self._dispatcher = dispatcher
        self._reconciliation: Event | None = None
        self._reconciliation_result: GlobalShortcutStatus | None = None
        self._closed = False

    def set_dispatcher(
        self,
        dispatcher: Callable[[Callable[[], GlobalShortcutStatus]], GlobalShortcutStatus],
    ) -> None:
        self._dispatcher = dispatcher

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
        with self._operation_lock:
            with self._lock:
                self._committed = shortcut
            self._shortcut_service.configure_shortcut(shortcut.value)
            status = self._shortcut_service.register()
        if warning:
            return GlobalShortcutStatus(status.shortcut, status.availability, warning)
        return status

    def update(self, value: str) -> Future[GlobalShortcutStatus]:
        try:
            candidate = GlobalShortcut(value)
        except ShortcutValidationError as error:
            result: Future[GlobalShortcutStatus] = Future()
            result.set_exception(error)
            return result
        return self._executor.submit(self._update_serial, candidate)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        try:
            self._dispatch(self._shortcut_service.close)
        finally:
            invalidate = getattr(self._dispatcher, "invalidate", None)
            if invalidate is not None:
                invalidate()
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _update_serial(self, candidate: GlobalShortcut) -> GlobalShortcutStatus:
        with self._operation_lock:
            with self._lock:
                if self._closed:
                    return GlobalShortcutStatus(candidate, ShortcutAvailability.DISABLED, "closed")
                if self._reconciliation is not None:
                    return GlobalShortcutStatus(
                        candidate,
                        ShortcutAvailability.UNAVAILABLE,
                        "persistence_pending",
                    )
                previous = self._committed
            status = self._dispatch(lambda: self._shortcut_service.replace_shortcut(candidate))
            if status.availability is not ShortcutAvailability.REGISTERED:
                return status
            try:
                persistence_future = self._persistence.update_settings(
                    {"hotkeys.start_stop": candidate.value}
                )
                try:
                    persistence_future.result(timeout=HOTKEY_PERSISTENCE_TIMEOUT_SECONDS)
                except TimeoutError:
                    reconciliation = Event()
                    with self._lock:
                        self._reconciliation = reconciliation
                        self._reconciliation_result = None
                    Thread(
                        target=self._reconcile_persistence,
                        args=(persistence_future, candidate, previous, reconciliation),
                        name="hotkey-persistence-reconciliation",
                        daemon=True,
                    ).start()
                    if reconciliation.wait(HOTKEY_RECONCILIATION_TIMEOUT_SECONDS):
                        with self._lock:
                            reconciled = self._reconciliation_result
                        if reconciled is not None:
                            return reconciled
                    return GlobalShortcutStatus(
                        candidate,
                        ShortcutAvailability.UNAVAILABLE,
                        "persistence_pending",
                    )
            except BaseException:
                restored = self._dispatch(lambda: self._shortcut_service.replace_shortcut(previous))
                if restored.availability is ShortcutAvailability.REGISTERED:
                    return GlobalShortcutStatus(
                        restored.shortcut,
                        restored.availability,
                        "persistence_error",
                    )
                return GlobalShortcutStatus(
                    restored.shortcut,
                    ShortcutAvailability.UNAVAILABLE,
                    "restoration_error",
                )
            with self._lock:
                self._committed = candidate
            return self._dispatch(lambda: self._shortcut_service.status)

    def _reconcile_persistence(
        self,
        persistence_future: Future[Settings],
        candidate: GlobalShortcut,
        previous: GlobalShortcut,
        reconciliation: Event,
    ) -> None:
        status = GlobalShortcutStatus(
            previous,
            ShortcutAvailability.UNAVAILABLE,
            "reconciliation_error",
        )
        try:
            persistence_future.result()
        except BaseException:
            with self._lock:
                closed = self._closed
            if not closed:
                try:
                    restored = self._dispatch(
                        lambda: self._shortcut_service.replace_shortcut(previous)
                    )
                except BaseException:
                    restored = GlobalShortcutStatus(
                        previous,
                        ShortcutAvailability.UNAVAILABLE,
                        "restoration_error",
                    )
                status = GlobalShortcutStatus(
                    restored.shortcut,
                    restored.availability,
                    "persistence_error",
                )
            else:
                status = GlobalShortcutStatus(
                    previous,
                    ShortcutAvailability.DISABLED,
                    "closed",
                )
        else:
            with self._lock:
                closed = self._closed
                if not closed:
                    self._committed = candidate
            if closed:
                status = GlobalShortcutStatus(candidate, ShortcutAvailability.DISABLED, "closed")
            else:
                try:
                    status = self._dispatch(lambda: self._shortcut_service.status)
                except BaseException:
                    status = GlobalShortcutStatus(
                        candidate,
                        ShortcutAvailability.DISABLED,
                        "closed",
                    )
        finally:
            with self._lock:
                if self._reconciliation is reconciliation:
                    self._reconciliation = None
                self._reconciliation_result = status
            reconciliation.set()

    def _dispatch(self, operation: Callable[[], GlobalShortcutStatus]) -> GlobalShortcutStatus:
        if self._dispatcher is None:
            return operation()
        return self._dispatcher(operation)
