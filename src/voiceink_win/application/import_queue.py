"""Bounded FIFO admission and reservation accounting."""

from __future__ import annotations

from collections import deque
from enum import StrEnum
from threading import Condition, RLock

from voiceink_win.domain import JobId
from voiceink_win.domain.imported_errors import QueueFullRejectedError


class ReservationState(StrEnum):
    HELD = "held"
    COMMITTED = "committed"
    RELEASED = "released"


class ReservationToken:
    def __init__(self, owner: JobId, queue: ImportQueue) -> None:
        self.owner = owner
        self._queue = queue
        self._state = ReservationState.HELD
        self._queued = False

    @property
    def state(self) -> ReservationState:
        with self._queue._lock:
            return self._state

    def release(self, owner: JobId) -> bool:
        with self._queue._lock:
            if owner != self.owner:
                return False
            if self._state is not ReservationState.HELD:
                return False
            self._state = ReservationState.RELEASED
            self._queue._reservations -= 1
            self._queue._condition.notify_all()
            return True


class ImportQueue:
    """O(1) amortized FIFO with capacity reserved for every admitted job."""

    def __init__(self, capacity: int = 8) -> None:
        if capacity < 1:
            raise ValueError("queue capacity must be positive")
        self.capacity = capacity
        self._entries: deque[object] = deque()
        self._reservations = 0
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._closed = False

    @property
    def reservation_count(self) -> int:
        with self._lock:
            return self._reservations

    def reserve(self, owner: JobId) -> ReservationToken:
        with self._lock:
            if self._closed or self._reservations >= self.capacity:
                raise QueueFullRejectedError("import queue is full")
            self._reservations += 1
            return ReservationToken(owner, self)

    def _release_committed(self, token: ReservationToken, owner: JobId) -> bool:
        with self._lock:
            if (
                token._queue is not self
                or token.owner != owner
                or token._state is not ReservationState.COMMITTED
            ):
                return False
            token._state = ReservationState.RELEASED
            self._reservations -= 1
            self._condition.notify_all()
            return True

    def enqueue(self, entry: object, token: ReservationToken) -> None:
        with self._lock:
            if token._queue is not self or token._state is not ReservationState.HELD:
                raise RuntimeError("reservation is not held by this queue")
            if getattr(getattr(entry, "job", None), "job_id", None) != token.owner:
                raise RuntimeError("reservation owner does not match queue entry")
            self._entries.append(entry)
            token._state = ReservationState.COMMITTED
            token._queued = True
            self._condition.notify()

    def enqueue_committed(self, entry: object, token: ReservationToken) -> bool:
        with self._lock:
            if token._queue is self and token._state is ReservationState.RELEASED:
                return False
            if token._queue is not self or token._state is not ReservationState.COMMITTED:
                raise RuntimeError("reservation is not committed by this queue")
            if getattr(getattr(entry, "job", None), "job_id", None) != token.owner:
                raise RuntimeError("reservation owner does not match queue entry")
            if token._queued:
                return False
            self._entries.append(entry)
            token._queued = True
            self._condition.notify()
            return True

    def get(self, timeout: float | None = None) -> object | None:
        with self._lock:
            if timeout is not None and timeout < 0:
                timeout = 0
            while not self._entries and not self._closed:
                if not self._condition.wait(timeout):
                    return None
                if timeout is not None:
                    timeout = 0
            if self._entries:
                entry = self._entries.popleft()
                token = getattr(entry, "token", None)
                if token is not None:
                    token._queued = False
                return entry
            return None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._condition.notify_all()
