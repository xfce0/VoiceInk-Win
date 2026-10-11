"""Windows global shortcut adapter with an unavailable cross-platform fallback."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from voiceink_win.domain import (
    GlobalShortcut,
    GlobalShortcutRegistration,
    ShortcutConflictError,
    ShortcutUnavailableError,
)

WM_HOTKEY = 0x0312
ERROR_HOTKEY_ALREADY_REGISTERED = 1409


class UnavailableGlobalShortcutPort:
    """Safe no-op boundary used when native global shortcuts are unavailable."""

    def register(
        self, shortcut: GlobalShortcut, callback: Callable[[], None]
    ) -> GlobalShortcutRegistration:
        del shortcut, callback
        raise ShortcutUnavailableError("Global shortcuts are unavailable on this platform.")


class WindowsGlobalShortcutPort:
    """Register one Win32 hotkey and receive it through Qt's native event loop."""

    def __init__(self) -> None:
        self._registration: _WindowsShortcutRegistration | None = None

    def register(
        self, shortcut: GlobalShortcut, callback: Callable[[], None]
    ) -> GlobalShortcutRegistration:
        if os.name != "nt":
            raise ShortcutUnavailableError("Global shortcuts require Windows.")
        if self._registration is not None:
            raise ShortcutConflictError("A global shortcut is already registered.")
        try:
            registration = _WindowsShortcutRegistration.create(shortcut, callback)
        except ShortcutConflictError:
            raise
        except ShortcutUnavailableError:
            raise
        except OSError as error:
            raise ShortcutUnavailableError(
                "Windows global shortcut registration failed."
            ) from error
        self._registration = registration
        registration.on_unregistered = lambda: setattr(self, "_registration", None)
        return registration


def create_global_shortcut_port() -> WindowsGlobalShortcutPort | UnavailableGlobalShortcutPort:
    """Select the native seam without importing Windows or Qt on other platforms."""
    return WindowsGlobalShortcutPort() if os.name == "nt" else UnavailableGlobalShortcutPort()


@dataclass(slots=True)
class _WindowsShortcutRegistration:
    application: Any
    user32: Any
    event_filter: Any
    hotkey_id: int
    on_unregistered: Callable[[], None] | None = None

    @classmethod
    def create(cls, shortcut: GlobalShortcut, callback: Callable[[], None]):
        try:
            import ctypes
            from ctypes import wintypes

            from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication
        except (ImportError, OSError) as error:
            raise ShortcutUnavailableError("Qt is unavailable for global shortcuts.") from error

        application = QCoreApplication.instance()
        if application is None:
            raise ShortcutUnavailableError("The Qt application event loop is unavailable.")
        modifiers, virtual_key = _windows_hotkey_parts(shortcut)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, wintypes.INT, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, wintypes.INT]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        hotkey_id = id(callback) & 0x7FFFFFFF or 1
        if not user32.RegisterHotKey(None, hotkey_id, modifiers, virtual_key):
            error_code = ctypes.get_last_error()
            if error_code == ERROR_HOTKEY_ALREADY_REGISTERED:
                raise ShortcutConflictError("The selected global shortcut is already in use.")
                raise ShortcutUnavailableError("Windows rejected the global shortcut.")
        try:
            event_filter = _NativeHotkeyFilter(QAbstractNativeEventFilter, hotkey_id, callback)
            application.installNativeEventFilter(event_filter)
        except BaseException as error:
            user32.UnregisterHotKey(None, hotkey_id)
            raise ShortcutUnavailableError(
                "The Windows global shortcut event bridge is unavailable."
            ) from error
        return cls(application, user32, event_filter, hotkey_id)

    def unregister(self) -> None:
        if self.on_unregistered is None:
            return
        callback = self.on_unregistered
        try:
            self.application.removeNativeEventFilter(self.event_filter)
        finally:
            if not self.user32.UnregisterHotKey(None, self.hotkey_id):
                raise ShortcutUnavailableError("Windows could not unregister the global shortcut.")
        self.on_unregistered = None
        callback()


def _NativeHotkeyFilter(base_class, hotkey_id: int, callback: Callable[[], None]):
    """Create the Qt filter lazily so non-Windows imports stay dependency-free."""

    import ctypes
    from ctypes import wintypes

    class NativeMessage(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt_x", wintypes.LONG),
            ("pt_y", wintypes.LONG),
        ]

    class NativeHotkeyFilter(base_class):
        def nativeEventFilter(self, event_type, message):
            if event_type not in ("windows_generic_MSG", b"windows_generic_MSG"):
                return False, 0
            if not message:
                return False, 0
            msg = ctypes.cast(int(message), ctypes.POINTER(NativeMessage)).contents
            if msg.message == WM_HOTKEY and msg.wParam == hotkey_id:
                callback()
                return True, 0
            return False, 0

    return NativeHotkeyFilter()


def _windows_hotkey_parts(shortcut: GlobalShortcut) -> tuple[int, int]:
    """Translate the small user-facing syntax supported by the Win32 adapter."""
    modifiers = {"CTRL": 0x0002, "CONTROL": 0x0002, "ALT": 0x0001, "SHIFT": 0x0004, "WIN": 0x0008}
    virtual_keys = {"SPACE": 0x20, "ENTER": 0x0D, "TAB": 0x09, "ESC": 0x1B, "ESCAPE": 0x1B}
    parts = [part.strip().upper() for part in shortcut.value.split("+") if part.strip()]
    if len(parts) < 2:
        raise ShortcutUnavailableError("A global shortcut requires a modifier and a key.")
    flags = 0
    for part in parts[:-1]:
        if part not in modifiers:
            raise ShortcutUnavailableError("The global shortcut modifier is unsupported.")
        flags |= modifiers[part]
    key = parts[-1]
    if key in virtual_keys:
        return flags, virtual_keys[key]
    if len(key) == 1 and key.isalnum():
        return flags, ord(key)
    if key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return flags, 0x70 + int(key[1:]) - 1
    raise ShortcutUnavailableError("The global shortcut key is unsupported.")
