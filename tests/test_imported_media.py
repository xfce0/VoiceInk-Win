from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import stat
import time
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

import voiceink_win.infrastructure.media_snapshot as media_snapshot
import voiceink_win.infrastructure.windows_snapshot as windows_snapshot
from scripts.native_smoke import _NativeSmokeTemporaryDirectory
from voiceink_win.application import (
    AsrApplicationService,
    ImportedMediaTranscriptionService,
    ImportQueue,
    ReservationState,
)
from voiceink_win.domain import (
    AsrTimeoutError,
    Attempt,
    ConfigurationError,
    ErrorCode,
    ImportRecoveryPendingError,
    ImportShutdownError,
    InvalidSourceError,
    JobId,
    JobWorkspace,
    MalformedWavError,
    ProcessCrashedError,
    QueueFullRejectedError,
    RejectedRequestError,
    ResourceLimitExceededError,
    ResourceLimitRejectedError,
    SnapshotManifest,
    SourceMedia,
    SourceSnapshot,
    Stage,
    VerifiedMediaHandle,
    WarningCode,
)
from voiceink_win.infrastructure import (
    BoundedPcmSink,
    FakeAsrRuntime,
    FakeAsrScenario,
    FakeClock,
    FakeMediaNormalizer,
    FakeMediaScenario,
    FakeSnapshotStore,
    WavLimits,
    WindowsJobObject,
    WindowsJobObjectProcessRunner,
    make_wav,
    media_process,
    validate_wav,
)
from voiceink_win.infrastructure.ffmpeg import (
    FfmpegArtifactManifest,
    SubprocessMediaNormalizer,
    VerifiedFfmpegArtifact,
)
from voiceink_win.infrastructure.media_process import ProcessResult
from voiceink_win.infrastructure.media_snapshot import (
    LocalMediaSnapshotStore,
    NativeWindowsMediaSecurityAdapter,
)
from voiceink_win.infrastructure.windows_snapshot import (
    WindowsKernel32,
    WindowsMediaSnapshotStore,
    _HandleLease,
)

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="requires the POSIX snapshot backend")


def source_file(tmp_path: Path, content: bytes = b"media") -> Path:
    path = tmp_path / "input.wav"
    path.write_bytes(content)
    return path


def service(tmp_path: Path, normalizer=None, runtime=None, **kwargs):
    normalizer = normalizer or FakeMediaNormalizer()
    runtime = runtime or FakeAsrRuntime()
    asr = AsrApplicationService(runtime)
    return ImportedMediaTranscriptionService(
        normalizer,
        asr,
        FakeSnapshotStore(tmp_path / "work"),
        **kwargs,
    )


def test_imported_audio_success_cleans_workspace_and_calls_asr(tmp_path: Path) -> None:
    normalizer = FakeMediaNormalizer()
    runtime = FakeAsrRuntime()
    application = service(tmp_path, normalizer, runtime)

    job_id = application.submit(str(source_file(tmp_path)))
    result = application.wait(job_id, timeout=2.0)
    application.close()

    assert result.status == "succeeded"
    assert result.transcription.transcription.text == "fake transcript"
    assert normalizer.calls == 1
    assert application._queue.reservation_count == 0
    assert list((tmp_path / "work").rglob("source.snapshot")) == []


def test_normalization_uses_one_stage_deadline_and_does_not_retry_after_processing_expiry(
    tmp_path: Path,
) -> None:
    clock = FakeClock(100.0)
    deadlines: list[float | None] = []

    class BoundaryStore(FakeSnapshotStore):
        def snapshot(self, *args, deadline=None, **kwargs):
            deadlines.append(deadline)
            clock.advance(10.0)
            return super().snapshot(*args, deadline=deadline, **kwargs)

        def verify(self, snapshot, *, cancellation=None, deadline=None):
            deadlines.append(deadline)
            return super().verify(snapshot, cancellation=cancellation, deadline=deadline)

    class BoundaryNormalizer(FakeMediaNormalizer):
        def normalize(self, source, workspace, cancellation, deadline):
            deadlines.append(deadline)
            return super().normalize(source, workspace, cancellation, deadline)

    store = BoundaryStore(tmp_path / "work")
    application = ImportedMediaTranscriptionService(
        BoundaryNormalizer(),
        AsrApplicationService(FakeAsrRuntime(), clock=clock),
        store,
        clock=clock,
        processing_deadline_seconds=10.0,
        stage_timeout_seconds=30.0,
    )

    result = application.submit_and_wait(str(source_file(tmp_path)), timeout=2.0)
    application.close()

    assert deadlines == [110.0, 110.0, 110.0]
    assert result.status == "failed"
    assert result.code is ErrorCode.DEADLINE_EXCEEDED
    assert result.attempt.value == 1


def test_import_service_passes_absolute_close_deadline_to_asr(tmp_path: Path) -> None:
    clock = FakeClock(50.0)

    class RecordingAsr:
        def __init__(self) -> None:
            self.deadline = None

        def close(self, *, deadline=None) -> None:
            self.deadline = deadline

    asr = RecordingAsr()
    application = ImportedMediaTranscriptionService(
        FakeMediaNormalizer(), asr, FakeSnapshotStore(tmp_path / "work"), clock=clock
    )

    application.close(timeout=7.0)

    assert asr.deadline == 57.0


def test_import_service_can_leave_shared_asr_close_to_composition(tmp_path: Path) -> None:
    class RecordingAsr:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self, *, deadline=None) -> None:
            del deadline
            self.close_calls += 1

    asr = RecordingAsr()
    application = ImportedMediaTranscriptionService(
        FakeMediaNormalizer(),
        asr,
        FakeSnapshotStore(tmp_path / "work"),
    )

    application.close(close_asr=False)

    assert asr.close_calls == 0


def test_close_keeps_store_worker_owned_until_it_finishes(tmp_path: Path) -> None:
    close_started = Event()
    release_close = Event()

    class BlockingCloseStore(FakeSnapshotStore):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.close_calls = 0

        def close(self, *, timeout=None) -> None:
            del timeout
            self.close_calls += 1
            close_started.set()
            release_close.wait(2.0)

    class RecordingAsr:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self, *, deadline=None) -> None:
            del deadline
            self.close_calls += 1

    store = BlockingCloseStore(tmp_path / "work")
    asr = RecordingAsr()
    application = ImportedMediaTranscriptionService(FakeMediaNormalizer(), asr, store)

    with pytest.raises(ImportRecoveryPendingError):
        application.close(timeout=0.01)
    assert close_started.is_set()
    assert asr.close_calls == 0
    with application._lock:
        store_threads = tuple(
            thread for thread in application._cleanup_threads if thread.name == "import-store-close"
        )
    assert len(store_threads) == 1
    assert store_threads[0].is_alive()

    with pytest.raises(ImportRecoveryPendingError):
        application.close(timeout=0.01)
    assert store.close_calls == 1
    assert asr.close_calls == 0
    with application._lock:
        assert store_threads[0] in application._cleanup_threads

    release_close.set()
    application.close(timeout=1.0)

    assert store.close_calls == 1
    assert asr.close_calls == 1


def test_snapshot_release_retries_without_evicting_the_terminal_record(tmp_path: Path) -> None:
    class FailsSnapshotReleaseTwice(FakeSnapshotStore):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.release_attempts = 0

        def release_snapshot(self, snapshot) -> None:
            self.release_attempts += 1
            if self.release_attempts <= 2:
                raise OSError("simulated snapshot lease failure")
            super().release_snapshot(snapshot)

    store = FailsSnapshotReleaseTwice(tmp_path / "work")
    application = ImportedMediaTranscriptionService(
        FakeMediaNormalizer(),
        AsrApplicationService(FakeAsrRuntime()),
        store,
        max_completed_records=1,
    )
    job_id = application.submit(str(source_file(tmp_path)))
    result = application.wait(job_id, timeout=2.0)
    record = application._record(job_id)

    assert result.status == "failed"
    assert record.snapshot is not None
    assert not application._can_evict_completed(record)
    assert job_id in application._snapshot_recovery

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and record.snapshot is not None:
        time.sleep(0.01)
    application.close(timeout=2.0)

    assert store.release_attempts >= 3
    assert record.snapshot is None
    assert job_id not in application._snapshot_recovery


def test_store_close_retries_after_a_transient_failure(tmp_path: Path) -> None:
    class FailsStoreCloseOnce(FakeSnapshotStore):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.close_attempts = 0

        def close(self, *, timeout=None) -> None:
            del timeout
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise OSError("transient store close failure")

    store = FailsStoreCloseOnce(tmp_path / "work")
    application = ImportedMediaTranscriptionService(
        FakeMediaNormalizer(), AsrApplicationService(FakeAsrRuntime()), store
    )

    with pytest.raises(ImportShutdownError):
        application.close(timeout=1.0)
    assert store.close_attempts == 1

    application.close(timeout=1.0)

    assert store.close_attempts == 2
    assert application._store_close_error is None


def test_cleanup_completion_at_deadline_publishes_cleanup_warning(tmp_path: Path) -> None:
    from voiceink_win.infrastructure import FakeClock

    clock = FakeClock()

    class CompletesAtDeadlineStore(FakeSnapshotStore):
        def cleanup(self, workspace, **kwargs):
            del kwargs
            clock.advance(1.0)
            if workspace.path.exists():
                super().cleanup(workspace)

    store = CompletesAtDeadlineStore(tmp_path / "work")
    application = ImportedMediaTranscriptionService(
        FakeMediaNormalizer(),
        AsrApplicationService(FakeAsrRuntime(), clock=clock),
        store,
        clock=clock,
        cleanup_timeout_seconds=1.0,
    )
    result = application.submit_and_wait(str(source_file(tmp_path)), timeout=2.0)

    assert result.code is ErrorCode.CLEANUP_WARNING
    assert result.stage is Stage.CLEANUP
    application.close(timeout=2.0)


def test_invalid_media_fails_without_asr_invocation(tmp_path: Path) -> None:
    normalizer = FakeMediaNormalizer(FakeMediaScenario.NO_AUDIO)
    runtime = FakeAsrRuntime()
    application = service(tmp_path, normalizer, runtime)

    result = application.submit_and_wait(str(source_file(tmp_path)), timeout=2.0)
    application.close()

    assert result.status == "failed"
    assert result.code is ErrorCode.NO_AUDIO_STREAM
    assert normalizer.calls == 1


def test_queue_full_rejects_before_workspace_creation(tmp_path: Path) -> None:
    queue = ImportQueue(capacity=1)
    owner = JobId("held")
    token = queue.reserve(owner)
    with pytest.raises(QueueFullRejectedError):
        queue.reserve(JobId("rejected"))
    assert token.state is ReservationState.HELD
    assert queue.reservation_count == 1
    assert token.release(owner)
    assert queue.reservation_count == 0


def test_pre_admission_resource_rejection_is_not_processing_failure() -> None:
    assert issubclass(ResourceLimitRejectedError, RejectedRequestError)
    assert not issubclass(ResourceLimitRejectedError, ResourceLimitExceededError)


def test_queued_cancellation_cleans_without_normalizer_or_asr(tmp_path: Path) -> None:
    started = Event()
    release = Event()

    class BlockingNormalizer(FakeMediaNormalizer):
        def normalize(self, source, workspace, cancellation, deadline):
            started.set()
            release.wait(2.0)
            return super().normalize(source, workspace, cancellation, deadline)

    normalizer = BlockingNormalizer()
    store = FakeSnapshotStore(tmp_path / "work")
    runtime = FakeAsrRuntime()
    asr = AsrApplicationService(runtime)
    application = ImportedMediaTranscriptionService(
        normalizer, asr, store, queue_capacity=2, retry_backoff=lambda _: 0
    )
    application.submit(str(source_file(tmp_path, b"first")))
    assert started.wait(1.0)
    second = application.submit(str(source_file(tmp_path, b"second")))

    assert application.cancel(second)
    release.set()
    result = application.wait(second, timeout=2.0)
    application.close()

    assert result.status == "cancelled"
    assert normalizer.calls == 1
    assert store.cleanup_calls >= 2


