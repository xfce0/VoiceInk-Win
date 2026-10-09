from __future__ import annotations

import time
from threading import Event, Thread

import pytest

from voiceink_win.application import AsrApplicationService, CancellationTokenSource
from voiceink_win.domain import (
    AsrRequest,
    AsrTimeoutError,
    BackendUnavailableError,
    CancellationError,
    ExecutionError,
    InvalidInputError,
    MissingModelError,
    ProcessCrashedError,
    ProtocolError,
    QueueFullError,
    RuntimeRecoveryPendingError,
    RuntimeUnavailableError,
    TranscriptResult,
)
from voiceink_win.infrastructure import FakeAsrRuntime, FakeAsrScenario


def request(
    *, deadline: float | None = None, cancellation=None, request_id: str = "test-request"
) -> AsrRequest:
    from voiceink_win.domain import CanonicalAudio

    return AsrRequest(
        CanonicalAudio(b"\x00\x00" * 16_000),
        request_id=request_id,
        deadline=deadline,
        cancellation=cancellation,
        include_timestamps=True,
    )


def test_application_service_returns_fake_success() -> None:
    result = AsrApplicationService(FakeAsrRuntime()).transcribe(request())

    assert result == TranscriptResult(
        text="fake transcript",
        duration=1.0,
        segments=result.segments,
        detected_language="en",
    )


@pytest.mark.parametrize(
    ("scenario", "error_type"),
    [
        (FakeAsrScenario.INVALID_INPUT, InvalidInputError),
        (FakeAsrScenario.UNAVAILABLE, RuntimeUnavailableError),
        (FakeAsrScenario.MISSING_MODEL, MissingModelError),
        (FakeAsrScenario.TIMEOUT, AsrTimeoutError),
        (FakeAsrScenario.CANCELLATION, CancellationError),
        (FakeAsrScenario.BACKEND_UNAVAILABLE, BackendUnavailableError),
        (FakeAsrScenario.CRASH, ProcessCrashedError),
        (FakeAsrScenario.MALFORMED_RESULT, ProtocolError),
    ],
)
def test_application_service_preserves_fake_failure_categories(scenario, error_type) -> None:
    with pytest.raises(error_type):
        AsrApplicationService(FakeAsrRuntime(scenario)).transcribe(request())


def test_application_service_rejects_cancelled_request_before_runtime_call() -> None:
    source = CancellationTokenSource()
    source.cancel()

    with pytest.raises(CancellationError):
        AsrApplicationService(FakeAsrRuntime()).transcribe(request(cancellation=source.token))


def test_application_service_enforces_deadline_before_runtime_call() -> None:
    with pytest.raises(AsrTimeoutError):
        AsrApplicationService(FakeAsrRuntime()).transcribe(request(deadline=time.monotonic() - 1))


def test_application_service_does_not_turn_unexpected_exception_into_empty_success() -> None:
    class BrokenRuntime(FakeAsrRuntime):
        def transcribe(self, request: AsrRequest) -> TranscriptResult:
            raise RuntimeError("boom")

    with pytest.raises(ExecutionError) as error:
        AsrApplicationService(BrokenRuntime()).transcribe(request())

    assert error.value.cause is not None


def test_application_service_returns_on_deadline_while_cooperative_worker_finishes_later() -> None:
    started = Event()
    release = Event()

    class SlowRuntime(FakeAsrRuntime):
        def transcribe(self, request: AsrRequest) -> TranscriptResult:
            started.set()
            release.wait()
            return super().transcribe(request)

    service = AsrApplicationService(SlowRuntime())
    deadline = time.monotonic() + 0.2
    with pytest.raises(AsrTimeoutError):
        service.transcribe(request(deadline=deadline))
    assert started.is_set()
    release.set()


def test_application_service_uses_runtime_concurrency_cap() -> None:
    started = Event()
    release = Event()
    calls = 0

    class CappedRuntime(FakeAsrRuntime):
        def transcribe(self, request: AsrRequest) -> TranscriptResult:
            nonlocal calls
            calls += 1
            started.set()
            release.wait()
            return super().transcribe(request)

    runtime = CappedRuntime(max_concurrency=1)
    service = AsrApplicationService(runtime, max_concurrency=4, queue_capacity=1)
    first = Thread(target=lambda: service.transcribe(request()), daemon=True)
    second = Thread(target=lambda: service.transcribe(request()), daemon=True)
    first.start()
    assert started.wait(1.0)
    second.start()
    time.sleep(0.05)
    assert calls == 1
    release.set()
    first.join(1.0)
    second.join(1.0)
    service.close()


