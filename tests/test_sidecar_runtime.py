from __future__ import annotations

import json
import time

import pytest

from voiceink_win.domain import (
    AsrRequest,
    AsrTimeoutError,
    BackendUnavailableError,
    CanonicalAudio,
    ConfigurationError,
    ExecutionError,
    InvalidInputError,
    MissingModelError,
    ProcessCrashedError,
    ProtocolError,
    RuntimeRecoveryPendingError,
    RuntimeUnavailableError,
)
from voiceink_win.infrastructure import (
    FakeClock,
    NeMoSidecarRuntime,
    SidecarConfig,
    TransportResponse,
)


class FakeTransport:
    def __init__(self, response: TransportResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, bytes, float | None]] = []
        self.closed = False

    def post(
        self, path: str, body: bytes, timeout: float | None, max_response_bytes: int
    ) -> TransportResponse:
        self.calls.append((path, body, timeout))
        return self.response

    def post_audio(
        self,
        path: str,
        metadata: bytes,
        pcm: memoryview,
        timeout: float | None,
        max_response_bytes: int,
        cancellation=None,
        deadline: float | None = None,
    ) -> TransportResponse:
        del cancellation, deadline
        payload = json.loads(metadata)
        payload["audio_pcm16le"] = bytes(pcm).hex()
        return self.post(path, json.dumps(payload).encode(), timeout, max_response_bytes)

    def close(self) -> None:
        self.closed = True


class FakeSupervisor:
    def __init__(self, *, ready: bool = True, running: bool = True, stubborn: bool = False) -> None:
        self.ready = ready
        self.running = running
        self.stubborn = stubborn
        self.fail_kill = False
        self.fail_terminate = False
        self.started = False
        self.terminated = False
        self.killed = False
        self.start_count = 0

    def start(self) -> None:
        self.started = True
        self.start_count += 1
        self.running = True

    def wait_ready(self, deadline: float) -> bool:
        assert deadline > time.monotonic()
        return self.ready

    def is_running(self) -> bool:
        return self.running

    def terminate(self, deadline: float) -> None:
        assert deadline > time.monotonic()
        self.terminated = True
        if self.fail_terminate:
            raise OSError("simulated terminate failure")
        if not self.stubborn:
            self.running = False

    def kill(self) -> None:
        if self.fail_kill:
            raise OSError("secret executable path must not leak")
        self.killed = True
        self.running = False


def config() -> SidecarConfig:
    return SidecarConfig("http://127.0.0.1:8123", backend="cuda:0")


def request() -> AsrRequest:
    return AsrRequest(CanonicalAudio(b"\x00\x00" * 16), request_id="sidecar-test")


def result_body() -> bytes:
    return json.dumps(
        {
            "protocol_version": 1,
            "schema": "voiceink.asr.result.v1",
            "text": "hello",
            "duration": 0.001,
            "detected_language": "en",
            "segments": [{"text": "hello", "start": 0.0, "end": 0.001}],
        }
    ).encode()


def error_body(code: str) -> bytes:
    return json.dumps({"protocol_version": 1, "code": code}).encode()


def test_sidecar_start_transcribe_and_close_use_injected_boundaries() -> None:
    transport = FakeTransport(TransportResponse(200, result_body()))
    supervisor = FakeSupervisor()
    runtime = NeMoSidecarRuntime(config(), transport, supervisor)

    runtime.start()
    result = runtime.transcribe(request())
    runtime.close()

    assert result.text == "hello"
    assert transport.calls[0][0] == "/transcribe"
    payload = json.loads(transport.calls[0][1])
    assert payload["backend"] == "cuda:0"
    assert payload["protocol_version"] == 1
    assert payload["schema"] == "voiceink.asr.request.v1"
    assert supervisor.started and supervisor.terminated
    assert transport.closed


@pytest.mark.parametrize(
    ("status", "code", "error_type"),
    [
        (400, "invalid_input", InvalidInputError),
        (404, "missing_model", MissingModelError),
        (408, "timeout", AsrTimeoutError),
        (503, "backend_unavailable", BackendUnavailableError),
        (500, "execution", ExecutionError),
        (500, "process_crashed", ProcessCrashedError),
    ],
)
def test_sidecar_maps_protocol_codes_to_typed_errors(status: int, code: str, error_type) -> None:
    transport = FakeTransport(TransportResponse(status, error_body(code)))
    runtime = NeMoSidecarRuntime(config(), transport, FakeSupervisor())
    runtime.start()

    with pytest.raises(error_type):
        runtime.transcribe(request())


def test_process_crashed_error_is_retryable() -> None:
    assert ProcessCrashedError.retryable


