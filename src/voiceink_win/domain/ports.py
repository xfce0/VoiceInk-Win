"""Ports owned by the domain and implemented by outer layers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .models import AsrCapabilities, AsrRequest, RuntimeHealth, TranscriptResult
from .shortcuts import GlobalShortcut


class AsrRuntime(Protocol):
    def capabilities(self) -> AsrCapabilities: ...

    def health(self) -> RuntimeHealth: ...

    def transcribe(self, request: AsrRequest) -> TranscriptResult: ...

    def close(self, deadline: float | None = None) -> None: ...


class AsrRequestHandle(Protocol):
    """Request-scoped ownership and quiescence boundary for admitted ASR work."""

    def cancel(self, deadline: float | None = None) -> None: ...

    def await_result(self, deadline: float | None = None) -> TranscriptResult: ...

    def await_quiescence(self, deadline: float | None = None) -> None: ...

    def release(self) -> None: ...


class GlobalShortcutRegistration(Protocol):
    """A registered shortcut that owns its native registration lifetime."""

    def unregister(self) -> None: ...


class GlobalShortcutPort(Protocol):
    """Register one process callback without exposing platform event details."""

    def register(
        self, shortcut: GlobalShortcut, callback: Callable[[], None]
    ) -> GlobalShortcutRegistration: ...
