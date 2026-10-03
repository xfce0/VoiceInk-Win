"""NeMo-Speech.cpp sidecar adapter with injected process and transport ports."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from voiceink_win.domain import (
    MAX_CANONICAL_AUDIO_BYTES,
    AsrCapabilities,
    AsrError,
    AsrRequest,
    AsrTimeoutError,
    CancellationError,
    ConfigurationError,
    ExecutionError,
    HealthStatus,
    InvalidInputError,
    ProcessCrashedError,
    ProtocolError,
    RuntimeHealth,
    RuntimeUnavailableError,
    TranscriptResult,
)

from .sidecar_protocol import (
    PROTOCOL_VERSION,
    REQUEST_SCHEMA,
    decode_result,
    map_error_response,
)
from .transport import TransportResponse


class SidecarTransport(Protocol):
    def post(
        self, path: str, body: bytes, timeout: float | None, max_response_bytes: int
    ) -> TransportResponse: ...

    def close(self) -> None: ...


class ProcessSupervisor(Protocol):
    def start(self) -> None: ...

    def wait_ready(self, deadline: float) -> bool: ...

    def is_running(self) -> bool: ...

    def terminate(self, deadline: float) -> None: ...

    def kill(self) -> None: ...


def _validate_loopback_endpoint(endpoint: str) -> None:
    if not isinstance(endpoint, str):
        raise ConfigurationError("sidecar endpoint must be an HTTP loopback URL")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ConfigurationError("sidecar endpoint must be an HTTP loopback URL")


@dataclass(frozen=True, slots=True)
class SidecarConfig:
    endpoint: str
    model_id: str = "parakeet-tdt-v3"
    backend: str = "cpu"
    readiness_timeout: float = 10.0
    shutdown_timeout: float = 5.0
    transcribe_path: str = "/transcribe"
    max_audio_bytes: int = MAX_CANONICAL_AUDIO_BYTES
    max_response_bytes: int = 4 * 1024 * 1024
    default_deadline_seconds: float = 30.0

    def __post_init__(self) -> None:
        _validate_loopback_endpoint(self.endpoint)
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ConfigurationError("sidecar model_id must not be empty")
        if self.backend != "cpu":
            prefix, _, index = self.backend.partition(":")
            if prefix != "cuda" or not index.isdigit():
                raise ConfigurationError("sidecar backend must be cpu or cuda:<index>")
        if (
            not math.isfinite(self.readiness_timeout)
            or not math.isfinite(self.shutdown_timeout)
            or self.readiness_timeout <= 0
            or self.shutdown_timeout <= 0
        ):
            raise ConfigurationError("sidecar lifecycle timeouts must be positive")
        if not 0 < self.max_audio_bytes <= MAX_CANONICAL_AUDIO_BYTES:
            raise ConfigurationError("sidecar max_audio_bytes is outside the canonical limit")
        if (
            self.max_response_bytes < 1
            or not math.isfinite(self.default_deadline_seconds)
            or self.default_deadline_seconds <= 0
        ):
            raise ConfigurationError("sidecar request limits must be positive")


class NeMoSidecarRuntime:
    def __init__(
        self,
        config: SidecarConfig,
        transport: SidecarTransport,
        supervisor: ProcessSupervisor,
    ) -> None:
        self._config = config
        self._transport = transport
        self._supervisor = supervisor
        self._started = False
        self._closed = False
        self._last_failure: str | None = None

    def capabilities(self) -> AsrCapabilities:
        return AsrCapabilities(
            model_id=self._config.model_id,
            backends=(self._config.backend,),
            supports_timestamps=True,
        )

    def health(self) -> RuntimeHealth:
        if self._closed:
            return RuntimeHealth(HealthStatus.CLOSED, "sidecar is closed", self._config.backend)
        if not self._started:
            message = self._last_failure or "sidecar has not been started"
            return RuntimeHealth(
                HealthStatus.FAILED if self._last_failure else HealthStatus.STARTING,
                message,
                self._config.backend,
            )
        try:
            running = self._supervisor.is_running()
        except Exception:
            return RuntimeHealth(
                HealthStatus.FAILED, "sidecar health check failed", self._config.backend
            )
        if not running:
            return RuntimeHealth(
                HealthStatus.FAILED, "sidecar process is not running", self._config.backend
            )
        return RuntimeHealth(HealthStatus.READY, "sidecar ready", self._config.backend)

    def start(self) -> None:
        if self._closed:
            raise RuntimeUnavailableError("sidecar is closed")
        if self._started:
            return
        startup_error: AsrError | None = None
        try:
            self._supervisor.start()
            ready = self._supervisor.wait_ready(time.monotonic() + self._config.readiness_timeout)
            if not ready:
                raise RuntimeUnavailableError("sidecar did not become ready")
            if not self._supervisor.is_running():
                raise ProcessCrashedError("sidecar exited during readiness")
        except AsrError as error:
            startup_error = error
        except TimeoutError as error:
            startup_error = AsrTimeoutError("sidecar readiness timed out", cause=error)
        except OSError as error:
            startup_error = ConfigurationError("sidecar process could not be started", cause=error)
        except Exception as error:
            startup_error = ExecutionError("sidecar readiness failed", cause=error)
        finally:
            if startup_error is not None:
                self._last_failure = "sidecar startup failed"
                cleanup_error = self._kill_after_start_failure()
                if cleanup_error is not None:
                    self._last_failure = "sidecar startup cleanup failed"
                    raise ExecutionError(
                        "sidecar startup cleanup failed", cause=cleanup_error
                    ) from startup_error
        if startup_error is not None:
            raise startup_error
        self._last_failure = None
        self._started = True

    def transcribe(self, request: AsrRequest) -> TranscriptResult:
        if self._closed or not self._started:
            raise RuntimeUnavailableError("sidecar is not ready")
        if request.audio.byte_length > self._config.max_audio_bytes:
            raise InvalidInputError("audio exceeds the sidecar request byte limit")
        try:
            if not self._supervisor.is_running():
                raise ProcessCrashedError("sidecar process is not running")
        except ProcessCrashedError:
            raise
        except Exception as error:
            raise ExecutionError("sidecar process health check failed", cause=error) from error
        if request.cancellation is not None and request.cancellation.is_cancelled():
            raise CancellationError("ASR request was cancelled")
        timeout = None
        deadline = (
            request.deadline
            if request.deadline is not None
            else time.monotonic() + self._config.default_deadline_seconds
        )
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise AsrTimeoutError("ASR request deadline exceeded")
        payload = json.dumps(
            {
                "request_id": request.request_id,
                "protocol_version": PROTOCOL_VERSION,
                "schema": REQUEST_SCHEMA,
                "audio_pcm16le": request.audio.pcm16le.hex(),
                "sample_rate": request.audio.sample_rate,
                "language": request.language,
                "timestamps": request.include_timestamps,
                "backend": self._config.backend,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            response = self._transport.post(
                self._config.transcribe_path,
                payload,
                timeout,
                self._config.max_response_bytes,
            )
        except AsrError:
            raise
        except TimeoutError as error:
            raise AsrTimeoutError("sidecar request timed out", cause=error) from error
        except (ConnectionError, OSError) as error:
            raise RuntimeUnavailableError(
                "sidecar transport is unavailable", cause=error
            ) from error
        except Exception as error:
            raise ExecutionError("sidecar transport failed", cause=error) from error
        self._check_request_lifecycle(request, deadline)
        if len(response.body) > self._config.max_response_bytes:
            raise ProtocolError("sidecar response exceeds the configured byte limit")
        if response.status_code != 200:
            raise map_error_response(response)
        return decode_result(response.body)

    def close(self) -> None:
        if self._closed:
            return
        failures: list[BaseException] = []
        if self._started:
            deadline = time.monotonic() + self._config.shutdown_timeout
            try:
                self._supervisor.terminate(deadline)
            except Exception as error:
                failures.append(error)
            try:
                running = self._supervisor.is_running()
            except Exception as error:
                failures.append(error)
                running = True
            if running:
                try:
                    self._supervisor.kill()
                except Exception as error:
                    failures.append(error)
        try:
            self._transport.close()
        except Exception as error:
            failures.append(error)
        self._closed = True
        if failures:
            raise ExecutionError("sidecar cleanup failed", cause=failures[0]) from failures[0]

    def _kill_after_start_failure(self) -> BaseException | None:
        try:
            self._supervisor.kill()
        except Exception as error:
            return error
        return None

    def _check_request_lifecycle(self, request: AsrRequest, deadline: float) -> None:
        if request.cancellation is not None and request.cancellation.is_cancelled():
            raise CancellationError("ASR request was cancelled")
        if time.monotonic() >= deadline:
            raise AsrTimeoutError("ASR request deadline exceeded")
