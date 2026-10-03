"""Cancellation protocol shared by requests and runtime implementations."""

from __future__ import annotations

from typing import Protocol


class CancellationToken(Protocol):
    def is_cancelled(self) -> bool:
        """Return whether the caller requested best-effort cancellation."""
