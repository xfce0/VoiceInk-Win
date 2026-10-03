"""Bounded application orchestration for one ASR runtime."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from queue import Full, Queue
from threading import Event, Lock, Thread
from typing import cast

from voiceink_win.domain import (
    AsrError,
    AsrRequest,
    AsrRuntime,
    AsrTimeoutError,
    CancellationError,
    ExecutionError,
    InvalidInputError,
    ProtocolError,
    QueueFullError,
    RuntimeUnavailableError,
    TranscriptResult,
)

DEFAULT_ASR_DEADLINE_SECONDS = 30.0
_STOP = object()


class _ServiceState(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(slots=True)
class _Invocation:
    result: TranscriptResult | object | None = None
    error: AsrError | None = None


@dataclass(slots=True)
class _Task:
    request: AsrRequest
    deadline: float
    cancellation: Event = field(default_factory=Event)
    done: Event = field(default_factory=Event)
    invocation: _Invocation = field(default_factory=_Invocation)


class _TaskCancellation:
    def __init__(self, task: _Task) -> None:
        self._task = task

    def is_cancelled(self) -> bool:
        token = self._task.request.cancellation
        return self._task.cancellation.is_set() or (token is not None and token.is_cancelled())


class AsrApplicationService:
    """Run requests on a fixed worker pool and reject work after close."""

    def __init__(
        self,
        runtime: AsrRuntime,
        *,
        max_concurrency: int = 1,
        queue_capacity: int | None = None,
        default_deadline: float = DEFAULT_ASR_DEADLINE_SECONDS,
        shutdown_timeout: float = 5.0,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if (
            not math.isfinite(default_deadline)
            or not math.isfinite(shutdown_timeout)
            or default_deadline <= 0
            or shutdown_timeout <= 0
        ):
            raise ValueError("service deadlines must be positive")
        capabilities = runtime.capabilities()
        worker_count = min(max_concurrency, capabilities.max_concurrency)
        if queue_capacity is None:
            queue_capacity = worker_count
        if queue_capacity < worker_count:
            raise ValueError("queue_capacity must be at least max_concurrency")

        self._runtime = runtime
        self._default_deadline = default_deadline
        self._shutdown_timeout = shutdown_timeout
        self._queue: Queue[_Task | object] = Queue(maxsize=queue_capacity)
        self._lock = Lock()
        self._close_lock = Lock()
        self._state = _ServiceState.OPEN
        self._pending: set[int] = set()
        self._active: dict[int, _Task] = {}
        self._workers: list[Thread] = []
        self._start_workers(worker_count)

    def capabilities(self):
        return self._runtime.capabilities()

    def health(self):
        return self._runtime.health()

    def transcribe(self, request: AsrRequest) -> TranscriptResult:
        self._validate_request(request)
        with self._lock:
            if self._state is not _ServiceState.OPEN:
                raise RuntimeUnavailableError("ASR service is closed")

        deadline = (
            request.deadline
            if request.deadline is not None
            else time.monotonic() + self._default_deadline
        )
        if time.monotonic() >= deadline:
            raise AsrTimeoutError("ASR request deadline exceeded")
        task = _Task(request=request, deadline=deadline)
        task_id = id(task)
        with self._lock:
            if self._state is not _ServiceState.OPEN:
                raise RuntimeUnavailableError("ASR service is closed")
            try:
                self._queue.put_nowait(task)
            except Full as error:
                raise QueueFullError("ASR request queue is full") from error
            self._pending.add(task_id)

        return self._wait_for_task(task, task_id)

    def close(self) -> None:
        with self._close_lock:
            with self._lock:
                if self._state is _ServiceState.CLOSED:
                    return
                self._state = _ServiceState.CLOSING
                for task in self._active.values():
                    task.cancellation.set()
                self._drain_pending_locked()

            deadline = time.monotonic() + self._shutdown_timeout
            while time.monotonic() < deadline:
                with self._lock:
                    if not self._active:
                        break
                time.sleep(0.01)

            close_error: AsrError | None = None
            try:
                self._runtime.close()
            except AsrError as error:
                close_error = error
            except Exception as error:
                close_error = ExecutionError("ASR runtime cleanup failed", cause=error)

            for _ in self._workers:
                self._queue.put_nowait(_STOP)
            for worker in self._workers:
                remaining = max(0.0, deadline - time.monotonic())
                worker.join(timeout=remaining)
            with self._lock:
                self._state = _ServiceState.CLOSED
            if close_error is not None:
                raise close_error

    def _start_workers(self, count: int) -> None:
        try:
            for index in range(count):
                worker = Thread(
                    target=self._worker_loop,
                    name=f"asr-worker-{index}",
                    daemon=True,
                )
                worker.start()
                self._workers.append(worker)
        except Exception as error:
            self._state = _ServiceState.CLOSING
            for _ in self._workers:
                self._queue.put_nowait(_STOP)
            for worker in self._workers:
                worker.join(timeout=self._shutdown_timeout)
            try:
                self._runtime.close()
            except Exception as cleanup_error:
                raise ExecutionError(
                    "ASR worker startup and cleanup failed", cause=cleanup_error
                ) from error
            raise ExecutionError("ASR worker startup failed", cause=error) from error

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                self._queue.task_done()
                return
            task = cast(_Task, item)
            task_id = id(task)
            with self._lock:
                self._pending.discard(task_id)
                if self._state is not _ServiceState.OPEN:
                    task.cancellation.set()
                self._active[task_id] = task
            try:
                if task.cancellation.is_set():
                    task.invocation.error = CancellationError("ASR request was cancelled")
                else:
                    runtime_request = replace(
                        task.request,
                        deadline=task.deadline,
                        cancellation=_TaskCancellation(task),
                    )
                    try:
                        task.invocation.result = self._runtime.transcribe(runtime_request)
                    except AsrError as error:
                        task.invocation.error = error
                    except Exception as error:
                        task.invocation.error = ExecutionError(
                            "ASR runtime execution failed", cause=error
                        )
            finally:
                with self._lock:
                    self._active.pop(task_id, None)
                task.done.set()
                self._queue.task_done()

    def _wait_for_task(self, task: _Task, task_id: int) -> TranscriptResult:
        while not task.done.wait(timeout=0.02):
            if self._request_cancelled(task):
                task.cancellation.set()
                raise CancellationError("ASR request was cancelled")
            if time.monotonic() >= task.deadline:
                task.cancellation.set()
                raise AsrTimeoutError("ASR request deadline exceeded")
        if task.cancellation.is_set() or self._request_cancelled(task):
            raise CancellationError("ASR request was cancelled")
        if task.invocation.error is not None:
            raise task.invocation.error
        if not isinstance(task.invocation.result, TranscriptResult):
            raise ProtocolError("ASR runtime returned a malformed transcript result")
        return task.invocation.result

    def _request_cancelled(self, task: _Task) -> bool:
        token = task.request.cancellation
        return token is not None and token.is_cancelled()

    def _drain_pending_locked(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except Exception:
                return
            if item is not _STOP:
                task = cast(_Task, item)
                self._pending.discard(id(task))
                task.cancellation.set()
                task.invocation.error = CancellationError("ASR service is closing")
                task.done.set()
            self._queue.task_done()

    def _validate_request(self, request: AsrRequest) -> None:
        if not isinstance(request, AsrRequest):
            raise InvalidInputError("ASR request must be an AsrRequest")
        if request.audio.byte_length == 0:
            raise InvalidInputError("ASR audio must not be empty")


ApplicationAsrService = AsrApplicationService