def test_transient_runtime_error_retries_after_workspace_cleanup(tmp_path: Path) -> None:
    runtime = FakeAsrRuntime(FakeAsrScenario.UNAVAILABLE)
    store = FakeSnapshotStore(tmp_path / "work")
    asr = AsrApplicationService(runtime)
    application = ImportedMediaTranscriptionService(
        FakeMediaNormalizer(), asr, store, retry_backoff=lambda _: 0
    )

    result = application.submit_and_wait(str(source_file(tmp_path)), timeout=3.0)
    application.close()

    assert result.status == "failed"
    assert result.code is ErrorCode.RUNTIME_UNAVAILABLE
    assert result.attempt.value == 2
    assert store.workspace_creations == 2
    assert store.cleanup_calls == 2


def test_process_crash_retries_on_a_second_attempt(tmp_path: Path) -> None:
    class CrashOnceRuntime(FakeAsrRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def transcribe(self, request):
            self.calls += 1
            if self.calls == 1:
                raise ProcessCrashedError("simulated process crash")
            return super().transcribe(request)

    runtime = CrashOnceRuntime()
    store = FakeSnapshotStore(tmp_path / "work")
    application = ImportedMediaTranscriptionService(
        FakeMediaNormalizer(), AsrApplicationService(runtime), store, retry_backoff=lambda _: 0
    )

    result = application.submit_and_wait(str(source_file(tmp_path)), timeout=3.0)
    application.close()

    assert result.status == "succeeded"
    assert result.attempt.value == 2
    assert runtime.calls == 2
    assert store.workspace_creations == 2
    assert store.cleanup_calls == 2


def test_retry_waiting_cancellation_enqueues_cleanup(tmp_path: Path) -> None:
    runtime = FakeAsrRuntime(FakeAsrScenario.UNAVAILABLE)
    store = FakeSnapshotStore(tmp_path / "work")
    application = ImportedMediaTranscriptionService(
        FakeMediaNormalizer(), AsrApplicationService(runtime), store, retry_backoff=lambda _: 10.0
    )
    job_id = application.submit(str(source_file(tmp_path)))

    deadline = time.monotonic() + 2.0
    while (
        time.monotonic() < deadline
        and application._record(job_id).job.stage is not Stage.RETRY_WAITING
    ):
        time.sleep(0.01)
    assert application._record(job_id).job.stage is Stage.RETRY_WAITING
    assert application.cancel(job_id)

    result = application.wait(job_id, timeout=2.0)
    application.close()

    assert result.status == "cancelled"
    # Cancellation may race with retry workspace creation, but never cleans one
    # workspace more than once.
    assert len(store.cleanup_paths) == len(set(store.cleanup_paths))


def test_retry_waiting_deadline_wakes_cleanup_without_backoff_delay(tmp_path: Path) -> None:
    store = FakeSnapshotStore(tmp_path / "work")
    application = ImportedMediaTranscriptionService(
        FakeMediaNormalizer(),
        AsrApplicationService(FakeAsrRuntime(FakeAsrScenario.UNAVAILABLE)),
        store,
        processing_deadline_seconds=0.1,
        retry_backoff=lambda _: 10.0,
    )
    started = time.monotonic()
    job_id = application.submit(str(source_file(tmp_path)))

    result = application.wait(job_id, timeout=2.0)
    elapsed = time.monotonic() - started
    application.close()

    assert result.status == "failed"
    assert result.code is ErrorCode.DEADLINE_EXCEEDED
    assert result.attempt.value == 2
    assert elapsed < 2.0
    assert store.cleanup_calls == 2


def test_close_terminalizes_retry_waiting_jobs_before_releasing_reservation(
    tmp_path: Path,
) -> None:
    store = FakeSnapshotStore(tmp_path / "work")
    application = ImportedMediaTranscriptionService(
        FakeMediaNormalizer(),
        AsrApplicationService(FakeAsrRuntime(FakeAsrScenario.UNAVAILABLE)),
        store,
        retry_backoff=lambda _: 30.0,
    )
    job_id = application.submit(str(source_file(tmp_path)))
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and (
        application._record(job_id).job.stage is not Stage.RETRY_WAITING
        or application._record(job_id).job.attempt.value != 2
    ):
        time.sleep(0.01)
    assert application._record(job_id).job.stage is Stage.RETRY_WAITING
    assert application._record(job_id).job.attempt.value == 2

    application.close(timeout=2.0)
    result = application._record(job_id).job.result

    assert result.status == "cancelled"
    assert application._queue.reservation_count == 0
    assert store.cleanup_calls == 2


def test_running_cancellation_never_publishes_partial_work(tmp_path: Path) -> None:
    started = Event()
    release = Event()

    class BlockingNormalizer(FakeMediaNormalizer):
        def normalize(self, source, workspace, cancellation, deadline):
            started.set()
            release.wait(2.0)
            try:
                return super().normalize(source, workspace, cancellation, deadline)
            finally:
                self.stage_owner_done.set()

    normalizer = BlockingNormalizer()
    runtime = FakeAsrRuntime()
    application = service(tmp_path, normalizer, runtime)
    job_id = application.submit(str(source_file(tmp_path)))
    assert started.wait(1.0)

    assert application.cancel(job_id)
    release.set()
    result = application.wait(job_id, timeout=2.0)
    application.close()

    assert result.status == "cancelled"
    assert normalizer.calls == 1


def test_processing_deadline_enters_cleanup_and_fails_without_retry(tmp_path: Path) -> None:
    started = Event()
    release = Event()

    class BlockingNormalizer(FakeMediaNormalizer):
        def normalize(self, source, workspace, cancellation, deadline):
            started.set()
            release.wait(2.0)
            return super().normalize(source, workspace, cancellation, deadline)

    application = service(
        tmp_path,
        BlockingNormalizer(),
        FakeAsrRuntime(),
        processing_deadline_seconds=0.2,
    )
    job_id = application.submit(str(source_file(tmp_path)))
    assert started.wait(2.0)
    time.sleep(0.25)
    application.check_deadlines()
    release.set()

    result = application.wait(job_id, timeout=2.0)
    application.close()

    assert result.status == "failed"
    assert result.code is ErrorCode.DEADLINE_EXCEEDED
    assert result.stage is Stage.NORMALIZATION
    assert result.attempt.value == 1


def test_non_cooperative_stage_deadline_publishes_before_owner_done(tmp_path: Path) -> None:
    started = Event()
    release = Event()

    class NonCooperativeNormalizer(FakeMediaNormalizer):
        stage_owner_done = Event()

        def normalize(self, source, workspace, cancellation, deadline):
            started.set()
            release.wait(2.0)
            try:
                return super().normalize(source, workspace, cancellation, deadline)
            finally:
                self.stage_owner_done.set()

    application = service(
        tmp_path,
        NonCooperativeNormalizer(),
        FakeAsrRuntime(),
        processing_deadline_seconds=0.2,
    )
    job_id = application.submit(str(source_file(tmp_path)))
    assert started.wait(2.0)
    record = application._record(job_id)
    time.sleep(0.25)
    application.check_deadlines()
    time.sleep(1.05)
    application.check_deadlines()

    result = application.wait(job_id, timeout=0.2)

    assert result.code is ErrorCode.CLEANUP_WARNING
    assert application._queue.reservation_count == 0
    assert record.snapshot is not None
    assert record.workspace is not None
    assert not record.stage_owner_done.is_set()

    release.set()
    application.close(timeout=2.0)
    assert record.snapshot is None
    assert record.workspace is None


def test_completed_record_eviction_waits_for_fenced_leases_during_churn(
    tmp_path: Path,
) -> None:
    owner_done = Event()
    first_started = Event()
    release_first = Event()

    class ChurningNormalizer(FakeMediaNormalizer):
        def __init__(self) -> None:
            super().__init__()
            self.owner_events = [owner_done, Event(), Event()]

        def stage_owner_done(self):
            return self.owner_events.pop(0)

        def normalize(self, source, workspace, cancellation, deadline):
            if self.calls == 0:
                first_started.set()
                release_first.wait(2.0)
            else:
                self.owner_events[0].set()
            result = super().normalize(source, workspace, cancellation, deadline)
            return result

    application = service(
        tmp_path,
        ChurningNormalizer(),
        FakeAsrRuntime(),
        max_completed_records=1,
        processing_deadline_seconds=5.0,
    )
    first_id = application.submit(str(source_file(tmp_path, b"first")))
    assert first_started.wait(1.0)
    first_record = application._record(first_id)
    first_record.job.deadline = time.monotonic() - 1.0
    time.sleep(0.03)
    application.check_deadlines()
    time.sleep(1.05)
    application.check_deadlines()

    first_result = application.wait(first_id, timeout=1.0)
    assert first_result.code is ErrorCode.CLEANUP_WARNING
    assert first_record.workspace is not None
    assert not first_record.stage_owner_done.is_set()

    for content in (b"second", b"third"):
        job_id = application.submit(str(source_file(tmp_path, content)))
        assert application.wait(job_id, timeout=2.0).status == "succeeded"

    assert application._record(first_id) is first_record

    owner_done.set()
    cleanup_deadline = time.monotonic() + 2.0
    while time.monotonic() < cleanup_deadline and first_record.workspace is not None:
        time.sleep(0.01)
    application.close(timeout=2.0)

    assert first_record.stage_owner_done.is_set()
    assert first_record.snapshot is None
    assert first_record.workspace is None
    assert first_record.source_released
    assert application._record(first_id) is first_record


def test_completed_record_is_pruned_after_fenced_cleanup_finishes(tmp_path: Path) -> None:
    cleanup_started = Event()
    release_cleanup = Event()

    class CleanupBlocksOnceStore(FakeSnapshotStore):
        def cleanup(self, workspace, **kwargs):
            self.cleanup_calls += 1
            if self.cleanup_calls == 1:
                cleanup_started.set()
                release_cleanup.wait(2.0)
            return super().cleanup(workspace, **kwargs)

    store = CleanupBlocksOnceStore(tmp_path / "work")
    application = ImportedMediaTranscriptionService(
        FakeMediaNormalizer(),
        AsrApplicationService(FakeAsrRuntime()),
        store,
        cleanup_timeout_seconds=0.01,
        max_completed_records=1,
    )
    first_id = application.submit(str(source_file(tmp_path, b"first")))
    assert cleanup_started.wait(1.0)
    first_record = application._record(first_id)
    first_record.cleanup_entered_at = time.monotonic() - 1.0
    application.check_deadlines()

    assert application.wait(first_id, timeout=1.0).code is ErrorCode.CLEANUP_WARNING
    assert first_record.workspace is not None

    for content in (b"second", b"third"):
        job_id = application.submit(str(source_file(tmp_path, content)))
        assert application.wait(job_id, timeout=5.0).status == "succeeded"
    assert application._record(first_id) is first_record

    release_cleanup.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and first_record.workspace is not None:
        time.sleep(0.01)
    assert first_record.workspace is None
    fourth_id = application.submit(str(source_file(tmp_path, b"fourth")))
    assert application.wait(fourth_id, timeout=5.0).status == "succeeded"

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and first_id in application._records:
        time.sleep(0.01)
    application.close(timeout=2.0)

    assert first_record.workspace is None
    assert first_record.snapshot is None
    assert first_record.source_released
    assert first_id not in application._records


def test_deadline_cleanup_failure_publishes_terminal_warning_and_releases_capacity(
    tmp_path: Path,
) -> None:
    started = Event()
    release = Event()

    class BlockingNormalizer(FakeMediaNormalizer):
        def normalize(self, source, workspace, cancellation, deadline):
            started.set()
            release.wait(2.0)
            return super().normalize(source, workspace, cancellation, deadline)

    class CleanupFailsOnceStore(FakeSnapshotStore):
        def cleanup(self, workspace):
            self.cleanup_calls += 1
            if self.cleanup_calls == 1:
                raise OSError("simulated cleanup failure")
            super().cleanup(workspace)

    store = CleanupFailsOnceStore(tmp_path / "work")
    application = ImportedMediaTranscriptionService(
        BlockingNormalizer(),
        AsrApplicationService(FakeAsrRuntime()),
        store,
        processing_deadline_seconds=0.2,
    )
    job_id = application.submit(str(source_file(tmp_path)))
    assert started.wait(2.0)
    time.sleep(0.25)
    application.check_deadlines()
    release.set()

    result = application.wait(job_id, timeout=2.0)
    application.close()

    assert result.status == "failed"
    assert result.code is ErrorCode.DEADLINE_EXCEEDED
    assert result.stage is Stage.NORMALIZATION
    assert result.warnings == (WarningCode.CLEANUP_WARNING,)
    assert application._queue.reservation_count == 0


def test_cleanup_deadline_fences_terminal_warning_and_releases_each_owner_once(
    tmp_path: Path,
) -> None:
    cleanup_started = Event()
    release_cleanup = Event()

    class BlockingCleanupStore(FakeSnapshotStore):
        def cleanup(self, workspace, **kwargs):
            cleanup_started.set()
            release_cleanup.wait(2.0)
            return super().cleanup(workspace, **kwargs)

    store = BlockingCleanupStore(tmp_path / "work")
    application = ImportedMediaTranscriptionService(
        FakeMediaNormalizer(),
        AsrApplicationService(FakeAsrRuntime()),
        store,
        cleanup_timeout_seconds=0.01,
    )
    job_id = application.submit(str(source_file(tmp_path)))
    assert cleanup_started.wait(1.0)
    record = application._record(job_id)
    record.cleanup_entered_at = time.monotonic() - 1.0
    application.check_deadlines()

    result = application.wait(job_id, timeout=1.0)
    assert result.code is ErrorCode.CLEANUP_WARNING
    assert record.cleanup_done is not None and record.cleanup_done.is_set()
    assert application._queue.reservation_count == 0
    assert store.source_release_calls == 1

    release_cleanup.set()
    application.close(timeout=2.0)
    assert store.source_release_calls == 1


def test_stage_reaper_fence_holds_snapshot_and_workspace_until_owner_done(tmp_path: Path) -> None:
    owner_done = Event()

    class ReaperOwnedNormalizer(FakeMediaNormalizer):
        stage_owner_done = owner_done

        def normalize(self, source, workspace, cancellation, deadline):
            del source, workspace, cancellation, deadline
            raise RuntimeError("FFmpeg cleanup is pending")

    store = FakeSnapshotStore(tmp_path / "work")
    application = ImportedMediaTranscriptionService(
        ReaperOwnedNormalizer(), AsrApplicationService(FakeAsrRuntime()), store
    )
    job_id = application.submit(str(source_file(tmp_path)))

    with pytest.raises(TimeoutError):
        application.wait(job_id, timeout=0.1)
    record = application._record(job_id)
    assert record.snapshot is not None
    assert record.workspace is not None
    assert store.cleanup_calls == 0

    owner_done.set()
    result = application.wait(job_id, timeout=2.0)
    application.close()

    assert result.status == "failed"
    assert store.cleanup_calls == 1


def test_force_terminal_failure_defers_owned_resources_until_stage_owner_done(
    tmp_path: Path,
) -> None:
    owner_done = Event()

    class FailingNormalizer(FakeMediaNormalizer):
        stage_owner_done = owner_done

        def normalize(self, source, workspace, cancellation, deadline):
            del source, workspace, cancellation, deadline
            raise RuntimeError("normalizer worker failed")

    store = FakeSnapshotStore(tmp_path / "work")
    application = ImportedMediaTranscriptionService(
        FailingNormalizer(), AsrApplicationService(FakeAsrRuntime()), store
    )
    job_id = application.submit(str(source_file(tmp_path)))
    record = application._record(job_id)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and record.failure is None:
        time.sleep(0.01)

    application._force_terminal_failure(record, RuntimeError("finalizer failed"))
    result = application.wait(job_id, timeout=1.0)

    assert result.code is ErrorCode.CLEANUP_WARNING
    assert application._queue.reservation_count == 0
    assert record.snapshot is not None
    assert record.workspace is not None
    assert store.cleanup_calls == 0

    owner_done.set()
    application.close(timeout=2.0)
    assert record.snapshot is None
    assert record.workspace is None
    assert store.cleanup_calls == 1


def test_processing_deadline_interrupts_active_adapter_before_cleanup(tmp_path: Path) -> None:
    started = Event()
    interrupted = Event()
    release = Event()

    class InterruptibleNormalizer(FakeMediaNormalizer):
        def normalize(self, source, workspace, cancellation, deadline):
            started.set()
            release.wait(2.0)
            return super().normalize(source, workspace, cancellation, deadline)

        def interrupt(self):
            interrupted.set()
            release.set()

    application = service(
        tmp_path,
        InterruptibleNormalizer(),
        FakeAsrRuntime(),
        processing_deadline_seconds=0.2,
    )
    job_id = application.submit(str(source_file(tmp_path)))
    assert started.wait(2.0)
    time.sleep(0.25)
    application.check_deadlines()

    result = application.wait(job_id, timeout=2.0)
    application.close()

    assert interrupted.is_set()
    assert result.code is ErrorCode.DEADLINE_EXCEEDED
    assert application._queue.reservation_count == 0


def test_late_asr_result_is_fenced_after_cancellation(tmp_path: Path) -> None:
    started = Event()
    release = Event()

    class LateRuntime(FakeAsrRuntime):
        def transcribe(self, request):
            started.set()
            release.wait(2.0)
            return super().transcribe(request)

    runtime = LateRuntime()
    application = service(tmp_path, FakeMediaNormalizer(), runtime)
    job_id = application.submit(str(source_file(tmp_path)))
    assert started.wait(1.0)
    assert application.cancel(job_id)
    release.set()

    result = application.wait(job_id, timeout=2.0)
    application.close()

    assert result.status == "cancelled"
    record = application._record(job_id)
    assert record.normalized is None
    assert record.transcript is None


def test_late_normalizer_result_is_not_retained_after_cancellation(tmp_path: Path) -> None:
    started = Event()
    release = Event()

    class LateNormalizer(FakeMediaNormalizer):
        def normalize(self, source, workspace, cancellation, deadline):
            started.set()
            release.wait(2.0)
            return super().normalize(source, workspace, cancellation, deadline)

    application = service(tmp_path, LateNormalizer(), FakeAsrRuntime())
    job_id = application.submit(str(source_file(tmp_path)))
    assert started.wait(1.0)
    assert application.cancel(job_id)
    release.set()

    result = application.wait(job_id, timeout=2.0)
    application.close()

    assert result.status == "cancelled"
    record = application._record(job_id)
    assert record.normalized is None
    assert record.transcript is None


def test_source_mutation_is_rejected_before_normalization(tmp_path: Path) -> None:
    class MutatingStore(FakeSnapshotStore):
        def snapshot(self, source, workspace, **kwargs):
            snapshot = super().snapshot(source, workspace, **kwargs)
            source.path.write_bytes(b"changed")
            return snapshot

    normalizer = FakeMediaNormalizer()
    runtime = FakeAsrRuntime()
    asr = AsrApplicationService(runtime)
    store = MutatingStore(tmp_path / "work")
    application = ImportedMediaTranscriptionService(normalizer, asr, store)

    result = application.submit_and_wait(str(source_file(tmp_path)), timeout=2.0)
    application.close()

    assert result.code is ErrorCode.SOURCE_CHANGED
    assert normalizer.calls == 0


def test_cleanup_failure_overrides_success(tmp_path: Path) -> None:
    normalizer = FakeMediaNormalizer()
    runtime = FakeAsrRuntime()
    asr = AsrApplicationService(runtime)
    application = ImportedMediaTranscriptionService(
        normalizer, asr, FakeSnapshotStore(tmp_path / "work", cleanup_error=True)
    )

    job_id = application.submit(str(source_file(tmp_path)))
    with pytest.raises(TimeoutError):
        application.wait(job_id, timeout=0.1)
    application._store.cleanup_error = False
    result = application.wait(job_id, timeout=2.0)
    application.close()

    assert result.status == "failed"
    assert result.code is ErrorCode.CLEANUP_WARNING
    assert result.stage is Stage.CLEANUP


def test_source_release_retries_after_close_failure_and_marks_only_success(
    tmp_path: Path,
) -> None:
    class FailsOnceStore(FakeSnapshotStore):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.release_attempts = 0

        def release_source(self, source) -> None:
            self.release_attempts += 1
            if self.release_attempts == 1:
                raise OSError("simulated CloseHandle failure")
            super().release_source(source)

    store = FailsOnceStore(tmp_path / "work")
    application = ImportedMediaTranscriptionService(
        FakeMediaNormalizer(), AsrApplicationService(FakeAsrRuntime()), store
    )

    job_id = application.submit(str(source_file(tmp_path)))
    result = application.wait(job_id, timeout=2.0)
    record = application._record(job_id)
    application.close()

    assert result.status == "failed"
    assert record.source_released
    assert store.release_attempts == 2


def test_workspace_reconciliation_keeps_source_recovery_separate(tmp_path: Path) -> None:
    class FailsWorkspaceCleanupOnceStore(FakeSnapshotStore):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.cleanup_attempts = 0
            self.release_attempts = 0

        def cleanup(self, workspace, **kwargs):
            self.cleanup_attempts += 1
            if self.cleanup_attempts == 1:
                raise OSError("workspace cleanup failed")
            return super().cleanup(workspace, **kwargs)

        def release_source(self, source) -> None:
            self.release_attempts += 1
            raise OSError("source CloseHandle failed")

    store = FailsWorkspaceCleanupOnceStore(tmp_path / "work")
    application = ImportedMediaTranscriptionService(
        FakeMediaNormalizer(), AsrApplicationService(FakeAsrRuntime()), store
    )
    job_id = application.submit(str(source_file(tmp_path)))

    result = application.wait(job_id, timeout=2.0)
    record = application._record(job_id)

    assert result.status == "failed"
    assert record.workspace is None
    assert application._recovery_workspaces == set()
    assert job_id in application._source_recovery

    with pytest.raises(ImportRecoveryPendingError):
        application.close(timeout=0.05)

    assert job_id in application._source_recovery


def test_recovery_cleanup_runs_once_while_reconciliation_is_pending(tmp_path: Path) -> None:
    first_cleanup_started = Event()
    release_first_cleanup = Event()
    second_recovery_cleanup_started = Event()

    class FailsWorkspaceCleanupOnceStore(FakeSnapshotStore):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.cleanup_attempts = 0

        def cleanup(self, workspace, **kwargs):
            del kwargs
            self.cleanup_attempts += 1
            if self.cleanup_attempts == 1:
                first_cleanup_started.set()
                release_first_cleanup.wait(2.0)
                raise OSError("workspace cleanup failed")
            if self.cleanup_attempts == 3:
                second_recovery_cleanup_started.set()
            return super().cleanup(workspace)

    class BlocksReconciliation(ImportedMediaTranscriptionService):
        def __init__(self, *args, **kwargs) -> None:
            self.reconciliation_started = Event()
            self.release_reconciliation = Event()
            super().__init__(*args, **kwargs)

        def _retry_recovery_workspaces(self, *, deadline=None) -> None:
            del deadline

        def _reconcile_successful_cleanup(self, records, workspace) -> None:
            self.reconciliation_started.set()
            self.release_reconciliation.wait(2.0)
            super()._reconcile_successful_cleanup(records, workspace)

    store = FailsWorkspaceCleanupOnceStore(tmp_path / "work")
    application = BlocksReconciliation(
        FakeMediaNormalizer(), AsrApplicationService(FakeAsrRuntime()), store
    )
    job_id = application.submit(str(source_file(tmp_path)))
    assert first_cleanup_started.wait(1.0)
    release_first_cleanup.set()

    record = application._record(job_id)
    assert record.cleanup_done is not None
    assert record.cleanup_done.wait(1.0)
    application._stopping.set()
    application._monitor.join(1.0)

    first_recovery = Thread(target=application._retry_recovery_workspaces_impl)
    first_recovery.start()
    assert application.reconciliation_started.wait(1.0)

    second_recovery = Thread(target=application._retry_recovery_workspaces_impl)
    second_recovery.start()
    assert not second_recovery_cleanup_started.wait(0.1)

    application.release_reconciliation.set()
    first_recovery.join(1.0)
    second_recovery.join(1.0)
    application.close(timeout=2.0)

    assert store.cleanup_attempts == 2
    assert record.workspace is None
    assert application._recovery_workspaces == set()
    assert application._cleanup_workspaces_inflight == set()
    assert application._reconciliation_workspaces_inflight == set()


def test_windows_handle_lease_retains_ownership_after_two_close_failures() -> None:
    class Dll:
        def __init__(self) -> None:
            self.failures = 2
            self.calls: list[int] = []

        def CloseHandle(self, handle: int) -> bool:
            self.calls.append(handle)
            if self.failures:
                self.failures -= 1
                return False
            return True

    dll = Dll()
    lease = _HandleLease(SimpleNamespace(dll=dll), 17)

    with pytest.raises(OSError):
        lease.close()
    with pytest.raises(OSError):
        lease.close()
    lease.close()

    assert dll.calls == [17, 17, 17]
    assert lease._closed


def test_windows_handle_lease_rejects_double_wrapping_and_allows_reuse_after_close() -> None:
    class Dll:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def CloseHandle(self, handle: int) -> bool:
            self.calls.append(handle)
            return True

    store = object.__new__(WindowsMediaSnapshotStore)
    store._api = SimpleNamespace(dll=Dll())
    first = _HandleLease(store._api, 17)
    second = _HandleLease(store._api, 17)

    store._register_lease(first)
    with pytest.raises(RuntimeError, match="different lease"):
        store._register_lease(second)

    store._release_lease(first)
    store._register_lease(second)
    store._release_lease(second)

    assert store._api.dll.calls == [17, 17]


def test_windows_source_release_recovery_retries_two_close_failures() -> None:
    class Dll:
        def __init__(self) -> None:
            self.failures = 2
            self.calls = 0

        def CloseHandle(self, handle: int) -> bool:
            del handle
            self.calls += 1
            if self.failures:
                self.failures -= 1
                return False
            return True

    dll = Dll()
    store = object.__new__(WindowsMediaSnapshotStore)
    store._api = SimpleNamespace(dll=dll)
    lease = _HandleLease(store._api, 17)
    source = SourceMedia(
        Path("C:/imports/input.wav"),
        identity="source",
        size=6,
        sha256=hashlib.sha256(b"source").hexdigest(),
        admission_handle=lease,
    )

    with pytest.raises(OSError):
        store.release_source(source)

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not lease._closed:
        time.sleep(0.01)
    assert lease._closed
    assert dll.calls >= 3


def test_windows_lease_reaper_never_closes_an_active_lease() -> None:
    class Dll:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def CloseHandle(self, handle: int) -> bool:
            self.calls.append(handle)
            return True

    dll = Dll()
    store = object.__new__(WindowsMediaSnapshotStore)
    store._api = SimpleNamespace(dll=dll)
    lease = _HandleLease(store._api, 19)

    store._register_lease(lease)
    time.sleep(0.05)

    assert dll.calls == []
    assert lease in store._active_leases
    assert lease not in store._failed_close_leases

    store._release_lease(lease)
    assert dll.calls == [19]


def test_windows_snapshot_release_attempts_all_handles_and_retries_failed_leases() -> None:
    class Dll:
        def __init__(self) -> None:
            self.failures = {11: 1, 13: 1}
            self.calls: list[int] = []

        def CloseHandle(self, handle: int) -> bool:
            self.calls.append(handle)
            if self.failures.get(handle, 0):
                self.failures[handle] -= 1
                return False
            return True

    dll = Dll()
    store = object.__new__(WindowsMediaSnapshotStore)
    store._api = SimpleNamespace(dll=dll)
    source = SourceMedia(
        Path("C:/imports/input.wav"),
        identity="source",
        size=6,
        sha256=hashlib.sha256(b"source").hexdigest(),
    )
    snapshot = SourceSnapshot(
        source,
        Path("C:/private/job/attempt-1/source.snapshot"),
        SnapshotManifest("snapshot", 6, hashlib.sha256(b"source").hexdigest()),
        source.identity,
        source.size,
        source.sha256,
        VerifiedMediaHandle(
            "C:/private/job/attempt-1/source.snapshot",
            11,
            "snapshot",
            6,
            source.sha256,
            _HandleLease(store._api, 11),
        ),
        11,
        (_HandleLease(store._api, 12), _HandleLease(store._api, 13)),
    )

    with pytest.raises(ExceptionGroup):
        store.release_snapshot(snapshot)

    assert set(dll.calls) == {11, 12, 13}
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and getattr(store, "_lease_recovery", ()):
        time.sleep(0.01)
    assert not store._lease_recovery


def test_windows_lease_reaper_rearms_after_a_shutdown_deadline() -> None:
    class Dll:
        def __init__(self) -> None:
            self.allow_close = Event()
            self.calls: list[int] = []

        def CloseHandle(self, handle: int) -> bool:
            self.calls.append(handle)
            return self.allow_close.is_set()

    dll = Dll()
    store = object.__new__(WindowsMediaSnapshotStore)
    store._api = SimpleNamespace(dll=dll)
    lease = _HandleLease(store._api, 23)
    store._register_lease(lease)

    with pytest.raises(OSError):
        store._release_lease(lease)
    with pytest.raises(TimeoutError):
        store.close(timeout=0.01)
    assert lease in store._failed_close_leases

    dll.allow_close.set()
    store.close(timeout=1.0)

    assert lease._closed
    assert not store._failed_close_leases


@pytest.mark.parametrize(
    "data",
    [b"", b"RIFF\x00\x00\x00\x00WAVE", b"RIFF" + b"\xff" * 4 + b"WAVE"],
)
def test_validate_wav_rejects_malformed_output(data: bytes) -> None:
    with pytest.raises(MalformedWavError):
        validate_wav(data)


def test_validate_wav_enforces_pcm_and_sample_boundaries() -> None:
    wav = make_wav(b"\x00\x00" * 4)
    assert validate_wav(wav, WavLimits(pcm_bytes=8, sample_count=4)).sample_count == 4
    with pytest.raises(ResourceLimitExceededError) as error:
        validate_wav(wav, WavLimits(pcm_bytes=7, sample_count=4))
    assert error.value.code is ErrorCode.RESOURCE_LIMIT_EXCEEDED


def test_bounded_sink_streams_wav_and_owns_one_final_pcm_value() -> None:
    wav = make_wav(b"\x00\x00" * 4)
    sink = BoundedPcmSink(max_bytes=64 * 1024, max_samples=4, max_pcm_bytes=8)
    for offset in range(0, len(wav), 3):
        sink.write(wav[offset : offset + 3])

    normalized = sink.normalized_audio(WavLimits(pcm_bytes=8, sample_count=4))
    assert normalized.sample_count == 4
    assert len(sink.bytes()) == 0


def test_wav_limits_use_the_canonical_32_minute_duration() -> None:
    limits = WavLimits()
    assert limits.pcm_bytes == 64 * 1024 * 1024
    assert limits.sample_count == 32 * 60 * 16_000
    assert limits.sample_count * 2 < limits.pcm_bytes


@POSIX_ONLY
def test_source_and_workspace_quotas_are_independent(tmp_path: Path) -> None:
    store = LocalMediaSnapshotStore(
        tmp_path / "private", max_workspace_bytes=600, max_snapshot_bytes=20
    )
    first = store.create_workspace(JobId("first"), 1)
    second = store.create_workspace(JobId("second"), 1)

    store._reserve_snapshot(first.path, 20)
    store._reserve_snapshot(second.path, 20)
    store._reserve_workspace(first.path, 400)
    store._reserve_workspace(second.path, 400)
    with pytest.raises(ResourceLimitExceededError):
        store._reserve_snapshot(first.path, 1)
    with pytest.raises(ResourceLimitExceededError):
        store._reserve_workspace(second.path, 100)


@POSIX_ONLY
def test_workspace_usage_counts_dot_prefixed_job_ids(tmp_path: Path) -> None:
    store = LocalMediaSnapshotStore(tmp_path / "private")
    workspace = store.create_workspace(JobId(".hidden-job"), 1)
    (workspace.path / "payload").write_bytes(b"payload")

    recovered = LocalMediaSnapshotStore(store.root)

    assert workspace.path in recovered._workspace_reserved
    assert recovered._workspace_reserved[workspace.path] == recovered._directory_size(
        workspace.path, exclude_snapshot=True
    )


@POSIX_ONLY
def test_cleanup_unlinks_external_symlink_without_following_target(tmp_path: Path) -> None:
    store = LocalMediaSnapshotStore(tmp_path / "private")
    job_id = JobId("safe")
    workspace = store.create_workspace(job_id, 1)
    target = tmp_path / "outside"
    target.mkdir()
    (target / "secret").write_text("keep", encoding="ascii")
    (workspace.path / "link").symlink_to(target, target_is_directory=True)

    store.cleanup(workspace)

    assert target.joinpath("secret").read_text(encoding="ascii") == "keep"
    assert not workspace.path.exists()


@POSIX_ONLY
def test_cleanup_unlinks_special_entry_without_opening_or_blocking(tmp_path: Path) -> None:
    store = LocalMediaSnapshotStore(tmp_path / "private")
    workspace = store.create_workspace(JobId("special"), 1)
    fifo = workspace.path / "blocked.fifo"
    os.mkfifo(fifo)

    store.cleanup(workspace)

    assert not fifo.exists()
    assert not workspace.path.exists()


@POSIX_ONLY
def test_snapshot_reservation_counts_against_workspace_quota(tmp_path: Path) -> None:
    store = LocalMediaSnapshotStore(
        tmp_path / "private", max_workspace_bytes=256, max_snapshot_bytes=10
    )
    workspace = store.create_workspace(JobId("safe"), 1)

    store._reserve_workspace(workspace.path, 60)
    with pytest.raises(ResourceLimitExceededError):
        store._reserve_snapshot(workspace.path, 20)


def test_windows_kernel32_binding_is_injectable() -> None:
    class Function:
        def __call__(self, *args):
            del args
            return True

    class FakeKernel32:
        CreateFileW = Function()
        GetFileInformationByHandle = Function()
        GetFileType = Function()
        GetFinalPathNameByHandleW = Function()
        ReadFile = Function()
        WriteFile = Function()
        SetFilePointerEx = Function()
        SetFileInformationByHandle = Function()
        CloseHandle = Function()
        CreateDirectoryW = Function()
        SetFileAttributesW = Function()
        DeleteFileW = Function()
        RemoveDirectoryW = Function()
        FindNextFileW = Function()
        GetOverlappedResult = Function()

    api = WindowsKernel32(FakeKernel32())

    assert api.dll.CreateFileW.argtypes[0] is ctypes.wintypes.LPCWSTR
    assert api.dll.CreateFileW.restype is ctypes.wintypes.HANDLE
    assert api.dll.GetFileInformationByHandle.argtypes[0] is ctypes.wintypes.HANDLE
    assert api.dll.GetFileInformationByHandle.restype is ctypes.wintypes.BOOL
    assert api.dll.CloseHandle.argtypes == [ctypes.wintypes.HANDLE]
    assert api.dll.CloseHandle.errcheck is windows_snapshot._check_bool
    assert api.dll.ReadFile.errcheck is windows_snapshot._check_read_bool
    assert api.dll.FindNextFileW.errcheck is windows_snapshot._check_find_next
    assert api.dll.GetOverlappedResult.errcheck is windows_snapshot._check_overlapped_result


@pytest.mark.parametrize(
    ("checker", "error_code"),
    [
        (windows_snapshot._check_find_next, windows_snapshot._ERROR_NO_MORE_FILES),
        (windows_snapshot._check_overlapped_result, windows_snapshot._ERROR_HANDLE_EOF),
    ],
)
def test_windows_snapshot_allows_terminal_native_errors(
    monkeypatch: pytest.MonkeyPatch,
    checker,
    error_code: int,
) -> None:
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error_code, raising=False)

    assert checker(False, None, None) is False


