"""Imported-media transcription use case and its single worker."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from inspect import signature
from pathlib import Path
from threading import Event, Lock, RLock, Thread, current_thread

from voiceink_win.domain import (
    AsrRequest,
    AsrTimeoutError,
    CancellationError,
    CancellationReason,
    Cancelled,
    CleanupWarningError,
    EndOfStream,
    ErrorCode,
    Failed,
    HistoryPort,
    HistoryRecord,
    HistoryStatus,
    ImportedDeadlineExceededError,
    ImportedProcessingError,
    ImportedTranscriptionResult,
    ImportJob,
    ImportObservation,
    ImportOptions,
    ImportRecoveryPendingError,
    ImportShutdownError,
    JobId,
    JobWorkspace,
    MediaNormalizer,
    MediaSnapshotStore,
    MonotonicClock,
    NormalizationFailedError,
    NormalizedAudio,
    ProcessingMetadata,
    ProgressSnapshot,
    ProtocolError,
    RuntimeDiagnostics,
    RuntimeUnavailableError,
    SourceMedia,
    Stage,
    StageTiming,
    Success,
    TerminalResult,
    TranscriptionSource,
    TranscriptResult,
    TranscriptVariant,
    WarningCode,
    safe_message,
)
from voiceink_win.domain.errors import BackendUnavailableError, ProcessCrashedError
from voiceink_win.domain.imported_errors import (
    InvalidSourceError,
    ResourceLimitExceededError,
    ResourceLimitRejectedError,
    RuntimeProtocolFailureError,
    SourceChangedError,
    TranscriptionFailedError,
    UnsupportedMediaError,
)

from .asr_service import AsrApplicationService, AsrRequestHandle, QuiescenceFence
from .cancellation import CancellationTokenSource
from .import_queue import ImportQueue, ReservationToken

logger = logging.getLogger(__name__)


def _set_event() -> Event:
    event = Event()
    event.set()
    return event


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class _ServiceState:
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(slots=True)
class _Record:
    job: ImportJob
    source: SourceMedia
    options: ImportOptions
    token: ReservationToken
    cancellation: CancellationTokenSource = field(default_factory=CancellationTokenSource)
    workspace: JobWorkspace | None = None
    snapshot: object | None = None
    normalized: NormalizedAudio | None = None
    normalized_duration: float | None = None
    transcript: TranscriptResult | None = None
    failure: ImportedProcessingError | None = None
    stage_started: dict[Stage, float] = field(default_factory=dict)
    stage_timings: list[StageTiming] = field(default_factory=list)
    attempt_count: int = 1
    retry_timer: Thread | None = None
    retry_wakeup: Event = field(default_factory=Event)
    cleanup_entered_at: float | None = None
    cleanup_lock: Lock = field(default_factory=Lock)
    cleanup_done: Event | None = None
    cleanup_result: bool | None = None
    cleanup_completed_at: float | None = None
    cleanup_thread: Thread | None = None
    history_persisted: bool = False
    history_persist_lock: Lock = field(default_factory=Lock)
    history_persistence_warning: bool = False
    stage_owner_done: Event = field(default_factory=_set_event)
    cleanup_fenced: bool = False
    deadline_stage: Stage | None = None
    source_released: bool = False
    source_release_lock: Lock = field(default_factory=Lock)
    reservation_released: bool = False
    reservation_release_lock: Lock = field(default_factory=Lock)
    cleanup_warning: bool = False
    deadline_interrupt_started: float | None = None
    asr_handle: AsrRequestHandle | None = None
    asr_handle_released: bool = False


class _JobObservation:
    """Closeable, read-only view over one existing import job."""

    def __init__(self, service: ImportedMediaTranscriptionService, job_id: JobId) -> None:
        self._service = service
        self._job_id = job_id
        self._closed = Event()
        self._last_key: tuple[Stage, int, bool] | None = None
        self._terminal_emitted = False

    def next(self, timeout: float | None = None) -> ImportObservation | EndOfStream | None:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self._closed.is_set():
            record = self._service._record(self._job_id)
            job = record.job
            with job.lock:
                stage = job.stage
                attempt = job.attempt
                terminal = job.result
                done = job.done.is_set()
            key = (stage, attempt.value, done)
            if done and self._terminal_emitted:
                return EndOfStream()
            if key != self._last_key:
                self._last_key = key
                self._terminal_emitted = done
                return ImportObservation(
                    self._job_id,
                    attempt,
                    ProgressSnapshot(stage),
                    terminal if done else None,
                )
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
            else:
                remaining = 0.05
            self._closed.wait(min(0.05, remaining))
        return EndOfStream()

    def close(self) -> None:
        self._closed.set()


class ImportedMediaTranscriptionService:
    """Submit, cancel, and synchronously wait for imported-media jobs."""

    def __init__(
        self,
        normalizer: MediaNormalizer,
        asr: AsrApplicationService,
        workspace_store: MediaSnapshotStore,
        *,
        queue: ImportQueue | None = None,
        clock: MonotonicClock | None = None,
        queue_capacity: int = 8,
        processing_deadline_seconds: float = 45 * 60,
        stage_timeout_seconds: float = 30 * 60,
        cleanup_timeout_seconds: float = 5 * 60,
        max_source_bytes: int = 2 * 1024**3,
        max_completed_records: int = 256,
        retry_backoff: Callable[[int], float] | None = None,
        history_port: HistoryPort | None = None,
        history_persist_retries: int = 3,
        history_persist_timeout_seconds: float = 5.0,
    ) -> None:
        self._normalizer = normalizer
        self._asr = asr
        self._store = workspace_store
        self._queue = queue or ImportQueue(queue_capacity)
        self._clock = clock or SystemClock()
        self._processing_deadline = processing_deadline_seconds
        self._stage_timeout = stage_timeout_seconds
        self._cleanup_timeout = cleanup_timeout_seconds
        self._max_source_bytes = max_source_bytes
        if max_completed_records < 1:
            raise ValueError("completed record limit must be positive")
        self._max_completed_records = max_completed_records
        self._retry_backoff = retry_backoff or (
            lambda attempt: min(30.0, 0.1 * (2 ** (attempt - 1)))
        )
        self._history_port = history_port
        if history_persist_retries < 1:
            raise ValueError("history persistence retries must be positive")
        if history_persist_timeout_seconds <= 0:
            raise ValueError("history persistence timeout must be positive")
        self._history_persist_retries = history_persist_retries
        self._history_persist_timeout = history_persist_timeout_seconds
        self._records: dict[JobId, _Record] = {}
        self._completed: deque[JobId] = deque()
        self._recovery_workspaces: set[JobWorkspace] = set()
        self._snapshot_recovery: dict[JobId, object] = {}
        self._source_recovery: dict[JobId, SourceMedia] = {}
        self._lock = RLock()
        self._stopping = Event()
        self._retry_threads: set[Thread] = set()
        self._cleanup_threads: set[Thread] = set()
        self._stage_owner_threads: set[Thread] = set()
        self._cleanup_workspaces_inflight: set[JobWorkspace] = set()
        self._reconciliation_workspaces_inflight: set[JobWorkspace] = set()
        self._store_close_thread: Thread | None = None
        self._store_close_error: BaseException | None = None
        self._store_close_succeeded = False
        self._state = _ServiceState.OPEN
        self._shutdown_deadline: float | None = None
        self._max_result_text_bytes = 4 * 1024 * 1024
        self._store.sweep_orphans(max_age_seconds=24 * 60 * 60)
        self._worker = Thread(target=self._worker_loop, name="import-media-worker", daemon=True)
        self._monitor = Thread(
            target=self._deadline_loop, name="import-deadline-monitor", daemon=True
        )
        self._worker.start()
        self._monitor.start()

    def submit(self, path: str, options: ImportOptions | None = None) -> JobId:
        options = options or ImportOptions()
        with self._lock:
            if self._state is not _ServiceState.OPEN:
                raise RuntimeUnavailableError("import service is closing")
        try:
            source = self._store.validate_source(Path(path), max_bytes=self._max_source_bytes)
        except ResourceLimitRejectedError:
            raise
        except (InvalidSourceError, ResourceLimitExceededError):
            raise
        except OSError as error:
            raise InvalidSourceError("source is not a permitted local file", cause=error) from error

        with self._lock:
            if self._state is not _ServiceState.OPEN:
                self._release_source(source)
                raise RuntimeUnavailableError("import service is closing")
            return self._enqueue_admitted(source, options)

    def _enqueue_admitted(self, source: SourceMedia, options: ImportOptions) -> JobId:
        job_id = JobId.new()
        token: ReservationToken | None = None
        job: ImportJob | None = None
        workspace: JobWorkspace | None = None
        try:
            token = self._queue.reserve(job_id)
            job = ImportJob(job_id, deadline=self._clock.monotonic() + self._processing_deadline)
            workspace = self._store.create_workspace(job.job_id, job.attempt.value)
            record = _Record(job, source, options, token, workspace=workspace)
            self._records[job.job_id] = record
            job.transition(job.attempt, Stage.ACCEPTED, Stage.QUEUED)
            self._queue.enqueue(record, token)
        except Exception as error:
            if workspace is None:
                workspace = getattr(error, "partial_workspace", None)
            if workspace is not None:
                if not self._best_effort_cleanup(workspace):
                    with self._lock:
                        self._recovery_workspaces.add(workspace)
            if token is not None:
                try:
                    token.release(job_id)
                except BaseException as cleanup_error:
                    error.add_note(f"reservation rollback failed: {cleanup_error}")
            try:
                self._release_source(source)
            except BaseException as cleanup_error:
                error.add_note(f"source handle release failed: {cleanup_error}")
            self._records.pop(job_id, None)
            raise
        return job.job_id

    def submit_and_wait(
        self, path: str, options: ImportOptions | None = None, timeout: float | None = None
    ) -> TerminalResult:
        return self.wait(self.submit(path, options), timeout=timeout)

    def wait(self, job_id: JobId, timeout: float | None = None) -> TerminalResult:
        record = self._record(job_id)
        if not record.job.done.wait(timeout):
            raise TimeoutError("import job did not finish before timeout")
        return record.job.result

    def observe(self, job_id: JobId) -> _JobObservation:
        """Observe one admitted job without creating another worker queue."""
        self._record(job_id)
        return _JobObservation(self, job_id)

    def consume(self, job_id: JobId, timeout: float | None = None) -> TerminalResult:
        result = self.wait(job_id, timeout)
        with self._lock:
            record = self._records.get(job_id)
            if record is not None and self._can_evict_completed(record):
                self._records.pop(job_id, None)
                try:
                    self._completed.remove(job_id)
                except ValueError:
                    pass
        return result

    def cancel(self, job_id: JobId) -> bool:
        record = self._record(job_id)
        with self._lock:
            accepted = record.job.request_cancellation(CancellationReason.USER)
            if not accepted:
                return False
            record.cancellation.cancel()
            self._interrupt_active_stage(record)
            record.retry_wakeup.set()
            if record.job.stage is Stage.CLEANING_UP:
                if record.cleanup_entered_at is None:
                    record.cleanup_entered_at = self._clock.monotonic()
                self._enqueue_cleanup_if_requested(record)
        return True

    def transition(
        self, job_id: JobId, expected_attempt, expected_state: Stage, new_state: Stage
    ) -> bool:
        """Expose the aggregate CAS operation without bypassing its job lock."""
        return self._record(job_id).job.transition(expected_attempt, expected_state, new_state)

    def check_deadlines(self) -> None:
        now = self._clock.monotonic()
        with self._lock:
            records = tuple(self._records.values())
        for record in records:
            if record.job.stage is Stage.CLEANING_UP:
                if record.cleanup_entered_at is not None and now >= self._cleanup_deadline(record):
                    self._fence_cleanup(record)
                    self._publish_cleanup_fence(record)
                continue
            if now < record.job.deadline or record.job.is_terminal():
                continue
            record.job.request_deadline()
            record.cancellation.cancel()
            record.retry_wakeup.set()
            self._enqueue_cleanup_if_requested(record)
            self._interrupt_active_stage(record)
            self._await_stage_owner(record, now)

    def close(self, timeout: float = 5.0, *, close_asr: bool | None = None) -> None:
        if close_asr is not None and not isinstance(close_asr, bool):
            raise ValueError("close_asr must be boolean or None")
        should_close_asr = close_asr is not False
        with self._lock:
            if self._state is _ServiceState.CLOSED:
                return
            self._state = _ServiceState.CLOSING
            records = tuple(self._records.values())
        close_deadline = self._clock.monotonic() + timeout
        with self._lock:
            self._shutdown_deadline = close_deadline

        def remaining() -> float:
            return max(0.0, close_deadline - self._clock.monotonic())

        shutdown_error: BaseException | None = None
        try:
            with self._lock:
                for record in records:
                    if record.job.is_terminal():
                        continue
                    if record.job.request_cancellation(CancellationReason.SHUTDOWN):
                        record.cancellation.cancel()
                    record.retry_wakeup.set()
                    self._enqueue_cleanup_if_requested(record)
            with self._lock:
                self._stopping.set()
            self._queue.close()

            with self._lock:
                retry_threads = tuple(self._retry_threads)
            for retry_thread in retry_threads:
                if retry_thread is not current_thread():
                    retry_thread.join(remaining())
            self._worker.join(remaining())
            self._monitor.join(remaining())
            if self._worker.is_alive() or self._monitor.is_alive():
                shutdown_error = ImportShutdownError(
                    "import service workers did not stop before close deadline"
                )
            with self._lock:
                retry_threads = tuple(self._retry_threads)
            for retry_thread in retry_threads:
                if retry_thread is not current_thread():
                    retry_thread.join(remaining())
            if any(thread.is_alive() for thread in retry_threads) and shutdown_error is None:
                shutdown_error = ImportShutdownError(
                    "import retry workers did not stop before close deadline"
                )
            with self._lock:
                store_close_thread = self._store_close_thread
                cleanup_threads = tuple(
                    thread for thread in self._cleanup_threads if thread is not store_close_thread
                )
            for cleanup_thread in cleanup_threads:
                cleanup_thread.join(remaining())
            if any(thread.is_alive() for thread in cleanup_threads) and shutdown_error is None:
                shutdown_error = ImportShutdownError(
                    "import cleanup workers did not stop before close deadline"
                )
            with self._lock:
                stage_owner_threads = tuple(self._stage_owner_threads)
            for stage_owner_thread in stage_owner_threads:
                stage_owner_thread.join(remaining())
            if any(thread.is_alive() for thread in stage_owner_threads) and shutdown_error is None:
                shutdown_error = ImportShutdownError(
                    "import stage reapers did not stop before close deadline"
                )

            if shutdown_error is None:
                for record in tuple(self._records.values()):
                    if record.job.is_terminal():
                        continue
                    if record.job.stage is not Stage.CLEANING_UP:
                        self._enter_cleanup(record, record.job.stage)
                    self._cleanup_and_publish(record, deadline=close_deadline)
            while True:
                self._retry_recovery_workspaces(deadline=close_deadline)
                self._retry_snapshot_recovery()
                with self._lock:
                    unrecovered = bool(
                        self._recovery_workspaces
                        or self._snapshot_recovery
                        or self._source_recovery
                    )
                if not unrecovered or remaining() <= 0:
                    break
                self._clock.sleep(min(0.01, remaining()))
            with self._lock:
                non_terminal = any(
                    not record.job.is_terminal() for record in self._records.values()
                )
                unrecovered = bool(
                    self._recovery_workspaces or self._snapshot_recovery or self._source_recovery
                )
            if non_terminal and shutdown_error is None:
                shutdown_error = ImportShutdownError(
                    "import job recovery did not reach terminal state before close"
                )
            if unrecovered and shutdown_error is None:
                shutdown_error = ImportRecoveryPendingError(
                    "import workspace recovery did not complete before close"
                )
            close_store = getattr(self._store, "close", None)
            if shutdown_error is None and close_store is not None:
                shutdown_error = self._close_store_bounded(close_store, close_deadline)
        except BaseException as error:
            shutdown_error = error
        finally:
            with self._lock:
                cleanup_threads = tuple(self._cleanup_threads)
                retry_threads = tuple(self._retry_threads)
                stage_owner_threads = tuple(self._stage_owner_threads)
            threads_stopped = not (
                self._worker.is_alive()
                or self._monitor.is_alive()
                or any(thread.is_alive() for thread in cleanup_threads)
                or any(thread.is_alive() for thread in retry_threads)
                or any(thread.is_alive() for thread in stage_owner_threads)
            )
            if threads_stopped and should_close_asr:
                try:
                    self._asr.close(deadline=close_deadline)
                except BaseException as error:
                    if shutdown_error is None:
                        shutdown_error = error
            if threads_stopped and shutdown_error is None:
                with self._lock:
                    self._state = _ServiceState.CLOSED
                    self._shutdown_deadline = None
        if shutdown_error is not None:
            raise shutdown_error

    def _close_store_bounded(
        self, close_store: Callable[..., None], deadline: float
    ) -> BaseException | None:
        with self._lock:
            thread = self._store_close_thread
            if thread is not None and not thread.is_alive():
                if self._store_close_succeeded:
                    return None
                # A completed attempt must not poison a later close retry.
                self._store_close_thread = None
                self._store_close_error = None
                thread = None
            if thread is None:

                def close_store_worker() -> None:
                    error: BaseException | None = None
                    try:
                        close_store(timeout=max(0.0, deadline - self._clock.monotonic()))
                    except BaseException as caught:
                        error = caught
                    finally:
                        with self._lock:
                            self._store_close_error = error
                            self._store_close_succeeded = error is None
                            self._cleanup_threads.discard(current_thread())

                thread = Thread(target=close_store_worker, name="import-store-close", daemon=True)
                self._store_close_thread = thread
                self._store_close_error = None
                self._store_close_succeeded = False
                self._cleanup_threads.add(thread)
                thread.start()
        thread.join(max(0.0, deadline - self._clock.monotonic()))
        with self._lock:
            if thread.is_alive():
                return ImportRecoveryPendingError("import store cleanup is still pending")
            error = self._store_close_error
        if error is not None:
            shutdown_error = ImportShutdownError("import store cleanup failed during shutdown")
            shutdown_error.add_note(str(error))
            return shutdown_error
        return None

    def _record(self, job_id: JobId) -> _Record:
        with self._lock:
            try:
                return self._records[job_id]
            except KeyError as error:
                raise KeyError(f"unknown import job: {job_id}") from error

    def _worker_loop(self) -> None:
        while not self._stopping.is_set() or self._queue.reservation_count:
            entry = self._queue.get(timeout=0.05)
            if entry is None:
                self.check_deadlines()
                continue
            try:
                if not isinstance(entry, _Record):
                    continue
                record = entry
                if record.job.is_terminal():
                    continue
                if record.job.stage is Stage.CLEANING_UP:
                    self._cleanup_and_publish(record)
                    continue
                if record.job.stage is Stage.RETRY_WAITING:
                    continue
                self._run_attempt(record)
            except BaseException as error:
                if isinstance(entry, _Record):
                    self._safe_worker_finalizer(entry, error)

    def _run_attempt(self, record: _Record) -> None:
        job = record.job
        attempt = job.attempt
        if not job.transition(attempt, Stage.QUEUED, Stage.NORMALIZING):
            if job.stage is Stage.CLEANING_UP:
                self._cleanup_and_publish(record)
            return
        stage_deadline = self._start_stage(record, Stage.NORMALIZING)
        normalized_result: NormalizedAudio | None = None
        normalized_committed = False
        try:
            if self._record_deadline_if_expired(record, Stage.NORMALIZATION):
                self._enter_cleanup(record, Stage.NORMALIZING)
                self._cleanup_and_publish(record)
                return
            assert record.workspace is not None
            with self._workspace_processing_lock(record.workspace):
                snapshot = self._store.snapshot(
                    record.source,
                    record.workspace,
                    cancellation=record.cancellation.token,
                    deadline=stage_deadline,
                )
                snapshot = self._store.verify(
                    snapshot,
                    cancellation=record.cancellation.token,
                    deadline=stage_deadline,
                )
                record.snapshot = snapshot
                normalized_result = self._normalizer.normalize(
                    snapshot,
                    record.workspace,
                    record.cancellation.token,
                    stage_deadline,
                )
            self._finish_stage(record, Stage.NORMALIZING)
            if record.stage_owner_done.is_set():
                normalized_committed = self._accept_stage_result(
                    record, attempt, Stage.NORMALIZING, normalized_result
                )
            self._release_snapshot_if_owned(record)
            if not normalized_committed or not record.stage_owner_done.is_set():
                self._enter_cleanup(record, Stage.NORMALIZING)
                self._cleanup_and_publish(record)
                return
            self._raise_if_interrupted(record, Stage.NORMALIZATION)
        except Exception as error:
            self._finish_stage(record, Stage.NORMALIZING)
            self._release_snapshot_if_owned(record)
            deadline_expired = self._record_deadline_if_expired(record, Stage.NORMALIZATION)
            if isinstance(error, CancellationError):
                record.job.request_cancellation(CancellationReason.USER)
                record.cancellation.cancel()
            if deadline_expired:
                record.failure = ImportedDeadlineExceededError(
                    "import processing deadline exceeded", cause=error
                )
                self._enter_cleanup(record, Stage.NORMALIZING)
            elif self._is_cancelled(record):
                self._enter_cleanup(record, Stage.NORMALIZING)
            else:
                record.failure = self._map_normalization_error(error)
                self._enter_cleanup(record, Stage.NORMALIZING)
            self._cleanup_and_publish(record)
            return
        finally:
            if not normalized_committed:
                normalized_result = None
                record.normalized = None

        if not job.transition(attempt, Stage.NORMALIZING, Stage.TRANSCRIBING):
            record.normalized = None
            job.force_cleanup(attempt)
            self._cleanup_and_publish(record)
            return
        stage_deadline = self._start_stage(record, Stage.TRANSCRIBING)
        transcript_result: TranscriptResult | None = None
        transcript_committed = False
        try:
            assert record.normalized is not None
            request = AsrRequest(
                record.normalized.audio,
                request_id=f"{job.job_id.value}:{attempt.value}",
                language=record.options.language,
                include_timestamps=record.options.include_timestamps,
                deadline=stage_deadline,
                cancellation=record.cancellation.token,
            )
            record.normalized = None
            record.asr_handle = self._asr.try_admit(request)
            record.asr_handle_released = False
            with self._workspace_processing_lock(record.workspace):
                transcript_result = record.asr_handle.await_result(stage_deadline)
            self._finish_stage(record, Stage.TRANSCRIBING)
            if record.stage_owner_done.is_set():
                transcript_committed = self._accept_stage_result(
                    record, attempt, Stage.TRANSCRIBING, transcript_result
                )
            if not transcript_committed:
                record.job.force_cleanup(attempt)
                self._cleanup_and_publish(record)
                return
            self._raise_if_interrupted(record, Stage.TRANSCRIPTION)
        except Exception as error:
            self._finish_stage(record, Stage.TRANSCRIBING)
            deadline_expired = self._record_deadline_if_expired(record, Stage.TRANSCRIPTION)
            if isinstance(error, CancellationError):
                record.job.request_cancellation(CancellationReason.USER)
                record.cancellation.cancel()
            if deadline_expired:
                record.failure = ImportedDeadlineExceededError(
                    "import processing deadline exceeded", cause=error
                )
                self._enter_cleanup(record, Stage.TRANSCRIBING)
            elif self._is_cancelled(record):
                self._enter_cleanup(record, Stage.TRANSCRIBING)
            elif self._can_retry(record, error):
                self._retry(record)
                return
            else:
                record.failure = self._map_transcription_error(error)
                self._enter_cleanup(record, Stage.TRANSCRIBING)
            self._cleanup_and_publish(record)
            return
        finally:
            if not transcript_committed:
                transcript_result = None
                record.transcript = None

        self._enter_cleanup(record, Stage.TRANSCRIBING)
        self._cleanup_and_publish(record)

    def _accept_stage_result(
        self,
        record: _Record,
        attempt,
        stage: Stage,
        result: NormalizedAudio | TranscriptResult,
    ) -> bool:
        if not record.job.accepts_stage_result(attempt, stage):
            return False
        if stage is Stage.NORMALIZING:
            record.normalized = result
            record.normalized_duration = result.duration
        else:
            record.transcript = result
        return True

    def _retry(self, record: _Record) -> None:
        job = record.job
        old_workspace = record.workspace
        record.retry_wakeup.clear()
        if not job.transition(job.attempt, Stage.TRANSCRIBING, Stage.RETRY_WAITING):
            self._cleanup_and_publish(record)
            return
        if not self._cleanup_workspace(record):
            record.failure = CleanupWarningError("temporary workspace cleanup failed")
            job.transition(job.attempt, Stage.RETRY_WAITING, Stage.CLEANING_UP)
            self._cleanup_and_publish(record)
            return
        with self._lock:
            if old_workspace is not None:
                self._recovery_workspaces.discard(old_workspace)
        if self._is_cancelled(record) or self._clock.monotonic() >= job.deadline:
            if not self._is_cancelled(record):
                job.request_deadline()
            job.force_cleanup(job.attempt)
            self._cleanup_and_publish(record)
            return
        attempt = job.prepare_retry(job.attempt)
        if attempt is None:
            self._cleanup_and_publish(record)
            return
        record.attempt_count = attempt.value
        record.cleanup_entered_at = None
        try:
            record.workspace = self._store.create_workspace(job.job_id, attempt.value)
            record.cleanup_done = None
            record.cleanup_result = None
            record.cleanup_completed_at = None
        except Exception as error:
            record.workspace = getattr(error, "partial_workspace", None)
            record.failure = ResourceLimitExceededError(
                "retry workspace could not be created", cause=error
            )
            job.transition(attempt, Stage.RETRY_WAITING, Stage.CLEANING_UP)
            self._cleanup_and_publish(record)
            return
        delay = self._retry_backoff(attempt.value - 1)
        record.retry_timer = Thread(target=self._retry_after, args=(record, delay), daemon=True)
        with self._lock:
            self._retry_threads.add(record.retry_timer)
            # Register and start under one lock so close() cannot observe an
            # unstarted thread and race its join.
            record.retry_timer.start()

    def _retry_after(self, record: _Record, delay: float) -> None:
        try:
            deadline = self._clock.monotonic() + delay
            while not self._stopping.is_set():
                remaining = deadline - self._clock.monotonic()
                if remaining <= 0:
                    break
                if record.retry_wakeup.wait(min(remaining, 0.05)):
                    break
            with self._lock:
                if self._stopping.is_set() or record.job.is_terminal():
                    return
                self.check_deadlines()
                if record.job.stage is Stage.CLEANING_UP:
                    self._enqueue_cleanup_if_requested(record)
                    return
                if record.job.transition(record.job.attempt, Stage.RETRY_WAITING, Stage.QUEUED):
                    self._queue.enqueue_committed(record, record.token)
                elif record.job.stage is Stage.CLEANING_UP:
                    self._enqueue_cleanup_if_requested(record)
        finally:
            with self._lock:
                self._retry_threads.discard(current_thread())

    def _cleanup_and_publish(self, record: _Record, *, deadline: float | None = None) -> None:
        if record.job.stage is not Stage.CLEANING_UP:
            return
        if not record.stage_owner_done.is_set():
            with self._lock:
                if record.workspace is not None:
                    self._recovery_workspaces.add(record.workspace)
            return
        if not self._cleanup_workspace(record, deadline=deadline):
            if record.cleanup_fenced:
                self._publish_cleanup_fence(record)
                return
            if record.failure is None:
                record.failure = CleanupWarningError("temporary workspace cleanup failed")
            else:
                record.cleanup_warning = True
            with self._lock:
                if record.workspace is not None:
                    self._recovery_workspaces.add(record.workspace)
            return
        self._publish_after_cleanup(record)

    def _publish_after_cleanup(self, record: _Record) -> None:
        if record.job.stage is not Stage.CLEANING_UP:
            return
        if not record.stage_owner_done.is_set():
            with self._lock:
                if record.workspace is not None:
                    self._recovery_workspaces.add(record.workspace)
            return
        with record.cleanup_lock:
            if (
                record.workspace is not None
                and record.cleanup_result is True
                and (
                    record.cleanup_completed_at is None
                    or record.cleanup_completed_at >= self._cleanup_deadline(record)
                )
            ):
                self._fence_cleanup_locked(record, record.workspace, record.cleanup_done)
                record.failure = CleanupWarningError("cleanup completed after its deadline")
                record.cleanup_warning = True
        if record.cleanup_fenced and record.workspace is not None and record.cleanup_done is None:
            with self._lock:
                self._recovery_workspaces.add(record.workspace)
            return
        try:
            self._release_source(record)
        except BaseException as error:
            record.cleanup_warning = True
            if record.failure is None:
                record.failure = CleanupWarningError("source handle cleanup failed", cause=error)
        with self._lock:
            workspace = record.workspace
            if workspace is not None and record.cleanup_result is True:
                record.workspace = None
                self._recovery_workspaces.discard(workspace)
        job = record.job
        result = self._make_terminal_result(record)
        self._persist_history_once(record, result)
        result = self._make_terminal_result(record)
        if job.complete(job.attempt, Stage.CLEANING_UP, result):
            record.normalized = None
            record.transcript = None
            self._release_terminal_reservation(record)
            self._retain_completed(record)
        elif job.stage is Stage.CLEANING_UP:
            # Cancellation/deadline intent may have won after result construction.
            result = self._make_terminal_result(record)
            if job.complete(job.attempt, Stage.CLEANING_UP, result):
                record.normalized = None
                record.transcript = None
                self._release_terminal_reservation(record)
                self._retain_completed(record)
            elif job.is_terminal():
                self._release_terminal_reservation(record)
        if job.is_terminal():
            self._release_terminal_reservation(record)
            try:
                self._release_source(record)
            except BaseException as error:
                record.cleanup_warning = True
                if record.failure is None:
                    record.failure = CleanupWarningError(
                        "source handle cleanup failed", cause=error
                    )
        with self._lock:
            self._prune_completed_locked()

    def _recover_worker_failure(self, record: _Record, error: BaseException) -> None:
        if record.job.is_terminal():
            return
        record.failure = NormalizationFailedError("worker failure", cause=error)
        if record.job.stage in {
            Stage.QUEUED,
            Stage.NORMALIZING,
            Stage.TRANSCRIBING,
            Stage.RETRY_WAITING,
        }:
            self._enter_cleanup(record, record.job.stage)
        elif record.job.stage is not Stage.CLEANING_UP:
            self._force_terminal_failure(record, error)
            return
        try:
            self._cleanup_and_publish(record)
        except BaseException as cleanup_error:
            self._force_terminal_failure(record, cleanup_error)

    def _safe_worker_finalizer(self, record: _Record, error: BaseException) -> None:
        try:
            self._recover_worker_failure(record, error)
        except BaseException as finalizer_error:
            try:
                self._force_terminal_failure(record, finalizer_error)
            except BaseException:
                if record.stage_owner_done.is_set():
                    try:
                        self._release_snapshot_if_owned(record)
                    except BaseException:
                        pass
                with self._lock:
                    if record.workspace is not None:
                        self._recovery_workspaces.add(record.workspace)

    def _force_terminal_failure(self, record: _Record, reason: str | BaseException) -> None:
        if record.job.is_terminal():
            return
        if not record.stage_owner_done.is_set():
            record.failure = CleanupWarningError("worker failure cleanup deferred", cause=reason)
            record.cleanup_warning = True
            record.cleanup_fenced = True
            record.job.force_cleanup(record.job.attempt)
            with self._lock:
                if record.workspace is not None:
                    self._recovery_workspaces.add(record.workspace)
            self._publish_fenced_terminal(record)
            return
        snapshot_release_failed = False
        if record.snapshot is not None:
            try:
                self._release_snapshot_if_owned(record)
            except BaseException:
                snapshot_release_failed = True
        cleanup_ok = self._cleanup_workspace(record)
        if record.failure is None:
            record.failure = NormalizationFailedError("worker failure", cause=reason)
        if snapshot_release_failed or not cleanup_ok:
            if record.failure is None:
                record.failure = CleanupWarningError("cleanup failed during recovery", cause=reason)
            else:
                record.cleanup_warning = True
        try:
            self._release_source(record)
        except BaseException as error:
            record.cleanup_warning = True
            if record.failure is None:
                record.failure = CleanupWarningError("source handle cleanup failed", cause=error)
        record.job.force_cleanup(record.job.attempt)
        result = self._make_terminal_result(record)
        self._persist_history_once(record, result)
        result = self._make_terminal_result(record)
        if record.job.complete(record.job.attempt, Stage.CLEANING_UP, result):
            self._release_terminal_reservation(record)
            self._retain_completed(record)
        elif record.job.is_terminal():
            self._release_terminal_reservation(record)
        if record.job.is_terminal():
            try:
                self._release_source(record)
            except BaseException:
                pass

    def _release_terminal_reservation(self, record: _Record) -> None:
        """Release only after this job, or a competing terminal CAS, won."""
        if record.job.is_terminal():
            with record.reservation_release_lock:
                if record.reservation_released:
                    return
                released = self._queue._release_committed(record.token, record.job.job_id)
                if released or record.token.state.name == "RELEASED":
                    record.reservation_released = True

    def _release_source(self, record_or_source: _Record | SourceMedia) -> None:
        if isinstance(record_or_source, _Record):
            record = record_or_source
            with record.source_release_lock:
                if record.source_released:
                    return
                try:
                    self._release_source_once(record, record.source)
                except BaseException:
                    with self._lock:
                        self._source_recovery[record.job.job_id] = record.source
                    raise
                record.source_released = True
                with self._lock:
                    self._source_recovery.pop(record.job.job_id, None)
                return
        else:
            source = record_or_source
            release = getattr(self._store, "release_source", None)
            if release is not None:
                release(source)
            return

    def _release_source_once(self, record: _Record, source: SourceMedia) -> None:
        release = getattr(self._store, "release_source", None)
        if release is not None:
            release(source)

    def _make_terminal_result(self, record: _Record) -> TerminalResult:
        job = record.job
        cancelled, deadline, reason = job.intent()
        if record.failure is not None and record.failure.code is ErrorCode.CLEANUP_WARNING:
            error = record.failure
            failure_stage = {
                "normalization": Stage.NORMALIZATION,
                "transcription": Stage.TRANSCRIPTION,
                "cleanup": Stage.CLEANUP,
            }[error.stage]
            return Failed(
                "failed",
                job.job_id,
                job.attempt,
                Stage.CLEANUP if error.code is ErrorCode.CLEANUP_WARNING else failure_stage,
                error.code,
                safe_message(error.code),
                error.retryable,
                self._result_warnings(record),
            )
        if deadline:
            failure_stage = record.deadline_stage or (
                Stage.NORMALIZATION if record.normalized is None else Stage.TRANSCRIPTION
            )
            return Failed(
                "failed",
                job.job_id,
                job.attempt,
                failure_stage,
                ErrorCode.DEADLINE_EXCEEDED,
                safe_message(ErrorCode.DEADLINE_EXCEEDED),
                False,
                self._result_warnings(record),
            )
        if cancelled:
            return Cancelled(
                "cancelled",
                job.job_id,
                job.attempt,
                Stage.CLEANING_UP,
                reason or CancellationReason.USER,
            )
        if record.failure is not None:
            error = record.failure
            failure_stage = {
                "normalization": Stage.NORMALIZATION,
                "transcription": Stage.TRANSCRIPTION,
                "cleanup": Stage.CLEANUP,
            }[error.stage]
            return Failed(
                "failed",
                job.job_id,
                job.attempt,
                failure_stage,
                error.code,
                safe_message(error.code),
                error.retryable,
                self._result_warnings(record),
            )
        assert record.transcript is not None and record.normalized_duration is not None
        metadata = ProcessingMetadata(
            record.normalized_duration,
            record.attempt_count,
            tuple(record.stage_timings),
            self._clock.monotonic() - (job.deadline - self._processing_deadline),
            Stage.SUCCEEDED,
        )
        transcription = ImportedTranscriptionResult(
            job.job_id,
            record.source.display_name,
            record.transcript,
            metadata,
            self._runtime_diagnostics(record),
        )
        return Success(
            "succeeded",
            job.job_id,
            job.attempt,
            transcription,
            self._result_warnings(record),
        )

    def _persist_history_once(self, record: _Record, result: TerminalResult) -> None:
        """Confirm durable history before terminal publication, with bounded retries."""
        if self._history_port is None:
            return
        with record.history_persist_lock:
            if record.history_persisted:
                return
            history = self._history_record(record, result)
            for attempt in range(1, self._history_persist_retries + 1):
                try:
                    self._history_port.upsert_history(history).result(
                        timeout=self._history_persist_timeout
                    )
                except BaseException:
                    logger.exception(
                        "history terminal persistence attempt failed",
                        extra={"job_id": str(record.job.job_id), "attempt": attempt},
                    )
                    if attempt < self._history_persist_retries:
                        self._clock.sleep(self._retry_backoff(attempt))
                else:
                    record.history_persisted = True
                    return
            record.history_persistence_warning = True

    def _history_record(self, record: _Record, result: TerminalResult) -> HistoryRecord:
        if isinstance(result, Success):
            transcript = result.transcription.transcription
            return HistoryRecord(
                id=record.job.job_id.value,
                source=TranscriptionSource.IMPORTED_FILE,
                duration=result.transcription.processing.normalized_duration or transcript.duration,
                original_text=transcript.text,
                enhanced_text=getattr(transcript, "enhanced_text", None),
                selected_variant=TranscriptVariant.ORIGINAL,
                status=HistoryStatus.COMPLETED,
                source_metadata=self._history_source_metadata(record),
            )
        if isinstance(result, Failed):
            return HistoryRecord(
                id=record.job.job_id.value,
                source=TranscriptionSource.IMPORTED_FILE,
                duration=record.normalized_duration or 0.0,
                status=HistoryStatus.FAILED,
                error=result.safe_message,
                failure_code=result.code.value,
                source_metadata=self._history_source_metadata(record),
            )
        return HistoryRecord(
            id=record.job.job_id.value,
            source=TranscriptionSource.IMPORTED_FILE,
            duration=record.normalized_duration or 0.0,
            status=HistoryStatus.CANCELLED,
            error="Processing was cancelled.",
            failure_code=ErrorCode.CANCELLED.value,
            source_metadata=self._history_source_metadata(record),
        )

    @staticmethod
    def _history_source_metadata(record: _Record) -> dict[str, object]:
        return {
            "job_id": record.job.job_id.value,
            "source_name": Path(record.source.display_name).name,
            "source_reference": f"sha256:{record.source.sha256}",
            "source_size": record.source.size,
            "source_sha256": record.source.sha256,
        }

    @staticmethod
    def _result_warnings(record: _Record) -> tuple[WarningCode, ...]:
        warnings: list[WarningCode] = []
        if record.cleanup_warning:
            warnings.append(WarningCode.CLEANUP_WARNING)
        if record.history_persistence_warning:
            warnings.append(WarningCode.HISTORY_PERSISTENCE_WARNING)
        return tuple(warnings)

    @contextmanager
    def _workspace_processing_lock(self, workspace: JobWorkspace):
        lock_factory = getattr(self._store, "workspace_lock", None)
        if lock_factory is None:
            yield
            return
        with lock_factory(workspace):
            yield

    def _runtime_diagnostics(self, record: _Record) -> RuntimeDiagnostics:
        artifact = getattr(self._normalizer, "artifact", None)
        manifest = getattr(artifact, "manifest", None)
        return RuntimeDiagnostics(
            request_id=f"{record.job.job_id.value}:{record.job.attempt.value}",
            ffmpeg_version=getattr(manifest, "version", None),
            ffmpeg_sha256=getattr(manifest, "sha256", None),
            ffmpeg_license=getattr(manifest, "license", None),
            ffmpeg_provenance_url=getattr(manifest, "provenance_url", None),
        )

    def _cleanup_workspace(self, record: _Record, *, deadline: float | None = None) -> bool:
        with record.cleanup_lock:
            if record.workspace is None:
                return record.cleanup_result is not False
            workspace = record.workspace
            if record.cleanup_entered_at is None:
                record.cleanup_entered_at = self._clock.monotonic()
            done = record.cleanup_done
            if done is None:
                done = Event()
                record.cleanup_done = done
                failed: list[BaseException] = []

                def cleanup() -> None:
                    try:
                        cleanup_method = self._store.cleanup
                        if "deadline" in signature(cleanup_method).parameters:
                            cleanup_method(
                                workspace,
                                deadline=self._cleanup_deadline(record, ceiling=deadline),
                            )
                        else:
                            cleanup_method(workspace)
                    except BaseException as error:
                        failed.append(error)
                    finally:
                        if not failed or not record.cleanup_fenced:
                            record.cleanup_result = not failed
                        record.cleanup_completed_at = self._clock.monotonic()
                        with self._lock:
                            self._cleanup_threads.discard(thread)
                            if failed and not record.cleanup_fenced:
                                self._recovery_workspaces.add(workspace)
                            elif not failed and not record.cleanup_fenced:
                                self._recovery_workspaces.discard(workspace)
                            if failed or not record.cleanup_fenced:
                                self._cleanup_workspaces_inflight.discard(workspace)
                        done.set()

                thread = Thread(target=cleanup, name="import-workspace-cleanup")
                record.cleanup_thread = thread
                with self._lock:
                    self._cleanup_threads.add(thread)
                    self._cleanup_workspaces_inflight.add(workspace)
                    thread.start()
            remaining = max(
                0.0,
                min(
                    self._cleanup_timeout,
                    self._cleanup_deadline(record, ceiling=deadline) - self._clock.monotonic(),
                ),
            )
            if not done.wait(remaining):
                self._fence_cleanup_locked(record, workspace, done)
                return False
            return record.cleanup_result is True

    def _cleanup_deadline(self, record: _Record, *, ceiling: float | None = None) -> float:
        entered = record.cleanup_entered_at
        if entered is None:
            entered = self._clock.monotonic()
        deadline = entered + self._cleanup_timeout
        with self._lock:
            if self._shutdown_deadline is not None:
                deadline = min(deadline, self._shutdown_deadline)
        if ceiling is not None:
            deadline = min(deadline, ceiling)
        return deadline

    def _fence_cleanup(self, record: _Record) -> None:
        with record.cleanup_lock:
            if record.workspace is not None:
                done = record.cleanup_done
                self._fence_cleanup_locked(record, record.workspace, done)

    def _fence_cleanup_locked(
        self, record: _Record, workspace: JobWorkspace, done: Event | None
    ) -> None:
        if record.cleanup_fenced:
            return
        record.cleanup_fenced = True
        record.cleanup_result = False
        if done is not None:
            done.set()
        with self._lock:
            self._recovery_workspaces.add(workspace)

    def _publish_cleanup_fence(self, record: _Record) -> None:
        if record.job.is_terminal() or record.job.stage is not Stage.CLEANING_UP:
            return
        record.failure = CleanupWarningError("cleanup deadline expired")
        record.cleanup_warning = True
        if not record.stage_owner_done.is_set():
            self._publish_fenced_terminal(record)
            return
        self._publish_after_cleanup(record)

    def _publish_fenced_terminal(self, record: _Record) -> None:
        """Publish a warning while stage-owned resources remain fenced for recovery."""
        if record.job.is_terminal():
            self._release_terminal_reservation(record)
            return
        record.job.force_cleanup(record.job.attempt)
        result = self._make_terminal_result(record)
        self._persist_history_once(record, result)
        result = self._make_terminal_result(record)
        if record.job.complete(record.job.attempt, Stage.CLEANING_UP, result):
            record.normalized = None
            record.transcript = None
            self._release_terminal_reservation(record)
            self._retain_completed(record)
        with self._lock:
            self._prune_completed_locked()

    def _interrupt_active_stage(self, record: _Record) -> None:
        if record.job.stage is Stage.NORMALIZING:
            interrupt = getattr(self._normalizer, "interrupt", None)
            if interrupt is None:
                interrupt = getattr(getattr(self._normalizer, "runner", None), "interrupt", None)
        elif record.job.stage is Stage.TRANSCRIBING:
            if record.asr_handle is not None:
                record.asr_handle.cancel()
                return
            interrupt = getattr(self._asr, "interrupt_active", None)
        else:
            interrupt = None
        if interrupt is not None:
            try:
                interrupt()
            except BaseException:
                record.cleanup_warning = True

    def _await_stage_owner(self, record: _Record, now: float) -> None:
        if record.job.stage not in {Stage.NORMALIZING, Stage.TRANSCRIBING}:
            return
        if record.deadline_interrupt_started is None:
            record.deadline_interrupt_started = now
        remaining = 0.05
        if record.stage_owner_done.wait(remaining):
            record.deadline_interrupt_started = None
            self._enter_cleanup(record, record.job.stage)
            self._cleanup_and_publish(record)
            return
        if self._clock.monotonic() - record.deadline_interrupt_started < 1.0:
            return
        # A non-cooperative adapter cannot retain queue capacity indefinitely. Keep
        # its workspace in recovery and fence its eventual late result.
        record.deadline_stage = (
            Stage.NORMALIZATION if record.job.stage is Stage.NORMALIZING else Stage.TRANSCRIPTION
        )
        record.job.force_cleanup(record.job.attempt)
        self._fence_cleanup(record)
        self._publish_cleanup_fence(record)

    def _release_snapshot(self, snapshot: object) -> None:
        release = getattr(self._store, "release_snapshot", None)
        if release is not None:
            release(snapshot)

    def _enqueue_cleanup_if_requested(self, record: _Record) -> None:
        if self._stopping.is_set():
            return
        if record.job.take_cleanup_enqueue_request():
            self._queue.enqueue_committed(record, record.token)

    def _retain_completed(self, record: _Record) -> None:
        with self._lock:
            if record.job.job_id not in self._completed:
                self._completed.append(record.job.job_id)
            self._prune_completed_locked()

    def _prune_completed_locked(self) -> None:
        while len(self._completed) > self._max_completed_records:
            evicted = False
            for _ in range(len(self._completed)):
                old_id = self._completed.popleft()
                old_record = self._records.get(old_id)
                if old_record is None or self._can_evict_completed(old_record):
                    self._records.pop(old_id, None)
                    evicted = True
                    break
                self._completed.append(old_id)
            if not evicted:
                return

    @staticmethod
    def _can_evict_completed(record: _Record) -> bool:
        cleanup_complete = record.workspace is None and (
            record.cleanup_done is None
            or (record.cleanup_done.is_set() and record.cleanup_result is True)
        )
        return (
            record.job.is_terminal()
            and record.stage_owner_done.is_set()
            and record.snapshot is None
            and cleanup_complete
            and record.source_released
        )

    def _best_effort_cleanup(
        self, workspace: JobWorkspace, *, deadline: float | None = None
    ) -> bool:
        cleanup = self._store.cleanup
        absolute_deadline = deadline or self._clock.monotonic() + self._cleanup_timeout
        with self._lock:
            if self._shutdown_deadline is not None:
                absolute_deadline = min(absolute_deadline, self._shutdown_deadline)
        errors: list[BaseException] = []
        done = Event()

        def run_cleanup() -> None:
            try:
                if "deadline" in signature(cleanup).parameters:
                    cleanup(workspace, deadline=absolute_deadline)
                else:
                    cleanup(workspace)
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()
                with self._lock:
                    self._cleanup_threads.discard(thread)

        thread = Thread(target=run_cleanup, name="import-recovery-cleanup", daemon=True)
        with self._lock:
            self._cleanup_threads.add(thread)
            thread.start()
        done.wait(max(0.0, absolute_deadline - self._clock.monotonic()))
        return done.is_set() and not errors

    def _enter_cleanup(self, record: _Record, stage: Stage) -> None:
        record.job.transition(record.job.attempt, stage, Stage.CLEANING_UP)

    def _start_stage(self, record: _Record, stage: Stage) -> float:
        record.stage_owner_done.clear()
        started = self._clock.monotonic()
        record.stage_started[stage] = started
        return min(record.job.deadline, started + self._stage_timeout)

    def _finish_stage(self, record: _Record, stage: Stage) -> None:
        started = record.stage_started.pop(stage, None)
        if started is not None:
            record.stage_timings.append(
                StageTiming(stage, max(0.0, self._clock.monotonic() - started))
            )
        owner_done = self._stage_owner_event(record, stage)
        if owner_done is None or owner_done.is_set():
            self._release_asr_handle(record)
            record.stage_owner_done.set()
            return

        def wait_for_owner() -> None:
            owner_done.wait()
            self._release_asr_handle(record)
            record.stage_owner_done.set()
            with self._lock:
                self._stage_owner_threads.discard(thread)
            try:
                self._release_snapshot_if_owned(record)
                if record.cleanup_fenced and record.job.is_terminal():
                    if record.workspace is None:
                        self._release_source(record)
                    else:
                        self._retry_recovery_workspaces()
                elif record.job.stage is Stage.CLEANING_UP:
                    self._cleanup_and_publish(record)
            except BaseException as error:
                self._safe_worker_finalizer(record, error)
            with self._lock:
                self._prune_completed_locked()

        thread = Thread(target=wait_for_owner, name="import-stage-reaper")
        with self._lock:
            self._stage_owner_threads.add(thread)
        thread.start()

    def _release_snapshot_if_owned(self, record: _Record) -> None:
        with record.cleanup_lock:
            if not record.stage_owner_done.is_set() or record.snapshot is None:
                return
            snapshot = record.snapshot
            try:
                self._release_snapshot(snapshot)
            except BaseException:
                with self._lock:
                    self._snapshot_recovery[record.job.job_id] = snapshot
                raise
            record.snapshot = None
            with self._lock:
                self._snapshot_recovery.pop(record.job.job_id, None)

    def _stage_owner_event(self, record: _Record, stage: Stage) -> Event | QuiescenceFence | None:
        if stage is Stage.TRANSCRIBING and record.asr_handle is not None:
            return record.asr_handle.quiescence_event
        owner = self._normalizer if stage is Stage.NORMALIZING else self._asr
        event = getattr(owner, "stage_owner_done", None)
        if callable(event):
            event = event()
        if isinstance(event, (Event, QuiescenceFence)):
            return event
        return None

    def _release_asr_handle(self, record: _Record) -> None:
        handle = record.asr_handle
        if handle is None or record.asr_handle_released or not handle.is_quiescent:
            return
        handle.release()
        record.asr_handle_released = True
        record.asr_handle = None

    def _raise_if_interrupted(self, record: _Record, stage: Stage) -> None:
        if self._is_cancelled(record):
            raise CancellationError("import job was cancelled")
        if self._clock.monotonic() >= record.job.deadline:
            record.job.request_deadline()
            record.deadline_stage = stage
            raise AsrTimeoutError("import job deadline exceeded")

    def _record_deadline_if_expired(self, record: _Record, stage: Stage) -> bool:
        """Linearize absolute-deadline expiry before classifying a stage failure."""
        if self._clock.monotonic() >= record.job.deadline:
            record.job.request_deadline()
            record.deadline_stage = stage
        return record.job.intent()[1]

    def _is_cancelled(self, record: _Record) -> bool:
        cancelled, deadline, _ = record.job.intent()
        return cancelled or deadline or record.cancellation.token.is_cancelled()

    def _can_retry(self, record: _Record, error: Exception) -> bool:
        return (
            record.job.attempt.value < 2
            and not self._is_cancelled(record)
            and self._clock.monotonic() < record.job.deadline
            and isinstance(
                error,
                RuntimeUnavailableError
                | AsrTimeoutError
                | ProcessCrashedError
                | BackendUnavailableError,
            )
        )

    def _map_normalization_error(self, error: Exception) -> ImportedProcessingError:
        if isinstance(error, ImportedProcessingError):
            return error
        if isinstance(error, ResourceLimitExceededError):
            return error
        if isinstance(error, SourceChangedError):
            return error
        return UnsupportedMediaError("media could not be normalized", cause=error)

    def _map_transcription_error(self, error: Exception) -> ImportedProcessingError:
        if isinstance(error, ImportedProcessingError):
            return error
        if isinstance(error, AsrTimeoutError):
            from voiceink_win.domain.imported_errors import ImportedRuntimeTimeoutError

            return ImportedRuntimeTimeoutError("transcription timed out", cause=error)
        if isinstance(
            error, RuntimeUnavailableError | ProcessCrashedError | BackendUnavailableError
        ):
            from voiceink_win.domain.imported_errors import ImportedRuntimeUnavailableError

            return ImportedRuntimeUnavailableError("transcription runtime unavailable", cause=error)
        if isinstance(error, ProtocolError):
            return RuntimeProtocolFailureError(
                "transcription runtime returned invalid data", cause=error
            )
        return TranscriptionFailedError("transcription failed", cause=error)

    def _deadline_loop(self) -> None:
        while not self._stopping.wait(0.05):
            try:
                self.check_deadlines()
                self._retry_recovery_workspaces()
                self._retry_snapshot_recovery()
            except BaseException:
                # Recovery is retried on the next tick; the monitor itself must survive.
                continue

    def _retry_recovery_workspaces(self, *, deadline: float | None = None) -> None:
        try:
            self._retry_recovery_workspaces_impl(deadline=deadline)
        except BaseException:
            # A failing cleanup adapter must not terminate deadline supervision.
            return

    def _retry_recovery_workspaces_impl(self, *, deadline: float | None = None) -> None:
        with self._lock:
            workspaces = tuple(self._recovery_workspaces)
        for workspace in workspaces:
            with self._lock:
                records = tuple(
                    record for record in self._records.values() if record.workspace == workspace
                )
            if any(not record.stage_owner_done.is_set() for record in records):
                continue
            completed = tuple(
                record
                for record in records
                if record.cleanup_done is not None
                and record.cleanup_done.is_set()
                and record.cleanup_result is True
            )
            if completed:
                self._reconcile_recovery_workspace(completed, workspace)
                continue
            with self._lock:
                if workspace in self._cleanup_workspaces_inflight:
                    continue
                self._cleanup_workspaces_inflight.add(workspace)
            cleaned = False
            try:
                cleaned = self._best_effort_cleanup(
                    workspace,
                    deadline=self._cleanup_deadline_for_recovery(records, deadline),
                )
                if cleaned:
                    self._reconcile_recovery_workspace(records, workspace)
            finally:
                with self._lock:
                    self._cleanup_workspaces_inflight.discard(workspace)
        self._retry_source_recovery()
        with self._lock:
            self._prune_completed_locked()

    def _retry_snapshot_recovery(self) -> None:
        with self._lock:
            pending = tuple(self._snapshot_recovery)
            records = dict(self._records)
        for job_id in pending:
            record = records.get(job_id)
            if record is None:
                continue
            try:
                self._release_snapshot_if_owned(record)
            except BaseException:
                continue
        with self._lock:
            self._prune_completed_locked()

    def _reconcile_recovery_workspace(
        self, records: tuple[_Record, ...], workspace: JobWorkspace
    ) -> None:
        with self._lock:
            if workspace in self._reconciliation_workspaces_inflight:
                return
            self._reconciliation_workspaces_inflight.add(workspace)
        try:
            self._reconcile_successful_cleanup(records, workspace)
        finally:
            with self._lock:
                self._reconciliation_workspaces_inflight.discard(workspace)
                self._cleanup_workspaces_inflight.discard(workspace)

    def _cleanup_deadline_for_recovery(
        self, records: tuple[_Record, ...], close_deadline: float | None
    ) -> float:
        deadline = float("inf")
        if records:
            deadline = min(self._cleanup_deadline(record) for record in records)
        else:
            deadline = self._clock.monotonic() + self._cleanup_timeout
        with self._lock:
            if self._shutdown_deadline is not None:
                deadline = min(deadline, self._shutdown_deadline)
        if close_deadline is not None:
            deadline = min(deadline, close_deadline)
        return deadline

    def _retry_source_recovery(self) -> None:
        with self._lock:
            pending = tuple(self._source_recovery.items())
            records = dict(self._records)
        for job_id, source in pending:
            record = records.get(job_id)
            try:
                if record is not None:
                    self._release_source(record)
                else:
                    self._release_source(source)
                    with self._lock:
                        self._source_recovery.pop(job_id, None)
            except BaseException:
                continue

    def _reconcile_successful_cleanup(
        self, records: tuple[_Record, ...], workspace: JobWorkspace
    ) -> None:
        """Commit ownership changes only after recovery cleanup really succeeded."""
        snapshot_release_failed = False
        with self._lock:
            for record in records:
                if record.workspace == workspace:
                    record.cleanup_result = True
                    record.cleanup_completed_at = self._clock.monotonic()
        for record in records:
            try:
                self._release_snapshot_if_owned(record)
            except BaseException as error:
                snapshot_release_failed = True
                record.cleanup_warning = True
                if record.failure is None:
                    record.failure = CleanupWarningError(
                        "snapshot handle cleanup failed", cause=error
                    )
        for record in records:
            try:
                self._release_source(record)
            except BaseException as error:
                record.cleanup_warning = True
                if record.failure is None:
                    record.failure = CleanupWarningError(
                        "source handle cleanup failed", cause=error
                    )
        if snapshot_release_failed:
            return
        with self._lock:
            for record in records:
                if record.workspace == workspace:
                    record.workspace = None
            self._recovery_workspaces.discard(workspace)
        for record in records:
            if not record.job.is_terminal():
                self._publish_after_cleanup(record)
        with self._lock:
            self._prune_completed_locked()


ImportedMediaService = ImportedMediaTranscriptionService