def test_application_service_rejects_full_queue() -> None:
    started = Event()
    release = Event()

    class BlockingRuntime(FakeAsrRuntime):
        def transcribe(self, request: AsrRequest) -> TranscriptResult:
            started.set()
            release.wait()
            return super().transcribe(request)

    service = AsrApplicationService(BlockingRuntime(), queue_capacity=1)
    first = Thread(target=lambda: service.transcribe(request()), daemon=True)
    second = Thread(target=lambda: service.transcribe(request()), daemon=True)
    first.start()
    assert started.wait(1.0)
    second.start()
    time.sleep(0.05)

    with pytest.raises(QueueFullError):
        service.transcribe(request())
    release.set()
    first.join(1.0)
    second.join(1.0)
    service.close()


def test_request_scoped_admission_is_bounded_and_releases_once() -> None:
    started = Event()
    release = Event()

    class BlockingRuntime(FakeAsrRuntime):
        def transcribe(self, request: AsrRequest) -> TranscriptResult:
            started.set()
            release.wait()
            return TranscriptResult("first", request.audio.duration)

    service = AsrApplicationService(BlockingRuntime(), queue_capacity=1)
    active = service.try_admit(request())
    assert started.wait(1.0)
    fence = active.quiescence_event
    assert not fence.is_set()
    with pytest.raises(AttributeError):
        fence.set()  # type: ignore[attr-defined]
    queued = service.try_admit(request())

    with pytest.raises(QueueFullError):
        service.try_admit(request())
    assert service.admitted_count == 2

    queued.cancel()
    with pytest.raises(CancellationError):
        queued.await_result()
    queued.await_quiescence(deadline=time.monotonic() + 1.0)
    queued.release()
    queued.release()

    release.set()
    assert active.await_result().text == "first"
    active.await_quiescence(deadline=time.monotonic() + 1.0)
    active.release()
    assert service.admitted_count == 0
    service.close()


def test_request_cancellation_fences_late_result_until_quiescence() -> None:
    started = Event()
    release = Event()

    class LateRuntime(FakeAsrRuntime):
        def transcribe(self, request: AsrRequest) -> TranscriptResult:
            started.set()
            release.wait()
            return TranscriptResult("late", request.audio.duration)

    service = AsrApplicationService(LateRuntime())
    handle = service.try_admit(request())
    assert started.wait(1.0)

    handle.cancel()
    with pytest.raises(CancellationError):
        handle.await_result()
    with pytest.raises(RuntimeRecoveryPendingError):
        handle.release()
    with pytest.raises(RuntimeRecoveryPendingError):
        handle.await_quiescence(deadline=time.monotonic() + 0.01)

    release.set()
    handle.await_quiescence(deadline=time.monotonic() + 1.0)
    with pytest.raises(CancellationError):
        handle.await_result()
    handle.release()
    assert service.admitted_count == 0
    service.close()


def test_late_result_cannot_cross_request_generation_fence() -> None:
    first_started = Event()
    first_release = Event()

    class GenerationRuntime(FakeAsrRuntime):
        def transcribe(self, request: AsrRequest) -> TranscriptResult:
            if request.request_id == "first":
                first_started.set()
                first_release.wait()
                return TranscriptResult("stale", request.audio.duration)
            return TranscriptResult("current", request.audio.duration)

    service = AsrApplicationService(GenerationRuntime(), queue_capacity=1)
    first = service.try_admit(request(request_id="first"))
    assert first_started.wait(1.0)
    first.cancel()
    second = service.try_admit(request(request_id="second"))
    assert second.generation != first.generation

    with pytest.raises(CancellationError):
        first.await_result()
    first_release.set()
    first.await_quiescence(deadline=time.monotonic() + 1.0)
    first.release()

    assert second.await_result().text == "current"
    second.await_quiescence(deadline=time.monotonic() + 1.0)
    second.release()
    service.close()