def test_windows_snapshot_read_returns_partial_data_at_sync_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def read_file(handle, buffer, size, count, overlapped):
        del handle, size, overlapped
        nonlocal calls
        calls += 1
        count_value = ctypes.cast(count, ctypes.POINTER(ctypes.wintypes.DWORD)).contents
        if calls == 1:
            ctypes.memmove(buffer, b"abc", 3)
            count_value.value = 3
            return True
        count_value.value = 0
        return False

    monkeypatch.setattr(
        ctypes,
        "get_last_error",
        lambda: windows_snapshot._ERROR_HANDLE_EOF,
        raising=False,
    )
    store = object.__new__(WindowsMediaSnapshotStore)
    store._api = SimpleNamespace(dll=SimpleNamespace(ReadFile=read_file))

    assert store._read(123, 5) == b"abc"
    assert calls == 2


def test_windows_snapshot_overlapped_read_treats_eof_as_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def get_overlapped_result(handle, overlapped, count, wait):
        del handle, overlapped, count, wait
        return False

    api = SimpleNamespace(
        dll=SimpleNamespace(
            WaitForSingleObject=lambda handle, timeout: 0,
            GetOverlappedResult=get_overlapped_result,
        )
    )
    store = object.__new__(WindowsMediaSnapshotStore)
    store._api = api
    count = ctypes.wintypes.DWORD(5)
    monkeypatch.setattr(
        ctypes,
        "get_last_error",
        lambda: windows_snapshot._ERROR_HANDLE_EOF,
        raising=False,
    )

    result = store._wait_overlapped(
        1,
        windows_snapshot._WindowsOverlapped(),
        2,
        count,
        None,
        None,
        b"data",
        allow_eof=True,
    )

    assert result is False
    assert count.value == 0


