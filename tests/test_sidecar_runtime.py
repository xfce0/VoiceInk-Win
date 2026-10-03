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
    RuntimeUnavailableError,
)
from voiceink_win.infrastructure import NeMoSidecarRuntime, SidecarConfig, TransportResponse


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

    def close(self) -> None:
        self.closed = True


class FakeSupervisor:
    def __init__(self, *, ready: bool = True, running: bool = True, stubborn: bool = False) -> None:
        self.ready = ready
        self.running = running
        self.stubborn = stubborn
        self.fail_kill = False
        self.started = False
        self.terminated = False
        self.killed = False

    def start(self) -> None:
        self.started = True

    def wait_ready(self, deadline: float) -> bool:
        assert deadline > time.monotonic()
        return self.ready

    def is_running(self) -> bool:
        return self.running

    def terminate(self, deadline: float) -> None:
        assert deadline > time.monotonic()
        self.terminated = True
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
    ],
)
def test_sidecar_maps_protocol_codes_to_typed_errors(status: int, code: str, error_type) -> None:
    transport = FakeTransport(TransportResponse(status, error_body(code)))
    runtime = NeMoSidecarRuntime(config(), transport, FakeSupervisor())
    runtime.start()

    with pytest.raises(error_type):
        runtime.transcribe(request())


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
