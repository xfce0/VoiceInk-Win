"""Process-scoped instance lease adapters for the interactive desktop shell."""

from __future__ import annotations

import os
from typing import Protocol

WINDOWS_INSTANCE_NAME = r"Local\VoiceInk-Win"


class InstanceLease(Protocol):
    def acquire(self) -> bool: ...

    def release(self) -> None: ...


class NoopInstanceLease:
    """Allow cross-platform development runs without a Windows kernel lease."""

    def __init__(self) -> None:
        self._acquired = False

    def acquire(self) -> bool:
        if self._acquired:
            return True
        self._acquired = True
        return True

    def release(self) -> None:
        self._acquired = False


class WindowsInstanceLease:
    """Own a kernel-backed, user-scoped mutex for one interactive shell."""

    def __init__(self, name: str = WINDOWS_INSTANCE_NAME) -> None:
        self._name = name
        self._handle: int | None = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, True, self._name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        if ctypes.get_last_error() == 183:
            kernel32.CloseHandle(handle)
            return False
        self._kernel32 = kernel32
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        kernel32 = self._kernel32
        try:
            kernel32.ReleaseMutex(handle)
        finally:
            kernel32.CloseHandle(handle)


def create_instance_lease() -> InstanceLease:
    """Select the native singleton boundary without importing Win32 elsewhere."""
    return WindowsInstanceLease() if os.name == "nt" else NoopInstanceLease()


__all__ = [
    "InstanceLease",
    "NoopInstanceLease",
    "WINDOWS_INSTANCE_NAME",
    "WindowsInstanceLease",
    "create_instance_lease",
]
