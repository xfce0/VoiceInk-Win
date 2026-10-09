"""Ports owned by the domain and implemented by outer layers."""

from __future__ import annotations

from typing import Protocol

from .models import AsrCapabilities, AsrRequest, RuntimeHealth, TranscriptResult


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
