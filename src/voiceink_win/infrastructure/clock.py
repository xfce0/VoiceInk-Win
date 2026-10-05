"""System clock implementations shared by infrastructure adapters."""

from __future__ import annotations

from time import monotonic, sleep


class SystemMonotonicClock:
    def monotonic(self) -> float:
        return monotonic()

    def sleep(self, seconds: float) -> None:
        sleep(seconds)