def test_sidecar_does_not_infer_missing_model_or_backend_from_http_status() -> None:
    for status in (404, 503):
        runtime = NeMoSidecarRuntime(
            config(),
            FakeTransport(TransportResponse(status, error_body("unknown"))),
            FakeSupervisor(),
        )
        runtime.start()

        with pytest.raises(ProtocolError):
            runtime.transcribe(request())


def test_sidecar_rejects_malformed_success_payload() -> None:
    runtime = NeMoSidecarRuntime(
        config(), FakeTransport(TransportResponse(200, b"not-json")), FakeSupervisor()
    )
    runtime.start()

    with pytest.raises(ProtocolError):
        runtime.transcribe(request())


def test_sidecar_maps_semantically_malformed_success_payload_to_protocol_error() -> None:
    body = json.dumps({"text": "hello", "duration": -1, "segments": []}).encode()
    runtime = NeMoSidecarRuntime(
        config(), FakeTransport(TransportResponse(200, body)), FakeSupervisor()
    )
    runtime.start()

    with pytest.raises(ProtocolError):
        runtime.transcribe(request())


def test_sidecar_rejects_response_larger_than_configured_limit() -> None:
    runtime = NeMoSidecarRuntime(
        SidecarConfig("http://127.0.0.1:8123", max_response_bytes=8),
        FakeTransport(TransportResponse(200, b"123456789")),
        FakeSupervisor(),
    )
    runtime.start()

    with pytest.raises(ProtocolError):
        runtime.transcribe(request())


def test_sidecar_rejects_audio_larger_than_request_limit() -> None:
    runtime = NeMoSidecarRuntime(
        SidecarConfig("http://127.0.0.1:8123", max_audio_bytes=2),
        FakeTransport(TransportResponse(200, result_body())),
        FakeSupervisor(),
    )
    runtime.start()

    with pytest.raises(InvalidInputError):
        runtime.transcribe(request())


def test_sidecar_reports_process_crash_and_kills_unready_process() -> None:
    crashed_supervisor = FakeSupervisor()
    crashed = NeMoSidecarRuntime(
        config(), FakeTransport(TransportResponse(200, result_body())), crashed_supervisor
    )
    crashed.start()
    crashed_supervisor.running = False

    with pytest.raises(ProcessCrashedError):
        crashed.transcribe(request())

    unready_supervisor = FakeSupervisor(ready=False)
    unready = NeMoSidecarRuntime(
        config(), FakeTransport(TransportResponse(200, result_body())), unready_supervisor
    )
    with pytest.raises(RuntimeUnavailableError):
        unready.start()
    assert unready_supervisor.killed


def test_sidecar_restarts_after_crash_for_the_retry_attempt() -> None:
    class SequencedTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__(TransportResponse(500, error_body("process_crashed")))
            self.responses = [self.response, TransportResponse(200, result_body())]

        def post_audio(self, *args, **kwargs) -> TransportResponse:
            del args, kwargs
            return self.responses.pop(0)

    supervisor = FakeSupervisor()
    transport = SequencedTransport()
    runtime = NeMoSidecarRuntime(config(), transport, supervisor)
    runtime.start()

    with pytest.raises(ProcessCrashedError):
        runtime.transcribe(request())
    assert supervisor.start_count == 2
    assert supervisor.terminated
    assert supervisor.running

    assert runtime.transcribe(request()).text == "hello"


def test_sidecar_forces_old_process_down_after_terminate_failure_before_restart() -> None:
    supervisor = FakeSupervisor()
    supervisor.fail_terminate = True
    runtime = NeMoSidecarRuntime(
        config(),
        FakeTransport(TransportResponse(500, error_body("process_crashed"))),
        supervisor,
    )
    runtime.start()

    with pytest.raises(ProcessCrashedError):
        runtime.transcribe(request())

    assert supervisor.killed
    assert supervisor.start_count == 2
    assert supervisor.running


def test_sidecar_failed_restart_rolls_back_the_new_process_transactionally() -> None:
    class FailsReadinessOnRestart(FakeSupervisor):
        def start(self) -> None:
            super().start()
            if self.start_count == 2:
                self.ready = False

    supervisor = FailsReadinessOnRestart()
    runtime = NeMoSidecarRuntime(
        config(),
        FakeTransport(TransportResponse(500, error_body("process_crashed"))),
        supervisor,
    )
    runtime.start()

    with pytest.raises(RuntimeUnavailableError):
        runtime.transcribe(request())

    assert supervisor.start_count == 2
    assert supervisor.terminated
    assert supervisor.killed
    assert not supervisor.running
    assert not runtime._started