def test_application_service_close_cancels_active_call_and_rejects_new_work() -> None:
    started = Event()
    caller_done = Event()
    caller_error: list[Exception] = []

    class CooperativeRuntime(FakeAsrRuntime):
        def transcribe(self, request: AsrRequest) -> TranscriptResult:
            started.set()
            while not request.cancellation.is_cancelled():
                time.sleep(0.005)
            raise CancellationError("cooperative cancellation")

    service = AsrApplicationService(CooperativeRuntime(), shutdown_timeout=1.0)

    def call() -> None:
        try:
            service.transcribe(request())
        except Exception as error:
            caller_error.append(error)
        finally:
            caller_done.set()

    caller = Thread(target=call, daemon=True)
    caller.start()
    assert started.wait(1.0)
    service.close()

    assert caller_done.wait(1.0)
    assert isinstance(caller_error[0], CancellationError)
    with pytest.raises(RuntimeUnavailableError):
        service.transcribe(request())


def test_application_close_passes_absolute_deadline_to_runtime() -> None:
    class DeadlineRuntime(FakeAsrRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.close_deadline = None

        def close(self, deadline=None) -> None:
            self.close_deadline = deadline
            super().close()

    from voiceink_win.infrastructure import FakeClock

    clock = FakeClock(100.0)
    runtime = DeadlineRuntime()
    service = AsrApplicationService(runtime, clock=clock)

    service.close(deadline=104.0)

    assert runtime.close_deadline == 104.0


def test_application_close_reports_pending_runtime_recovery_without_dropping_ownership() -> None:
    started = Event()
    release = Event()

    class StubbornRuntime(FakeAsrRuntime):
        def close(self, deadline=None) -> None:
            del deadline
            started.set()
            release.wait(1.0)
            super().close()

    from voiceink_win.infrastructure import FakeClock

    clock = FakeClock(100.0)
    runtime = StubbornRuntime()
    service = AsrApplicationService(runtime, clock=clock)

    with pytest.raises(RuntimeRecoveryPendingError):
        service.close(deadline=100.0)

    assert started.wait(1.0)
    assert service._state.value == "closing"
    assert service._runtime_close_thread is not None
    assert service._runtime_close_thread.is_alive()

    release.set()
    service._runtime_close_thread.join(1.0)
    assert service._state.value == "closed"


def test_application_close_preserves_pending_runtime_error_and_retries_with_new_deadline() -> None:
    class PendingOnceRuntime(FakeAsrRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.close_attempts = 0
            self.close_deadlines: list[float | None] = []

        def close(self, deadline=None) -> None:
            self.close_attempts += 1
            self.close_deadlines.append(deadline)
            if self.close_attempts == 1:
                raise RuntimeRecoveryPendingError("runtime cleanup is still pending")
            super().close()

    runtime = PendingOnceRuntime()
    service = AsrApplicationService(runtime)
    first_deadline = time.monotonic() + 1.0
    second_deadline = first_deadline + 1.0

    with pytest.raises(RuntimeRecoveryPendingError):
        service.close(deadline=first_deadline)

    assert service._state.value == "closing"
    assert not service._runtime_close_started

    service.close(deadline=second_deadline)

    assert runtime.close_attempts == 2
    assert runtime.close_deadlines[0] < runtime.close_deadlines[1]
    assert service._state.value == "closed"


def test_application_close_retries_transient_runtime_error() -> None:
    class TransientCloseRuntime(FakeAsrRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.close_attempts = 0

        def close(self, deadline=None) -> None:
            del deadline
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise RuntimeUnavailableError("runtime close temporarily unavailable")
            super().close()

    runtime = TransientCloseRuntime()
    service = AsrApplicationService(runtime)
    first_deadline = time.monotonic() + 5.0
    second_deadline = first_deadline + 5.0

    with pytest.raises(RuntimeUnavailableError):
        service.close(deadline=first_deadline)

    service.close(deadline=second_deadline)

    assert runtime.close_attempts == 2
    assert service._state.value == "closed"


def test_application_service_wraps_worker_start_failure(monkeypatch) -> None:
    import voiceink_win.application.asr_service as module

    class FailingThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread start failed")

    monkeypatch.setattr(module, "Thread", FailingThread)
    runtime = FakeAsrRuntime()

    with pytest.raises(ExecutionError):
        AsrApplicationService(runtime)
