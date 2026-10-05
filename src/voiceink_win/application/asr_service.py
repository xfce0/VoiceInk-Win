"""Bounded application orchestration for one ASR runtime."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from inspect import signature
from queue import Full, Queue
from threading import Event, Lock, Thread, current_thread
from typing import cast

from voiceink_win.domain import (
    AsrError,
    AsrRequest,
    AsrRuntime,
    AsrTimeoutError,
    CancellationError,
    ExecutionError,
    InvalidInputError,
    MonotonicClock,
    ProtocolError,
    QueueFullError,
    RuntimeRecoveryPendingError,
    RuntimeUnavailableError,
    TranscriptResult,
)

DEFAULT_ASR_DEADLINE_SECONDS = 30.0
_STOP = object()


class _ServiceState(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class SystemMonotonicClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


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
        clock: MonotonicClock | None = None,
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
        self._clock = clock or SystemMonotonicClock()
        self._queue: Queue[_Task | object] = Queue(maxsize=queue_capacity)
        self._lock = Lock()
        self._close_lock = Lock()
        self._state = _ServiceState.OPEN
        self._pending: set[int] = set()
        self._active: dict[int, _Task] = {}
        self._workers: list[Thread] = []
        self._runtime_close_started = False
        self._runtime_close_error: AsrError | None = None
        self._deferred_runtime_close: Thread | None = None
        self._runtime_close_thread: Thread | None = None
        self._start_workers(worker_count)

    def capabilities(self):
        return self._runtime.capabilities()

    def health(self):
        return self._runtime.health()

    @property
    def clock(self) -> MonotonicClock:
        return self._clock

    def set_clock(self, clock: MonotonicClock) -> None:
        with self._lock:
            if self._state is not _ServiceState.OPEN:
                raise RuntimeUnavailableError("ASR service is closed")
            self._clock = clock

    def wait_idle(self, timeout: float) -> bool:
        """Wait until runtime workers have finished all admitted requests."""
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        deadline = self._clock.monotonic() + timeout
        while self._clock.monotonic() < deadline:
            with self._lock:
                if not self._active and not self._pending:
                    return True
            self._clock.sleep(0.01)
        with self._lock:
            return not self._active and not self._pending

    def wait_active(self, timeout: float) -> bool:
        """Wait until at least one request has entered the runtime worker."""
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        deadline = self._clock.monotonic() + timeout
        while self._clock.monotonic() < deadline:
            with self._lock:
                if self._active:
                    return True
            self._clock.sleep(0.01)
        with self._lock:
            return bool(self._active)

    def interrupt_active(self) -> None:
        """Request cancellation and interrupt runtimes that expose a hard stop."""
        with self._lock:
            active = tuple(self._active.values())
        for task in active:
            task.cancellation.set()
        interrupt = getattr(self._runtime, "interrupt", None)
        if interrupt is not None:
            interrupt()

    def transcribe(self, request: AsrRequest) -> TranscriptResult:
        self._validate_request(request)
        with self._lock:
            if self._state is not _ServiceState.OPEN:
                raise RuntimeUnavailableError("ASR service is closed")

        deadline = (
            request.deadline
            if request.deadline is not None
            else self._clock.monotonic() + self._default_deadline
        )
        if self._clock.monotonic() >= deadline:
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

    def close(self, *, deadline: float | None = None) -> None:
        with self._close_lock:
            with self._lock:
                if self._state is _ServiceState.CLOSED:
                    return
                self._state = _ServiceState.CLOSING
                for task in self._active.values():
                    task.cancellation.set()
                self._drain_pending_locked()

            close_deadline = (
                deadline
                if deadline is not None
                else self._clock.monotonic() + self._shutdown_timeout
            )
            while self._clock.monotonic() < close_deadline:
                with self._lock:
                    if not self._active:
                        break
                self._clock.sleep(0.01)
            with self._lock:
                active_error = (
                    RuntimeRecoveryPendingError(
                        "ASR worker cleanup remains pending after the close deadline"
                    )
                    if self._active
                    else None
                )

            for _ in self._workers:
                self._queue.put_nowait(_STOP)
            for worker in self._workers:
                remaining = max(0.0, close_deadline - self._clock.monotonic())
                worker.join(timeout=remaining)
            workers_alive = any(worker.is_alive() for worker in self._workers)
            close_error: AsrError | None = None
            if not workers_alive:
                close_error = self._close_runtime_once(close_deadline)
            else:
                self._start_deferred_runtime_close(close_deadline)
                with self._lock:
                    close_error = RuntimeRecoveryPendingError(
                        "ASR runtime cleanup is deferred until workers stop"
                    )
            with self._lock:
                if not workers_alive and close_error is None:
                    self._state = _ServiceState.CLOSED
                elif active_error is None:
                    active_error = RuntimeRecoveryPendingError(
                        "ASR shutdown recovery remains pending"
                    )
            if active_error is not None:
                raise active_error
            if close_error is not None:
                raise close_error

    def _close_runtime_once(self, deadline: float) -> AsrError | None:
        with self._lock:
            if self._runtime_close_started:
                if self._runtime_close_thread is not None and self._runtime_close_thread.is_alive():
                    return RuntimeRecoveryPendingError("ASR runtime cleanup is still pending")
                return self._runtime_close_error
            self._runtime_close_started = True
            self._runtime_close_error = None
        close_errors: list[AsrError] = []
        finished = Event()

        def close_runtime() -> None:
            try:
                close = self._runtime.close
                parameters = signature(close).parameters
                if "deadline" in parameters or any(
                    parameter.kind is parameter.VAR_KEYWORD for parameter in parameters.values()
                ):
                    close(deadline=deadline)
                else:
                    close()
            except AsrError as error:
                close_errors.append(error)
            except Exception as error:
                close_errors.append(ExecutionError("ASR runtime cleanup failed", cause=error))
            finally:
                with self._lock:
                    close_error = close_errors[0] if close_errors else None
                    self._runtime_close_error = close_error
                    if isinstance(close_error, RuntimeRecoveryPendingError):
                        self._runtime_close_started = False
                        self._runtime_close_thread = None
                    if not close_errors:
                        self._state = _ServiceState.CLOSED
                finished.set()

        thread = Thread(target=close_runtime, name="asr-runtime-close", daemon=True)
        with self._lock:
            self._runtime_close_thread = thread
        thread.start()
        thread.join(max(0.0, deadline - self._clock.monotonic()))
        if not finished.is_set():
            return RuntimeRecoveryPendingError("ASR runtime cleanup exceeded the close deadline")
        with self._lock:
            return self._runtime_close_error

    def _start_deferred_runtime_close(self, deadline: float) -> None:
        with self._lock:
            if self._runtime_close_started or (
                self._deferred_runtime_close is not None and self._deferred_runtime_close.is_alive()
            ):
                return
            worker = Thread(
                target=self._deferred_runtime_close_loop,
                args=(deadline,),
                name="asr-runtime-cleanup",
                daemon=True,
            )
            self._deferred_runtime_close = worker
            worker.start()

    def _deferred_runtime_close_loop(self, deadline: float) -> None:
        for worker in self._workers:
            if worker is not current_thread():
                worker.join()
        self._close_runtime_once(deadline)

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
        while not task.done.is_set():
            if self._request_cancelled(task):
                task.cancellation.set()
                raise CancellationError("ASR request was cancelled")
            if self._clock.monotonic() >= task.deadline:
                task.cancellation.set()
                raise AsrTimeoutError("ASR request deadline exceeded")
            self._clock.sleep(0.02)
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