def test_windows_snapshot_overlapped_read_closes_handles_on_initial_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []

    def read_file(handle, buffer, size, count, overlapped):
        del handle, buffer, size, count, overlapped
        return False

    store = object.__new__(WindowsMediaSnapshotStore)
    store._api = SimpleNamespace(dll=SimpleNamespace(ReadFile=read_file))
    store._duplicate = lambda handle: 2
    store._new_overlapped = lambda: (windows_snapshot._WindowsOverlapped(), 3)
    store._close_handles = lambda handles: closed.extend(handles) or []
    monkeypatch.setattr(
        ctypes,
        "get_last_error",
        lambda: windows_snapshot._ERROR_HANDLE_EOF,
        raising=False,
    )

    assert store._read_overlapped(1, 4, 0, None, None) == b""
    assert closed == [3, 2]


def test_windows_snapshot_overlapped_write_rejects_eof_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = SimpleNamespace(
        dll=SimpleNamespace(
            WaitForSingleObject=lambda handle, timeout: 0,
            GetOverlappedResult=lambda handle, overlapped, count, wait: False,
        )
    )
    store = object.__new__(WindowsMediaSnapshotStore)
    store._api = api
    count = ctypes.wintypes.DWORD()
    monkeypatch.setattr(
        ctypes,
        "get_last_error",
        lambda: windows_snapshot._ERROR_HANDLE_EOF,
        raising=False,
    )
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "native"), raising=False)

    with pytest.raises(OSError):
        store._wait_overlapped(
            1,
            windows_snapshot._WindowsOverlapped(),
            2,
            count,
            None,
            None,
            b"data",
        )