def test_sidecar_failed_restart_uses_request_deadline_and_injected_clock() -> None:
    class FailsReadinessAndCleanup(FakeSupervisor):
        def __init__(self) -> None:
            super().__init__()
            self.terminate_deadlines: list[float] = []
            self.kill_deadlines: list[float | None] = []
            self.cleanup_pending = False

        def start(self) -> None:
            super().start()
            if self.start_count == 2:
                self.ready = False
                self.cleanup_pending = True

        def wait_ready(self, deadline: float) -> bool:
            return self.start_count == 1 and deadline > 100.0

        def terminate(self, deadline: float) -> None:
            self.terminate_deadlines.append(deadline)
            self.terminated = True
            self.running = False

        def kill(self, deadline: float | None = None) -> None:
            self.kill_deadlines.append(deadline)
            self.killed = True
            self.running = False

        def cleanup_complete(self) -> bool:
            return not self.cleanup_pending

    clock = FakeClock(100.0)
    supervisor = FailsReadinessAndCleanup()
    runtime = NeMoSidecarRuntime(
        config(),
        FakeTransport(TransportResponse(500, error_body("process_crashed"))),
        supervisor,
        clock=clock,
    )
    runtime.start()
    request_deadline = clock.monotonic() + 0.05
    request_with_deadline = AsrRequest(
        CanonicalAudio(b"\x00\x00" * 16),
        request_id="sidecar-deadline-test",
        deadline=request_deadline,
    )

    with pytest.raises(AsrTimeoutError, match="deadline"):
        runtime.transcribe(request_with_deadline)

    assert supervisor.start_count == 2
    assert supervisor.terminate_deadlines
    assert all(deadline == request_deadline for deadline in supervisor.terminate_deadlines)
    assert supervisor.kill_deadlines
    assert all(deadline == request_deadline for deadline in supervisor.kill_deadlines)
    assert clock.monotonic() >= request_deadline
    assert not runtime._started


def test_sidecar_timeout_closes_and_restarts_before_a_retry_attempt() -> None:
    class TimeoutThenSuccessTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__(TransportResponse(200, result_body()))
            self.attempts = 0

        def post_audio(self, *args, **kwargs) -> TransportResponse:
            self.attempts += 1
            if self.attempts == 1:
                raise TimeoutError("simulated request timeout")
            return super().post_audio(*args, **kwargs)

    supervisor = FakeSupervisor()
    transport = TimeoutThenSuccessTransport()
    runtime = NeMoSidecarRuntime(config(), transport, supervisor)
    runtime.start()

    with pytest.raises(AsrTimeoutError):
        runtime.transcribe(request())

    assert transport.closed
    assert supervisor.start_count == 2
    assert supervisor.running
    assert runtime.transcribe(request()).text == "hello"


def test_sidecar_startup_cleanup_failure_is_typed_and_health_is_sanitized() -> None:
    supervisor = FakeSupervisor(ready=False)
    supervisor.fail_kill = True
    runtime = NeMoSidecarRuntime(
        config(), FakeTransport(TransportResponse(200, result_body())), supervisor
    )

    with pytest.raises(ExecutionError):
        runtime.start()

    health = runtime.health()
    assert health.message == "sidecar startup cleanup failed"
    assert "secret" not in health.message


def test_sidecar_rejects_non_loopback_endpoint() -> None:
    with pytest.raises(ConfigurationError):
        SidecarConfig("https://example.invalid/runtime")


def test_sidecar_kills_stubborn_process_during_close() -> None:
    supervisor = FakeSupervisor(stubborn=True)
    runtime = NeMoSidecarRuntime(
        config(), FakeTransport(TransportResponse(200, result_body())), supervisor
    )
    runtime.start()

    runtime.close()

    assert supervisor.terminated
    assert supervisor.killed


def test_sidecar_preserves_pending_cleanup_for_a_later_close_retry() -> None:
    class PendingCleanupSupervisor(FakeSupervisor):
        def __init__(self) -> None:
            super().__init__()
            self.cleanup_ready = False

        def cleanup_complete(self) -> bool:
            return self.cleanup_ready

    supervisor = PendingCleanupSupervisor()
    runtime = NeMoSidecarRuntime(
        config(), FakeTransport(TransportResponse(200, result_body())), supervisor
    )
    runtime.start()

    with pytest.raises(RuntimeRecoveryPendingError):
        runtime.close(deadline=time.monotonic() + 1.0)
    assert not runtime._closed

    supervisor.cleanup_ready = True
    runtime.close(deadline=time.monotonic() + 1.0)

    assert runtime._closed
