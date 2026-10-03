"""Ports owned by the domain and implemented by outer layers."""

from __future__ import annotations

from typing import Protocol

from .models import AsrCapabilities, AsrRequest, RuntimeHealth, TranscriptResult


class AsrRuntime(Protocol):
    def capabilities(self) -> AsrCapabilities: ...

    def health(self) -> RuntimeHealth: ...

    def transcribe(self, request: AsrRequest) -> TranscriptResult: ...

    def close(self) -> None: ...
