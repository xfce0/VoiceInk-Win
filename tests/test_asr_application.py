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
    RuntimeUnavailableError,
    TranscriptResult,
)
from voiceink_win.infrastructure import FakeAsrRuntime, FakeAsrScenario


def request(*, deadline: float | None = None, cancellation=None) -> AsrRequest:
    from voiceink_win.domain import CanonicalAudio

    return AsrRequest(
        CanonicalAudio(b"\x00\x00" * 16_000),
        request_id="test-request",
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