def test_windows_snapshot_directory_names_accepts_end_of_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []

    def find_first(pattern, data_pointer):
        del pattern
        data = ctypes.cast(data_pointer, ctypes.POINTER(windows_snapshot._WindowsFindData))
        data.contents.name = "entry"
        return 7

    def find_next(handle, data_pointer):
        del handle, data_pointer
        return False

    api = SimpleNamespace(
        dll=SimpleNamespace(
            FindFirstFileW=find_first,
            FindNextFileW=find_next,
            FindClose=lambda handle: closed.append(handle) or True,
        ),
        INVALID_HANDLE_VALUE=-1,
        ERROR_NO_MORE_FILES=windows_snapshot._ERROR_NO_MORE_FILES,
    )
    store = object.__new__(WindowsMediaSnapshotStore)
    store._api = api
    monkeypatch.setattr(
        ctypes,
        "get_last_error",
        lambda: windows_snapshot._ERROR_NO_MORE_FILES,
        raising=False,
    )

    assert store._directory_names(Path("C:/workspace")) == ("entry",)
    assert closed == [7]


def test_bounded_pcm_sink_accepts_streaming_wav_sizes() -> None:
    wav = bytearray(make_wav(b"\x00\x00" * 4))
    wav[4:8] = b"\xff\xff\xff\xff"
    wav[40:44] = b"\xff\xff\xff\xff"
    sink = BoundedPcmSink(max_bytes=64 * 1024, max_samples=4, max_pcm_bytes=8)

    sink.write(bytes(wav[:17]))
    sink.write(bytes(wav[17:]))

    result = sink.normalized_audio(WavLimits(pcm_bytes=8, sample_count=4))
    assert result.audio.pcm16le == b"\x00\x00" * 4


def test_windows_snapshot_delete_ignores_readonly_attribute() -> None:
    class Function:
        def __init__(self, value=True) -> None:
            self.value = value
            self.calls: list[tuple[object, ...]] = []

        def __call__(self, *args):
            self.calls.append(args)
            return self.value

    class FakeKernel32:
        CreateFileW = Function()
        GetFileInformationByHandle = Function()
        GetFileType = Function()
        GetFinalPathNameByHandleW = Function()
        ReadFile = Function()
        WriteFile = Function()
        SetFilePointerEx = Function()
        SetFileInformationByHandle = Function()
        CloseHandle = Function()
        CreateDirectoryW = Function()
        SetFileAttributesW = Function()
        DeleteFileW = Function()
        RemoveDirectoryW = Function()

    api = WindowsKernel32(FakeKernel32())
    store = object.__new__(WindowsMediaSnapshotStore)
    store._api = api

    store._delete_handle(123)

    call = api.dll.SetFileInformationByHandle.calls[-1]
    disposition = ctypes.cast(
        call[2], ctypes.POINTER(windows_snapshot._WindowsFileDispositionEx)
    ).contents
    assert call[1] == api.FILE_DISPOSITION_INFO_EX
    assert disposition.flags == (
        api.FILE_DISPOSITION_FLAG_DELETE | api.FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE
    )


def test_windows_snapshot_reopens_revalidates_and_closes_all_no_delete_handles() -> None:
    class Function:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def __call__(self, *args):
            self.calls.append(args)
            return True

    close_handle = Function()
    api = SimpleNamespace(
        dll=SimpleNamespace(CloseHandle=close_handle),
        GENERIC_READ=1,
        OPEN_EXISTING=3,
        FILE_SHARE_READ=1,
    )
    store = object.__new__(WindowsMediaSnapshotStore)
    store._api = api
    store.root = Path("C:/private")
    store._root_canonical = "c:\\private"
    store._root_identity = "1:root"
    store.max_snapshot_bytes = 100
    source_path = Path("C:/imports/input.wav")
    source_digest = hashlib.sha256(b"source").hexdigest()
    source = SourceMedia(source_path, identity="source", size=6, sha256=source_digest)
    snapshot_path = store.root / "job" / "attempt-1" / "source.snapshot"
    snapshot_digest = hashlib.sha256(b"media").hexdigest()
    snapshot = SourceSnapshot(
        source,
        snapshot_path,
        SnapshotManifest("1:file", 5, snapshot_digest, "1:job", "1:attempt"),
        source.identity,
        source.size,
        source.sha256,
    )
    opened: list[tuple[str, int | None]] = []
    closed: list[int] = []
    identities = {
        10: ("1:root", 0),
        11: ("1:job", 0),
        12: ("1:attempt", 0),
        13: ("1:file", 5),
    }
    store._assert_no_reparse_components = lambda path: None
    store._contained_path = lambda path, root: True
    store._open = lambda path, access, disposition, **kwargs: (
        opened.append(("root", kwargs.get("share"))) or 10
    )
    store._open_relative = lambda parent, name, **kwargs: (
        opened.append((name, kwargs.get("share")))
        or {"job": 11, "attempt-1": 12, "source.snapshot": 13}[name]
    )
    store._identity = lambda handle: identities[handle]
    store._hash = lambda handle, limit, **kwargs: (snapshot_digest, 5)
    store._read_manifest = lambda path, **kwargs: {
        "snapshot_identity": "1:file",
        "snapshot_size": 5,
        "snapshot_sha256": snapshot_digest,
        "job_identity": "1:job",
        "attempt_identity": "1:attempt",
    }
    store._assert_contained_handle = lambda handle: None
    store._seek = lambda handle, offset: None
    store._close = lambda handle: closed.append(handle)

    verified = store.verify(snapshot)
    assert [share for _, share in opened] == [api.FILE_SHARE_READ] * 4
    verified.verified_input.revalidate()
    assert verified.verified_input.revalidate is not None
    store.release_snapshot(verified)
    assert closed == []
    assert len(close_handle.calls) == 4


def test_windows_cleanup_stops_before_operation_after_absolute_deadline() -> None:
    store = object.__new__(WindowsMediaSnapshotStore)
    store._clock = FakeClock(0.0)

    with pytest.raises(TimeoutError, match="cleanup deadline"):
        store._remove_tree(Path("C:/private/job/attempt-1"), deadline=-1.0)


@POSIX_ONLY
def test_snapshot_lifecycle_checks_use_injected_clock_at_deadline_boundary(
    tmp_path: Path,
) -> None:
    clock = FakeClock(10.0)
    store = LocalMediaSnapshotStore(tmp_path / "private", clock=clock)
    workspace = store.create_workspace(JobId("clock"), 1)

    with pytest.raises(TimeoutError, match="cleanup deadline"):
        store.cleanup(workspace, deadline=10.0)
    with pytest.raises(AsrTimeoutError, match="snapshot operation"):
        media_snapshot._check_lifecycle(None, 10.0, clock)
    with pytest.raises(AsrTimeoutError, match="snapshot operation"):
        windows_snapshot._check_lifecycle(None, 10.0, clock)
    with pytest.raises(TimeoutError, match="cleanup deadline"):
        windows_snapshot._check_cleanup_deadline(10.0, clock)


