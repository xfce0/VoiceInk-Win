"""Deterministic monotonic clock for queue and deadline tests."""

from __future__ import annotations

from threading import Lock


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._value = start
        self._lock = Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self._value

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("fake clock cannot move backwards")
        with self._lock:
            self._value += seconds
