"""Application-owned cancellation primitives."""

from __future__ import annotations

from threading import Event


class EventCancellationToken:
    def __init__(self, event: Event) -> None:
        self._event = event

    def is_cancelled(self) -> bool:
        return self._event.is_set()


class CancellationTokenSource:
    def __init__(self) -> None:
        self._event = Event()
        self.token = EventCancellationToken(self._event)

    def cancel(self) -> None:
        self._event.set()