@pytest.mark.parametrize("file_type", [2, 3])
def test_windows_snapshot_admission_rejects_device_and_pipe_handles(file_type: int) -> None:
    class FakeDll:
        def CreateFileW(self, *args):
            del args
            return ctypes.c_void_p(17)

        def GetFileInformationByHandle(self, handle, pointer):
            del handle
            pointer = ctypes.cast(pointer, ctypes.POINTER(windows_snapshot._WindowsFileInformation))
            pointer.contents.attributes = 0
            return True

        def GetFileType(self, handle):
            del handle
            return file_type

        def CloseHandle(self, handle):
            del handle
            return True

    api = SimpleNamespace(
        dll=FakeDll(),
        GENERIC_READ=0x80000000,
        SHARE=0x7,
        OPEN_EXISTING=3,
        FILE_FLAG_OPEN_REPARSE_POINT=0x00200000,
        FILE_FLAG_BACKUP_SEMANTICS=0x02000000,
        FILE_ATTRIBUTE_REPARSE_POINT=0x400,
        FILE_ATTRIBUTE_DIRECTORY=0x10,
        FILE_TYPE_DISK=1,
        INVALID_HANDLE_VALUE=ctypes.c_void_p(-1).value,
    )
    store = object.__new__(WindowsMediaSnapshotStore)
    store._api = api

    with pytest.raises(OSError, match="disk files"):
        store._open(Path("C:/input"), api.GENERIC_READ, api.OPEN_EXISTING)

    adapter = object.__new__(NativeWindowsMediaSecurityAdapter)
    adapter._kernel32 = api.dll
    with pytest.raises(InvalidSourceError, match="regular file"):
        adapter._open(Path("C:/input"), is_directory=False)


def test_windows_job_object_binding_and_lifecycle_are_injectable() -> None:
    class Function:
        def __init__(self, value=True) -> None:
            self.value = value
            self.calls: list[tuple[object, ...]] = []

        def __call__(self, *args):
            self.calls.append(args)
            return self.value

    class FakeKernel32:
        CreateJobObjectW = Function(ctypes.c_void_p(123))
        SetInformationJobObject = Function()
        AssignProcessToJobObject = Function()
        TerminateJobObject = Function()
        CloseHandle = Function()

    api = WindowsJobObject(kernel32=FakeKernel32())
    process = type("Process", (), {"_handle": 456})()
    api.assign(process)
    api.terminate()
    api.close()

    assert api._kernel32.CreateJobObjectW.argtypes == [
        ctypes.wintypes.LPVOID,
        ctypes.wintypes.LPCWSTR,
    ]
    assert api._kernel32.CreateJobObjectW.restype is ctypes.wintypes.HANDLE
    assert api._kernel32.AssignProcessToJobObject.argtypes == [
        ctypes.wintypes.HANDLE,
        ctypes.wintypes.HANDLE,
    ]
    assert api._kernel32.AssignProcessToJobObject.calls[-1][0].value == 123
    assert api._kernel32.AssignProcessToJobObject.calls[-1][1] == 456
    assert api._kernel32.TerminateJobObject.calls[-1][0].value == 123
    assert api._kernel32.TerminateJobObject.calls[-1][1] == 1
    assert api._kernel32.CloseHandle.calls[-1][0].value == 123


def test_windows_runner_cleans_process_and_job_when_assignment_fails(monkeypatch) -> None:
    class Function:
        def __init__(self, value=True) -> None:
            self.value = value
            self.calls: list[tuple[object, ...]] = []

        def __call__(self, *args):
            self.calls.append(args)
            return self.value

    class FakeKernel32:
        CreateJobObjectW = Function(ctypes.c_void_p(321))
        SetInformationJobObject = Function()
        AssignProcessToJobObject = Function(False)
        TerminateJobObject = Function()
        CloseHandle = Function()

    class Process:
        _handle = 654

        def __init__(self) -> None:
            self.returncode = None
            self.killed = False

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self, timeout=None):
            del timeout
            self.returncode = -9
            return self.returncode

    process = Process()
    monkeypatch.setattr(media_process.os, "name", "nt")
    monkeypatch.setattr(media_process.subprocess, "Popen", lambda *args, **kwargs: process)
    runner = WindowsJobObjectProcessRunner(kernel32=FakeKernel32())

    with pytest.raises(OSError, match="AssignProcessToJobObject failed"):
        runner.run(
            ["ffmpeg"],
            timeout=1,
            cancellation=type("Token", (), {"is_cancelled": lambda self: False})(),
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )

    assert runner._kernel32.TerminateJobObject.calls
    assert runner._kernel32.CloseHandle.calls


