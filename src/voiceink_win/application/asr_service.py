"""Bounded application orchestration for one ASR runtime."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from inspect import signature
from queue import Empty, Full, Queue
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


class _TaskState(StrEnum):
    ADMITTED = "admitted"
    ACTIVE = "active"
    QUIESCENT = "quiescent"
    RELEASED = "released"


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
    generation: int
    request: AsrRequest
    deadline: float
    cancellation: Event = field(default_factory=Event)
    done: Event = field(default_factory=Event)
    terminal: Event = field(default_factory=Event)
    invocation: _Invocation = field(default_factory=_Invocation)
    state: _TaskState = _TaskState.ADMITTED
    auto_release: bool = False


class _TaskCancellation:
    def __init__(self, task: _Task) -> None:
        self._task = task

    def is_cancelled(self) -> bool:
        token = self._task.request.cancellation
        return self._task.cancellation.is_set() or (token is not None and token.is_cancelled())


class AsrRequestHandle:
    """Own one admitted request until its worker is quiescent and released."""

    def __init__(self, service: AsrApplicationService, task: _Task) -> None:
        self._service = service
        self._task = task

    @property
    def generation(self) -> int:
        return self._task.generation

    @property
    def quiescence_event(self) -> Event:
        """Expose the internal fence to application workflows that own cleanup."""
        return self._task.done

    @property
    def is_quiescent(self) -> bool:
        return self._task.done.is_set()

    @property
    def is_released(self) -> bool:
        with self._service._lock:
            return self._task.state is _TaskState.RELEASED

    def cancel(self, deadline: float | None = None) -> None:
        del deadline
        self._service._cancel_task(self._task)

    def await_result(self, deadline: float | None = None) -> TranscriptResult:
        return self._service._await_result(self._task, deadline)

    def await_quiescence(self, deadline: float | None = None) -> None:
        self._service._await_quiescence(self._task, deadline)

    def release(self) -> None:
        self._service._release_task(self._task)


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
        self._admitted: dict[int, _Task] = {}
        self._next_generation = 1
        # Keep the historical queue_capacity meaning: it bounds waiting work;
        # active workers add their own bounded slots.
        self._admission_capacity = worker_count + queue_capacity
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

    @property
    def admitted_count(self) -> int:
        with self._lock:
            return len(self._admitted)

    def interrupt_active(self) -> None:
        """Request cancellation and interrupt runtimes that expose a hard stop."""
        with self._lock:
            active = tuple(self._active.values())
            for task in active:
                self._cancel_task_locked(task, CancellationError("ASR request was interrupted"))
        interrupt = getattr(self._runtime, "interrupt", None)
        if interrupt is not None:
            interrupt()

    def try_admit(self, request: AsrRequest) -> AsrRequestHandle:
        """Admit one request or raise a bounded ``QueueFullError``.

        The admission lock is the single linearization point. The returned
        handle owns the admission slot until ``release()`` succeeds.
        """
        self._validate_request(request)
        token = request.cancellation
        if token is not None and token.is_cancelled():
            raise CancellationError("ASR request was cancelled")
        deadline = (
            request.deadline
            if request.deadline is not None
            else self._clock.monotonic() + self._default_deadline
        )
        if self._clock.monotonic() >= deadline:
            raise AsrTimeoutError("ASR request deadline exceeded")
        with self._lock:
            if self._state is not _ServiceState.OPEN:
                raise RuntimeUnavailableError("ASR service is closed")
            if len(self._admitted) >= self._admission_capacity:
                raise QueueFullError("ASR request queue is full")
            task = _Task(
                generation=self._next_generation,
                request=request,
                deadline=deadline,
            )
            self._next_generation += 1
            try:
                self._queue.put_nowait(task)
            except Full as error:
                raise QueueFullError("ASR request queue is full") from error
            self._admitted[task.generation] = task
            self._pending.add(task.generation)
        return AsrRequestHandle(self, task)

    def transcribe(self, request: AsrRequest) -> TranscriptResult:
        """Preserve the legacy synchronous API over request-scoped admission."""
        handle = self.try_admit(request)
        try:
            return handle.await_result()
        finally:
            self._finish_legacy_request(handle._task)

    def close(self, *, deadline: float | None = None) -> None:
        with self._close_lock:
            with self._lock:
                if self._state is _ServiceState.CLOSED:
                    return
                self._state = _ServiceState.CLOSING
                for task in self._active.values():
                    self._cancel_task_locked(task, CancellationError("ASR service is closing"))
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
                elif active_error is None and close_error is None:
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
                    if isinstance(close_error, RuntimeRecoveryPendingError) or (
                        close_error is not None and close_error.retryable
                    ):
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
            with self._lock:
                self._pending.discard(task.generation)
                if task.state in {_TaskState.QUIESCENT, _TaskState.RELEASED}:
                    should_run = False
                else:
                    if self._state is not _ServiceState.OPEN:
                        self._cancel_task_locked(task, CancellationError("ASR service is closing"))
                    elif self._request_cancelled(task):
                        self._cancel_task_locked(
                            task, CancellationError("ASR request was cancelled")
                        )
                    elif self._clock.monotonic() >= task.deadline:
                        task.cancellation.set()
                        self._publish_terminal_locked(
                            task, error=AsrTimeoutError("ASR request deadline exceeded")
                        )
                    if task.state is _TaskState.ADMITTED:
                        task.state = _TaskState.ACTIVE
                        self._active[task.generation] = task
                        should_run = not task.terminal.is_set()
                    else:
                        should_run = False
            result: TranscriptResult | object | None = None
            error: AsrError | None = None
            try:
                if should_run:
                    runtime_request = replace(
                        task.request,
                        deadline=task.deadline,
                        cancellation=_TaskCancellation(task),
                    )
                    try:
                        result = self._runtime.transcribe(runtime_request)
                    except AsrError as runtime_error:
                        error = runtime_error
                    except Exception as runtime_error:
                        error = ExecutionError("ASR runtime execution failed", cause=runtime_error)
            finally:
                with self._lock:
                    if should_run:
                        self._publish_terminal_locked(task, result=result, error=error)
                    self._active.pop(task.generation, None)
                    if task.state is not _TaskState.RELEASED:
                        task.state = _TaskState.QUIESCENT
                        task.done.set()
                        if task.auto_release:
                            self._release_task_locked(task)
                self._queue.task_done()

    def _await_result(self, task: _Task, deadline: float | None) -> TranscriptResult:
        effective_deadline = task.deadline if deadline is None else min(task.deadline, deadline)
        while not task.terminal.is_set():
            if self._request_cancelled(task):
                self._cancel_task(task)
                continue
            if self._clock.monotonic() >= effective_deadline:
                self._timeout_task(task)
                continue
            task.terminal.wait(0.01)
            if not task.terminal.is_set():
                self._clock.sleep(0.01)
        with self._lock:
            error = task.invocation.error
            result = task.invocation.result
        if error is not None:
            raise error
        if not isinstance(result, TranscriptResult):
            raise ProtocolError("ASR runtime returned a malformed transcript result")
        return result

    def _await_quiescence(self, task: _Task, deadline: float | None) -> None:
        effective_deadline = task.deadline if deadline is None else min(task.deadline, deadline)
        while not task.done.is_set():
            if effective_deadline is not None and self._clock.monotonic() >= effective_deadline:
                raise RuntimeRecoveryPendingError("ASR request quiescence exceeded its deadline")
            wait_time = 0.01
            if effective_deadline is not None:
                wait_time = min(wait_time, max(0.0, effective_deadline - self._clock.monotonic()))
            task.done.wait(wait_time)
            if not task.done.is_set():
                self._clock.sleep(wait_time)

    def _cancel_task(self, task: _Task) -> None:
        with self._lock:
            self._cancel_task_locked(task, CancellationError("ASR request was cancelled"))

    def _cancel_task_locked(self, task: _Task, error: CancellationError) -> None:
        if self._admitted.get(task.generation) is not task:
            return
        task.cancellation.set()
        if task.terminal.is_set():
            return
        self._publish_terminal_locked(task, error=error)
        if task.state is _TaskState.ADMITTED:
            self._pending.discard(task.generation)
            task.state = _TaskState.QUIESCENT
            task.done.set()
            if task.auto_release:
                self._release_task_locked(task)

    def _timeout_task(self, task: _Task) -> None:
        with self._lock:
            if self._admitted.get(task.generation) is not task:
                return
            task.cancellation.set()
            if not task.terminal.is_set():
                self._publish_terminal_locked(
                    task, error=AsrTimeoutError("ASR request deadline exceeded")
                )

    def _publish_terminal_locked(
        self,
        task: _Task,
        *,
        result: TranscriptResult | object | None = None,
        error: AsrError | None = None,
    ) -> bool:
        if (
            self._admitted.get(task.generation) is not task
            or task.state is _TaskState.RELEASED
            or task.terminal.is_set()
        ):
            return False
        task.invocation.result = result
        task.invocation.error = error
        task.terminal.set()
        return True

    def _release_task(self, task: _Task) -> None:
        with self._lock:
            if task.state is _TaskState.RELEASED:
                return
            if not task.done.is_set():
                raise RuntimeRecoveryPendingError("ASR request must be quiescent before release")
            self._release_task_locked(task)

    def _release_task_locked(self, task: _Task) -> None:
        if task.state is _TaskState.RELEASED:
            return
        if not task.done.is_set():
            return
        if self._admitted.get(task.generation) is task:
            del self._admitted[task.generation]
        task.state = _TaskState.RELEASED

    def _finish_legacy_request(self, task: _Task) -> None:
        with self._lock:
            if task.state is _TaskState.RELEASED:
                return
            if task.done.is_set():
                self._release_task_locked(task)
            else:
                task.auto_release = True

    def _request_cancelled(self, task: _Task) -> bool:
        token = task.request.cancellation
        return token is not None and token.is_cancelled()

    def _drain_pending_locked(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                return
            if item is not _STOP:
                task = cast(_Task, item)
                self._pending.discard(task.generation)
                if task.state not in {_TaskState.QUIESCENT, _TaskState.RELEASED}:
                    self._cancel_task_locked(task, CancellationError("ASR service is closing"))
                    task.state = _TaskState.QUIESCENT
                    task.done.set()
                    if task.auto_release:
                        self._release_task_locked(task)
            self._queue.task_done()

    def _validate_request(self, request: AsrRequest) -> None:
        if not isinstance(request, AsrRequest):
            raise InvalidInputError("ASR request must be an AsrRequest")
        if request.audio.byte_length == 0:
            raise InvalidInputError("ASR audio must not be empty")


ApplicationAsrService = AsrApplicationService