def test_windows_runner_terminates_job_on_collector_exception(monkeypatch) -> None:
    class Function:
        def __init__(self, value=True) -> None:
            self.value = value
            self.calls: list[tuple[object, ...]] = []

        def __call__(self, *args):
            self.calls.append(args)
            return self.value

    class FakeKernel32:
        CreateJobObjectW = Function(ctypes.c_void_p(322))
        SetInformationJobObject = Function()
        AssignProcessToJobObject = Function()
        TerminateJobObject = Function()
        CloseHandle = Function()

    class Process:
        _handle = 655
        pid = 4567

        def __init__(self) -> None:
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            self.returncode = -9
            return self.returncode

        def kill(self):
            self.returncode = -9

    process = Process()
    monkeypatch.setattr(media_process.os, "name", "nt")
    monkeypatch.setattr(media_process.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(media_process, "_resume_suspended_process", lambda pid: None)

    def fail_collection(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("collector failed")

    monkeypatch.setattr(media_process.SubprocessRunner, "_collect_process", fail_collection)
    runner = WindowsJobObjectProcessRunner(kernel32=FakeKernel32())

    with pytest.raises(RuntimeError, match="collector failed"):
        runner.run(
            ["ffmpeg"],
            timeout=1,
            cancellation=type("Token", (), {"is_cancelled": lambda self: False})(),
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )

    assert runner._kernel32.TerminateJobObject.calls
    assert runner._kernel32.CloseHandle.calls
    assert runner._kernel32.TerminateJobObject.calls[0][0].value == 322


def test_windows_snapshot_store_is_native_only_on_windows(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("contract assertion is for the non-Windows test host")
    with pytest.raises(RuntimeError):
        WindowsMediaSnapshotStore(tmp_path / "workspace")


def test_windows_cleanup_quarantines_job_after_active_lock_release(tmp_path: Path) -> None:
    root = tmp_path / "private"
    workspace_path = root / "job" / "attempt-1"
    workspace_path.mkdir(parents=True)
    workspace = JobWorkspace(
        workspace_path,
        JobId("job"),
        Attempt(1),
        "job-identity",
        "attempt-identity",
    )
    store = object.__new__(WindowsMediaSnapshotStore)
    store._clock = FakeClock()
    store.root = root
    events: list[str] = []

    class ActiveLock:
        def __enter__(self):
            events.append("active-lock-acquired")
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            del exc_type, exc_value, traceback
            events.append("active-lock-released")

    store.workspace_lock = lambda _workspace: ActiveLock()
    root_lock = object()
    store._acquire_root_lock = lambda **kwargs: root_lock
    store._release_workspace_lock = lambda lock: (
        events.append("root-lock-released") if lock is root_lock else None
    )
    store._cleanup_contents = lambda _workspace, **kwargs: (
        events.append("attempt-cleaned") or workspace_path.parent
    )

    def quarantine_job(path: Path, identity: str) -> Path:
        assert path == workspace_path.parent
        assert identity == workspace.job_identity
        assert events[-1] == "active-lock-released"
        events.append("job-quarantined")
        return root / ".voiceink-quarantine-test"

    store._quarantine_empty_job = quarantine_job
    store._remove_workspace_lock_file = lambda _path: events.append("lock-file-removed")
    store._remove_quarantine_tree = lambda _path, **kwargs: events.append("quarantine-removed")
    store._release_workspace_tree = lambda _path: events.append("quota-released")

    store.cleanup(workspace)

    assert events == [
        "active-lock-acquired",
        "attempt-cleaned",
        "active-lock-released",
        "job-quarantined",
        "lock-file-removed",
        "quarantine-removed",
        "quota-released",
        "root-lock-released",
    ]


def test_windows_native_path_policy_rejects_extended_unc() -> None:
    assert NativeWindowsMediaSecurityAdapter._is_unc(r"\\server\share\input.wav")
    assert NativeWindowsMediaSecurityAdapter._is_unc(r"\\?\UNC\server\share\input.wav")
    assert not NativeWindowsMediaSecurityAdapter._is_unc(r"\\?\C:\input.wav")


def test_native_smoke_awaits_store_reapers_before_temp_cleanup() -> None:
    report: dict[str, object] = {}
    context = _NativeSmokeTemporaryDirectory(report)

    class Store:
        def __init__(self) -> None:
            self.closed_while_present = False

        def close(self, timeout: float) -> None:
            del timeout
            self.closed_while_present = True

    store = Store()
    with context as temporary:
        marker = Path(temporary) / "marker"
        marker.write_text("keep", encoding="ascii")
        context.store = store

    assert store.closed_while_present
    assert report["overlapped_reapers_awaited"] is True
    assert not marker.exists()


@POSIX_ONLY
def test_sweep_only_removes_owned_old_workspaces(tmp_path: Path) -> None:
    store = LocalMediaSnapshotStore(tmp_path / "shared")
    unrelated = store.root / "unrelated" / "attempt-1"
    unrelated.mkdir(parents=True)
    (unrelated / "manifest.json").write_text(
        json.dumps(
            {
                "job_id": "unrelated",
                "attempt": 1,
                "job_identity": "not-the-directory",
                "attempt_identity": "not-the-directory",
            }
        ),
        encoding="ascii",
    )
    os.utime(unrelated, (1, 1))
    os.utime(unrelated.parent, (1, 1))
    unrelated_sentinel = unrelated.parent / "sentinel.bin"
    unrelated_sentinel.write_bytes(b"do not mutate")
    os.utime(unrelated.parent, (1, 1))
    unknown_dot = store.root / ".unknown-stale-directory"
    unknown_dot.mkdir()
    (unknown_dot / "sentinel.bin").write_bytes(b"also do not mutate")
    os.utime(unknown_dot, (1, 1))
    before = {
        path.relative_to(store.root): path.read_bytes()
        for path in (unrelated / "manifest.json", unrelated_sentinel, unknown_dot / "sentinel.bin")
    }
    owned = store.create_workspace(JobId("owned"), 1)
    os.utime(owned.path, None)
    os.utime(owned.path.parent, (1, 1))

    assert store.sweep_orphans(max_age_seconds=1) == 1
    assert unrelated.exists()
    assert not (unrelated.parent / media_snapshot.WORKSPACE_ACTIVE_LOCK).exists()
    assert not (unrelated.parent / ".voiceink.lock").exists()
    assert unknown_dot.exists()
    assert {
        path.relative_to(store.root): path.read_bytes()
        for path in (unrelated / "manifest.json", unrelated_sentinel, unknown_dot / "sentinel.bin")
    } == before
    assert not owned.path.exists()


@POSIX_ONLY
def test_sweep_rejects_mixed_owned_and_unknown_children_without_mutation(tmp_path: Path) -> None:
    store = LocalMediaSnapshotStore(tmp_path / "shared")
    workspace = store.create_workspace(JobId("mixed"), 1)
    unknown_file = workspace.path.parent / "unrelated.bin"
    unknown_file.write_bytes(b"preserve")
    invalid_attempt = workspace.path.parent / "attempt-2"
    invalid_attempt.mkdir()
    (invalid_attempt / "sentinel").write_bytes(b"preserve too")
    os.utime(workspace.path.parent, (1, 1))
    before = {
        path.relative_to(store.root): path.read_bytes()
        for path in (unknown_file, invalid_attempt / "sentinel")
    }

    assert store.sweep_orphans(max_age_seconds=1) == 0
    assert workspace.path.exists()
    assert unknown_file.exists()
    assert invalid_attempt.exists()
    assert not (workspace.path.parent / media_snapshot.WORKSPACE_ACTIVE_LOCK).exists()
    assert {
        path.relative_to(store.root): path.read_bytes()
        for path in (unknown_file, invalid_attempt / "sentinel")
    } == before


@POSIX_ONLY
def test_root_quarantine_recovers_after_deletion_failure_and_releases_reservations(
    tmp_path: Path,
) -> None:
    store = LocalMediaSnapshotStore(tmp_path / "shared")
    workspace = store.create_workspace(JobId("restart-recovery"), 1)
    shutil.rmtree(workspace.path)
    unrelated = store.root / ".unrelated-stale-directory"
    unrelated.mkdir()
    sentinel = unrelated / "sentinel"
    sentinel.write_bytes(b"keep untouched")
    os.utime(workspace.path.parent, (1, 1))

    root_fd = store._open_dir(store.root)
    job_fd = store._open_child_dir(root_fd, workspace.job_id.value)
    original_remove = store._remove_quarantine_directory_locked
    store._remove_quarantine_directory_locked = lambda *args, **kwargs: (_ for _ in ()).throw(
        OSError("simulated deletion failure")
    )
    try:
        with pytest.raises(OSError, match="simulated deletion failure"):
            store._remove_empty_job_files_locked(
                root_fd,
                workspace.job_id.value,
                job_fd,
                workspace.job_identity,
            )
    finally:
        store._remove_quarantine_directory_locked = original_remove
        os.close(job_fd)
        os.close(root_fd)

    quarantines = tuple(store.root.glob(f"{media_snapshot.ROOT_QUARANTINE_PREFIX}*"))
    assert len(quarantines) == 1
    metadata_path = quarantines[0] / media_snapshot.QUARANTINE_METADATA
    metadata = json.loads(metadata_path.read_text(encoding="ascii"))
    metadata.pop("kind")
    metadata_path.write_text(json.dumps(metadata), encoding="ascii")
    recovered = LocalMediaSnapshotStore(store.root)

    assert not quarantines[0].exists()
    assert sentinel.read_bytes() == b"keep untouched"
    assert recovered._workspace_reserved == {}
    assert recovered._snapshot_reserved == {}


def test_windows_sweep_fail_closed_keeps_reparse_like_job_untouched(tmp_path: Path) -> None:
    root = tmp_path / "private"
    (root / media_snapshot.WORKSPACE_ROOT_MARKER).mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel").write_text("keep", encoding="ascii")
    job = root / "job"
    job.symlink_to(external, target_is_directory=True)

    store = object.__new__(WindowsMediaSnapshotStore)
    store.root = root
    store._api = SimpleNamespace(
        dll=SimpleNamespace(
            LockFileEx=lambda *args: True,
            UnlockFileEx=lambda *args: True,
        ),
        GENERIC_READ=1,
        GENERIC_WRITE=2,
        DELETE=4,
        OPEN_ALWAYS=4,
        OPEN_EXISTING=3,
        SHARE=7,
        LOCKFILE_EXCLUSIVE_LOCK=2,
        LOCKFILE_FAIL_IMMEDIATELY=1,
    )
    store._assert_owned_root_marker = lambda: None
    store._assert_no_reparse_components = lambda path: (
        (_ for _ in ()).throw(OSError("reparse point")) if path.is_symlink() else None
    )

    assert store.sweep_orphans(max_age_seconds=1) == 0
    assert (external / "sentinel").exists()
    assert job.is_symlink()


def test_windows_sweep_does_not_lock_unknown_stale_job(tmp_path: Path) -> None:
    root = tmp_path / "private"
    (root / media_snapshot.WORKSPACE_ROOT_MARKER).mkdir(parents=True)
    job = root / "unknown-job"
    job.mkdir()
    sentinel = job / "sentinel.bin"
    sentinel.write_bytes(b"preserve")
    os.utime(job, (1, 1))
    lock_calls: list[object] = []

    class Dll:
        def LockFileEx(self, *args):
            lock_calls.append(args)
            return True

        def UnlockFileEx(self, *args):
            return True

    store = object.__new__(WindowsMediaSnapshotStore)
    store.root = root
    store._api = SimpleNamespace(
        dll=Dll(),
        GENERIC_READ=1,
        GENERIC_WRITE=2,
        DELETE=4,
        OPEN_ALWAYS=4,
        OPEN_EXISTING=3,
        SHARE=7,
        LOCKFILE_EXCLUSIVE_LOCK=2,
        LOCKFILE_FAIL_IMMEDIATELY=1,
    )
    store._assert_owned_root_marker = lambda: None
    store._assert_no_reparse_components = lambda path: None
    store._assert_contained_handle = lambda handle: None
    store._open = lambda path, *args, **kwargs: path
    store._identity = lambda handle: ("job", 0)
    store._handle_mtime = lambda handle: 1.0
    store._close = lambda handle: None

    assert store.sweep_orphans(max_age_seconds=1) == 0
    assert sentinel.read_bytes() == b"preserve"
    assert not (job / media_snapshot.WORKSPACE_ACTIVE_LOCK).exists()
    assert not (job / ".voiceink.lock").exists()
    assert lock_calls == []


def test_windows_sweep_removes_valid_stale_workspace_using_job_age(tmp_path: Path) -> None:
    root = tmp_path / "private"
    (root / media_snapshot.WORKSPACE_ROOT_MARKER).mkdir(parents=True)
    attempt = root / "job" / "attempt-1"
    attempt.mkdir(parents=True)
    os.utime(attempt, None)
    os.utime(attempt.parent, (1, 1))

    store = object.__new__(WindowsMediaSnapshotStore)
    store.root = root
    store._api = SimpleNamespace(
        dll=SimpleNamespace(
            LockFileEx=lambda *args: True,
            UnlockFileEx=lambda *args: True,
        ),
        GENERIC_READ=1,
        GENERIC_WRITE=2,
        DELETE=4,
        OPEN_ALWAYS=4,
        OPEN_EXISTING=3,
        SHARE=7,
        LOCKFILE_EXCLUSIVE_LOCK=2,
        LOCKFILE_FAIL_IMMEDIATELY=1,
    )
    store._assert_owned_root_marker = lambda: None
    store._assert_no_reparse_components = lambda path: None
    store._assert_contained_handle = lambda handle: None
    store._open = lambda path, *args, **kwargs: path
    store._identity = lambda handle: ("job" if Path(handle) == attempt.parent else "attempt", 0)
    store._handle_mtime = lambda handle: 1.0
    store._close = lambda handle: None
    store._remove_tree = lambda path, **kwargs: shutil.rmtree(path)
    store._quarantine_attempt = lambda path, expected_identity, metadata: path
    store._release_workspace_tree = lambda path: None
    store._read_manifest = lambda path, **kwargs: {
        "job_id": "job",
        "attempt": 1,
        "job_identity": "job",
        "attempt_identity": "attempt",
    }
    store._quarantine_empty_job = lambda path, expected_identity: None

    assert store.sweep_orphans(max_age_seconds=1) == 1
    assert not attempt.exists()


def test_windows_quarantine_recovery_rewrites_metadata_after_transient_failure(
    tmp_path: Path,
) -> None:
    quarantine = tmp_path / "job" / ".voiceink-attempt-quarantine-retry"
    quarantine.mkdir(parents=True)
    store = object.__new__(WindowsMediaSnapshotStore)
    store._api = SimpleNamespace(GENERIC_READ=1, GENERIC_WRITE=2, OPEN_EXISTING=3)
    store._open = lambda path, *args, **kwargs: path
    store._identity = lambda handle: ("attempt", 0)
    store._close = lambda handle: None
    writes: list[dict[str, object]] = []
    store._write_manifest = lambda path, values, **kwargs: writes.append(values)
    failed = True

    def remove_once(path, **kwargs):
        nonlocal failed
        if failed:
            failed = False
            raise OSError("transient deletion failure")
        shutil.rmtree(path)

    store._remove_tree = remove_once
    metadata = {
        "job_id": "job",
        "attempt": 1,
        "job_identity": "job",
        "attempt_identity": "attempt",
    }

    with pytest.raises(OSError, match="transient deletion failure"):
        store._remove_quarantine_tree(
            quarantine,
            expected_identity="attempt",
            metadata=metadata,
        )

    assert writes == [metadata]
    store._remove_quarantine_tree(
        quarantine,
        expected_identity="attempt",
        metadata=metadata,
    )
    assert not quarantine.exists()


def test_windows_usage_scan_reopens_persisted_workspace_with_service_lock(tmp_path: Path) -> None:
    root = tmp_path / "private"
    attempt = root / "job" / "attempt-1"
    attempt.mkdir(parents=True)
    source_snapshot = attempt / "source.snapshot"
    source_snapshot.write_bytes(b"snapshot")
    manifest = attempt / "manifest.json"
    manifest.write_bytes(b"manifest")
    (root / "job" / media_snapshot.WORKSPACE_ACTIVE_LOCK).write_bytes(b"")

    store = object.__new__(WindowsMediaSnapshotStore)
    store.root = root
    store._api = SimpleNamespace(
        GENERIC_READ=1,
        OPEN_EXISTING=3,
        FILE_ATTRIBUTE_DIRECTORY=0x10,
        FILE_ATTRIBUTE_REPARSE_POINT=0x400,
        dll=SimpleNamespace(
            GetFileInformationByHandle=lambda handle, info: (
                setattr(
                    ctypes.cast(
                        info,
                        ctypes.POINTER(windows_snapshot._WindowsFileInformation),
                    ).contents,
                    "attributes",
                    0x10 if Path(handle).is_dir() else 0,
                )
                or True
            )
        ),
    )
    store._open = lambda path, *args, **kwargs: path
    store._close = lambda handle: None
    store._assert_contained_handle = lambda handle: None
    store._identity = lambda handle: (str(Path(handle)), Path(handle).stat().st_size)

    first_usage, first_snapshots = store._scan_workspace_usage()
    second_usage, second_snapshots = store._scan_workspace_usage()

    assert first_usage == second_usage
    assert first_snapshots == second_snapshots
    assert first_snapshots[attempt] == source_snapshot.stat().st_size
    assert first_usage[attempt] == manifest.stat().st_size


def test_windows_root_quarantine_recovery_skips_unvalidated_dot_entries(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    valid = root / ".voiceink-quarantine-recovery"
    valid.mkdir()
    (valid / media_snapshot.WORKSPACE_ACTIVE_LOCK).write_bytes(b"")
    (valid / media_snapshot.QUARANTINE_METADATA).write_text("metadata", encoding="ascii")
    unknown = root / ".voiceink-quarantine-unknown"
    unknown.mkdir()
    (unknown / "sentinel").write_text("keep", encoding="ascii")
    unrelated = root / ".voiceink-unrelated"
    unrelated.write_text("keep", encoding="ascii")

    store = object.__new__(WindowsMediaSnapshotStore)
    store.root = root
    store._api = SimpleNamespace(GENERIC_READ=1, OPEN_EXISTING=3, dll=None)
    store._assert_no_reparse_components = lambda path: None
    store._open = lambda path, *args, **kwargs: path
    store._close = lambda handle: None
    store._identity = lambda handle: ("job-identity", 0)
    store._read_manifest = lambda path, **kwargs: {
        "job_id": "persisted-job",
        "job_identity": "job-identity",
    }
    store._remove_workspace_lock_file = lambda path: (
        path / media_snapshot.WORKSPACE_ACTIVE_LOCK
    ).unlink()

    def remove_tree(path: Path, *, expected_identity: str, **kwargs) -> None:
        del kwargs
        assert expected_identity == "job-identity"
        children = tuple(path.iterdir())
        assert children, "cleanup must handle a non-empty quarantine"
        for child in children:
            if child.is_dir():
                remove_tree(child, expected_identity=expected_identity)
            else:
                child.unlink()
        path.rmdir()

    store._remove_tree = remove_tree

    store._recover_root_quarantines()

    assert not valid.exists()
    assert unknown.exists()
    assert unrelated.exists()


@POSIX_ONLY
def test_existing_valid_workspace_root_is_marked_during_upgrade(tmp_path: Path) -> None:
    store = LocalMediaSnapshotStore(tmp_path / "legacy")
    store.create_workspace(JobId("legacy"), 1)
    (store.root / media_snapshot.WORKSPACE_ROOT_MARKER).rmdir()

    upgraded = LocalMediaSnapshotStore(store.root)

    assert (upgraded.root / media_snapshot.WORKSPACE_ROOT_MARKER).is_dir()


@POSIX_ONLY
def test_existing_workspace_root_without_marker_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "existing"
    root.mkdir()
    root.chmod(0o700)
    unrelated = root / "unrelated"
    unrelated.mkdir()

    with pytest.raises(OSError, match="established safely"):
        LocalMediaSnapshotStore(root)
    assert unrelated.exists()


@POSIX_ONLY
def test_workspace_root_rejects_group_or_world_access(tmp_path: Path) -> None:
    root = tmp_path / "existing"
    root.mkdir(mode=0o700)
    (root / media_snapshot.WORKSPACE_ROOT_MARKER).mkdir(mode=0o700)
    root.chmod(0o720)

    with pytest.raises(OSError, match="workspace root"):
        LocalMediaSnapshotStore(root)


@POSIX_ONLY
def test_workspace_root_rejects_marker_with_group_or_world_access(tmp_path: Path) -> None:
    root = tmp_path / "existing"
    root.mkdir(mode=0o700)
    marker = root / media_snapshot.WORKSPACE_ROOT_MARKER
    marker.mkdir(mode=0o700)
    marker.chmod(0o720)

    with pytest.raises(OSError, match="workspace ownership marker"):
        LocalMediaSnapshotStore(root)


@POSIX_ONLY
def test_workspace_root_rejects_wrong_owner(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "existing"
    root.mkdir(mode=0o700)
    (root / media_snapshot.WORKSPACE_ROOT_MARKER).mkdir(mode=0o700)
    current_uid = os.getuid()
    monkeypatch.setattr(media_snapshot.os, "getuid", lambda: current_uid + 1)

    with pytest.raises(OSError, match="owner"):
        LocalMediaSnapshotStore(root)


@POSIX_ONLY
def test_source_symlink_is_rejected_without_following_target(tmp_path: Path) -> None:
    target = source_file(tmp_path)
    link = tmp_path / "linked-media"
    link.symlink_to(target)
    store = LocalMediaSnapshotStore(tmp_path / "private")

    with pytest.raises(InvalidSourceError) as error:
        store.validate_source(link, max_bytes=100)

    assert error.value.code is ErrorCode.INVALID_SOURCE
    assert target.exists()


@POSIX_ONLY
@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_source_admission_rejects_non_regular_files(tmp_path: Path, kind: str) -> None:
    candidate = tmp_path / kind
    if kind == "directory":
        candidate.mkdir()
    else:
        os.mkfifo(candidate)
        assert stat.S_ISFIFO(candidate.stat().st_mode)
    store = LocalMediaSnapshotStore(tmp_path / "private")

    with pytest.raises(InvalidSourceError):
        store.validate_source(candidate, max_bytes=100)


@POSIX_ONLY
def test_cleanup_fails_closed_when_attempt_directory_is_replaced(tmp_path: Path) -> None:
    store = LocalMediaSnapshotStore(tmp_path / "private")
    workspace = store.create_workspace(JobId("replacement"), 1)
    shutil.rmtree(workspace.path)
    workspace.path.mkdir()

    with pytest.raises(OSError, match="identity"):
        store.cleanup(workspace)

    assert workspace.path.exists()


@POSIX_ONLY
def test_cleanup_reclaims_owned_attempt_quarantine_after_transient_failure(
    tmp_path: Path, monkeypatch
) -> None:
    store = LocalMediaSnapshotStore(tmp_path / "private")
    workspace = store.create_workspace(JobId("quarantine-retry"), 1)
    (workspace.path / "payload").write_text("payload", encoding="ascii")
    original_rmdir = media_snapshot.os.rmdir
    failed = False

    def fail_once(name, **kwargs):
        nonlocal failed
        if (
            isinstance(name, str)
            and name.startswith(".voiceink-attempt-quarantine-")
            and not failed
        ):
            failed = True
            raise OSError("transient deletion failure")
        return original_rmdir(name, **kwargs)

    monkeypatch.setattr(media_snapshot.os, "rmdir", fail_once)

    with pytest.raises(OSError, match="transient deletion failure"):
        store.cleanup(workspace)

    quarantines = tuple(workspace.path.parent.glob(".voiceink-attempt-quarantine-*"))
    assert len(quarantines) == 1
    assert not (quarantines[0] / "manifest.json").exists()
    assert (quarantines[0] / media_snapshot.QUARANTINE_METADATA).exists()
    unrelated = workspace.path.parent / ".unrelated"
    unrelated.mkdir()

    store.cleanup(workspace)

    assert not quarantines[0].exists()
    assert unrelated.exists()


@POSIX_ONLY
def test_workspace_creation_exposes_partial_workspace_before_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    store = LocalMediaSnapshotStore(tmp_path / "private")
    original_mkdir = media_snapshot.os.mkdir

    def fail_attempt(name, mode=0o777, *, dir_fd=None):
        if name == "attempt-1":
            raise OSError("simulated attempt creation failure")
        return original_mkdir(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(media_snapshot.os, "mkdir", fail_attempt)

    with pytest.raises(OSError) as error:
        store.create_workspace(JobId("partial"), 1)

    partial = error.value.partial_workspace
    assert partial.path.parent.exists()
    store.cleanup(partial)
    assert not partial.path.parent.exists()


def test_windows_workspace_creation_exposes_partial_workspace() -> None:
    store = object.__new__(WindowsMediaSnapshotStore)
    store.root = Path("C:/private")
    store._api = SimpleNamespace(GENERIC_READ=1, OPEN_EXISTING=3)
    store._mkdir = lambda path: (
        (_ for _ in ()).throw(OSError("simulated attempt creation failure"))
        if path.name == "attempt-1"
        else None
    )
    store._assert_no_reparse_components = lambda path: None
    store._open = lambda *args, **kwargs: 1
    store._identity = lambda handle: ("job-identity", 0)
    store._close = lambda handle: None

    with pytest.raises(OSError) as error:
        store.create_workspace(JobId("partial"), 1)

    partial = error.value.partial_workspace
    assert partial.path == Path("C:/private/partial/attempt-1")
    assert partial.job_identity == "job-identity"


def test_windows_sweep_keeps_old_unmanifested_workspace_safely(tmp_path: Path) -> None:
    root = tmp_path / "private"
    (root / media_snapshot.WORKSPACE_ROOT_MARKER).mkdir(parents=True)
    attempt = root / "job" / "attempt-1"
    attempt.mkdir(parents=True)
    os.utime(attempt, (1, 1))
    os.utime(attempt.parent, (1, 1))

    store = object.__new__(WindowsMediaSnapshotStore)
    store.root = root
    store._api = SimpleNamespace(GENERIC_READ=1, DELETE=2, OPEN_EXISTING=3)
    store._open = lambda *args, **kwargs: 1
    store._identity = lambda handle: ("job" if handle == 1 else "attempt", 0)
    store._handle_mtime = lambda handle: 1.0
    store._assert_contained_handle = lambda handle: None
    store._close = lambda handle: None
    store._remove_tree = lambda path, **kwargs: shutil.rmtree(path)
    store._remove_directory = lambda path, **kwargs: path.rmdir()
    store._release_workspace_tree = lambda path: None
    store._read_manifest = lambda path, **kwargs: (_ for _ in ()).throw(FileNotFoundError())

    assert store.sweep_orphans(max_age_seconds=1) == 0
    assert root.joinpath("job").exists()


def test_reservation_release_is_owner_checked_and_idempotent() -> None:
    queue = ImportQueue(capacity=1)
    owner = JobId("owner")
    token = queue.reserve(owner)
    assert not token.release(JobId("other"))
    assert token.release(owner)
    assert not token.release(owner)
    assert token.state is ReservationState.RELEASED
    assert queue.reservation_count == 0


def test_committed_reservation_cannot_be_released_before_terminal_cas() -> None:
    queue = ImportQueue(capacity=1)
    owner = JobId("owner")
    token = queue.reserve(owner)
    entry = SimpleNamespace(job=SimpleNamespace(job_id=owner))
    queue.enqueue(entry, token)

    assert token.state is ReservationState.COMMITTED
    assert not token.release(owner)
    assert queue.reservation_count == 1
    assert not queue._release_committed(token, JobId("other"))
    assert queue.reservation_count == 1
    assert queue._release_committed(token, owner)
    assert queue.reservation_count == 0
    assert not queue._release_committed(token, owner)


@POSIX_ONLY
def test_ffmpeg_normalizer_builds_safe_first_audio_stream_argv(tmp_path: Path) -> None:
    class NeverCancelled:
        def is_cancelled(self) -> bool:
            return False

    class Runner:
        def __init__(self) -> None:
            self.argv: list[str] = []

        def run(self, argv, **kwargs):
            self.argv = argv
            return ProcessResult(make_wav(), b"", 0)

    runner = Runner()
    source = source_file(tmp_path)
    store = LocalMediaSnapshotStore(tmp_path / "work")
    workspace = store.create_workspace(JobId("job"), 1)
    snapshot = store.verify(store.snapshot(store.validate_source(source, max_bytes=100), workspace))
    normalizer = SubprocessMediaNormalizer(runner=runner)

    result = normalizer.normalize(snapshot, workspace, NeverCancelled(), time.monotonic() + 1)
    store.release_snapshot(snapshot)

    assert result.sample_count == 1600
    assert runner.argv[0] == "ffmpeg"
    assert runner.argv[runner.argv.index("-map") + 1] == "0:a:0"
    assert "shell=True" not in runner.argv


def test_ffmpeg_normalizer_accepts_windows_drive_paths() -> None:
    assert SubprocessMediaNormalizer._is_allowed_input(r"C:\workspace\input.wav")
    assert not SubprocessMediaNormalizer._is_allowed_input("https://example.test/input.wav")


@POSIX_ONLY
def test_ffmpeg_execution_lock_rejects_replaced_executable(tmp_path: Path) -> None:
    executable = tmp_path / "ffmpeg"
    executable.write_bytes(b"trusted ffmpeg")
    artifact = VerifiedFfmpegArtifact.verify(
        executable,
        FfmpegArtifactManifest(
            "ffmpeg-test",
            "https://example.invalid/ffmpeg",
            hashlib.sha256(b"trusted ffmpeg").hexdigest(),
            "GPL-3.0-or-later",
            executable,
        ),
    )
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"untrusted ffmpeg")
    replacement.replace(executable)

    with pytest.raises(ConfigurationError, match="identity|checksum"):
        with artifact.execution_lock():
            pass
