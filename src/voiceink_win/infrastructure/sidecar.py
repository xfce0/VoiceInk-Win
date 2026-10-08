"""NeMo-Speech.cpp sidecar adapter with injected process and transport ports."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from inspect import signature
from typing import Protocol
from urllib.parse import urlsplit

from voiceink_win.domain import (
    MAX_CANONICAL_AUDIO_BYTES,
    AsrCapabilities,
    AsrError,
    AsrRequest,
    AsrTimeoutError,
    CancellationError,
    CancellationToken,
    ConfigurationError,
    ExecutionError,
    HealthStatus,
    InvalidInputError,
    ProcessCrashedError,
    ProtocolError,
    RuntimeHealth,
    RuntimeRecoveryPendingError,
    RuntimeUnavailableError,
    TranscriptResult,
)

from .authentication import generate_nonce
from .sidecar_protocol import (
    PROTOCOL_VERSION,
    REQUEST_SCHEMA,
    decode_nemo_result,
    decode_result,
    map_error_response,
    map_nemo_error_response,
)
from .startup_diagnostics import StartupDiagnostics
from .transport import TransportResponse


class SidecarTransport(Protocol):
    def get(
        self,
        path: str,
        timeout: float | None,
        max_response_bytes: int,
        nonce: str | None = None,
    ) -> TransportResponse: ...

    def post(
        self, path: str, body: bytes, timeout: float | None, max_response_bytes: int
    ) -> TransportResponse: ...

    def post_audio(
        self,
        path: str,
        metadata: bytes,
        pcm: memoryview,
        timeout: float | None,
        max_response_bytes: int,
        cancellation: CancellationToken | None,
        deadline: float | None = None,
    ) -> TransportResponse: ...

    def clear_credentials(self) -> None: ...

    def close(self) -> None: ...


class ProcessSupervisor(Protocol):
    diagnostics: StartupDiagnostics
    nonce: str | None
    api_key: str | None

    def start(self) -> None: ...

    def wait_ready(self, deadline: float) -> bool: ...

    def is_running(self) -> bool: ...

    def terminate(self, deadline: float) -> None: ...

    def kill(self, deadline: float | None = None) -> None: ...

    def clear_credentials(self) -> None: ...


class MonotonicClock(Protocol):
    def monotonic(self) -> float: ...


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()


def _validate_loopback_endpoint(endpoint: str) -> None:
    if not isinstance(endpoint, str):
        raise ConfigurationError("sidecar endpoint must be an HTTP loopback URL")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ConfigurationError("sidecar endpoint must be an HTTP loopback URL")


@dataclass(frozen=True, slots=True)
class SidecarConfig:
    endpoint: str
    model_id: str = "parakeet-tdt-0.6b-v3.oss-align.q8_0"
    backend: str = "cpu"
    readiness_timeout: float = 10.0
    shutdown_timeout: float = 5.0
    transcribe_path: str = "/v1/audio/transcriptions"
    max_audio_bytes: int = MAX_CANONICAL_AUDIO_BYTES
    max_response_bytes: int = 4 * 1024 * 1024
    default_deadline_seconds: float = 30.0
    require_model_attestation: bool = False

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
        clock: MonotonicClock | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._supervisor = supervisor
        self._clock = clock or _SystemClock()
        self._diagnostics = supervisor.diagnostics
        set_transport_clock = getattr(transport, "set_clock", None)
        if set_transport_clock is not None:
            set_transport_clock(self._clock)
        self._started = False
        self._closed = False
        self._cleanup_pending = False
        self._credentials_clear_pending = False
        self._last_failure: str | None = None

    @property
    def diagnostics(self) -> StartupDiagnostics:
        return self._diagnostics

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
        if self._cleanup_pending:
            raise RuntimeRecoveryPendingError("sidecar cleanup is still pending")
        self._diagnostics.reset()
        startup_error: AsrError | None = None
        startup_phase = "process_create_suspended"
        operation = "create_process"
        try:
            self._supervisor.start()
            self._sync_transport_credentials()
            startup_phase = "readiness_probe"
            operation = "probe_readiness"
            readiness_deadline = self._clock.monotonic() + self._config.readiness_timeout
            ready = self._supervisor.wait_ready(readiness_deadline)
            if not ready:
                raise RuntimeUnavailableError("sidecar did not become ready")
            self._diagnostics.record("readiness_probe", "probe_readiness", "ready")
            if not self._supervisor.is_running():
                raise ProcessCrashedError("sidecar exited during readiness")
            if self._config.require_model_attestation:
                startup_phase = "attestation_passthrough"
                operation = "observe_attestation"
                self._attest_configured_model(readiness_deadline)
                self._diagnostics.record(
                    "attestation_passthrough", "observe_attestation", "observed"
                )
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
                if self._diagnostics.primary_failure is None:
                    self._diagnostics.record_failure(startup_phase, operation, startup_error)
                self._started = False
                self._last_failure = "sidecar startup failed"
                self._cleanup_pending = True
                cleanup_deadline = self._clock.monotonic() + self._config.shutdown_timeout
                cleanup_error = self._kill_after_start_failure(cleanup_deadline)
                if cleanup_error is not None:
                    try:
                        cleanup_pending = not self._supervisor_cleanup_complete()
                    except BaseException as error:
                        cleanup_pending = True
                        self._diagnostics.record_cleanup_failure("get_process_state", error)
                    self._diagnostics.set_cleanup_outcome(
                        "pending" if cleanup_pending else "failed"
                    )
                    self._record_cleanup_failure(cleanup_error)
                    self._cleanup_pending = cleanup_pending
                else:
                    self._diagnostics.set_cleanup_outcome("complete")
                    self._cleanup_pending = False
        if startup_error is not None:
            raise startup_error
        self._last_failure = None
        self._cleanup_pending = False
        self._started = True

    def _attest_configured_model(self, deadline: float) -> None:
        api_key = getattr(self._supervisor, "api_key", None)
        if not isinstance(api_key, str) or not api_key:
            raise ConfigurationError("sidecar model attestation credentials are unavailable")
        if not self._supervisor.is_running():
            raise ProcessCrashedError("sidecar exited before model attestation")
        get = getattr(self._transport, "get", None)
        if get is None:
            raise ConfigurationError("sidecar transport does not support model attestation")
        timeout = deadline - self._clock.monotonic()
        if timeout <= 0:
            raise AsrTimeoutError("sidecar model attestation timed out")
        response = get(
            "/v1/models",
            timeout,
            self._config.max_response_bytes,
        )
        _validate_model_attestation(response, self._config.model_id)
        if not self._supervisor.is_running():
            raise ProcessCrashedError("sidecar exited during model attestation")

    def transcribe(self, request: AsrRequest) -> TranscriptResult:
        if self._closed or not self._started:
            raise RuntimeUnavailableError("sidecar is not ready")
        if request.audio.byte_length > self._config.max_audio_bytes:
            raise InvalidInputError("audio exceeds the sidecar request byte limit")
        deadline = (
            request.deadline
            if request.deadline is not None
            else self._clock.monotonic() + self._config.default_deadline_seconds
        )
        timeout = deadline - self._clock.monotonic()
        if timeout <= 0:
            raise AsrTimeoutError("ASR request deadline exceeded")
        if request.cancellation is not None and request.cancellation.is_cancelled():
            raise CancellationError("ASR request was cancelled")
        try:
            if not self._supervisor.is_running():
                self._restart_after_crash(deadline)
                raise ProcessCrashedError("sidecar process is not running")
        except AsrError:
            raise
        except Exception as error:
            raise ExecutionError("sidecar process health check failed", cause=error) from error
        metadata = json.dumps(
            {
                "request_id": request.request_id,
                "protocol_version": PROTOCOL_VERSION,
                "schema": REQUEST_SCHEMA,
                "sample_rate": request.audio.sample_rate,
                "language": request.language,
                "timestamps": request.include_timestamps,
                "backend": self._config.backend,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            post_multipart_audio = getattr(self._transport, "post_multipart_audio", None)
            if post_multipart_audio is not None:
                response = post_multipart_audio(
                    self._config.transcribe_path,
                    memoryview(request.audio.pcm16le),
                    request.audio.sample_rate,
                    self._config.model_id,
                    request.language,
                    "verbose_json" if request.include_timestamps else "json",
                    timeout,
                    self._config.max_response_bytes,
                    request.cancellation,
                    deadline=deadline,
                )
            else:
                response = self._transport.post_audio(
                    self._config.transcribe_path,
                    metadata,
                    memoryview(request.audio.pcm16le),
                    timeout,
                    self._config.max_response_bytes,
                    request.cancellation,
                    deadline=deadline,
                )
        except AsrError:
            raise
        except TimeoutError as error:
            self._restart_after_crash(deadline)
            raise AsrTimeoutError("sidecar request timed out", cause=error) from error
        except (ConnectionError, OSError) as error:
            if not self._supervisor.is_running():
                self._restart_after_crash(deadline)
                raise ProcessCrashedError("sidecar process crashed", cause=error) from error
            raise RuntimeUnavailableError(
                "sidecar transport is unavailable", cause=error
            ) from error
        except Exception as error:
            raise ExecutionError("sidecar transport failed", cause=error) from error
        self._check_request_lifecycle(request, deadline)
        if len(response.body) > self._config.max_response_bytes:
            raise ProtocolError("sidecar response exceeds the configured byte limit")
        if response.status_code != 200:
            error = (
                map_nemo_error_response(response)
                if post_multipart_audio is not None
                else map_error_response(response)
            )
            if isinstance(error, ProcessCrashedError):
                self._restart_after_crash(deadline)
            raise error
        result = (
            decode_nemo_result(response.body, request.audio.duration)
            if post_multipart_audio is not None
            else decode_result(response.body)
        )
        self._check_request_lifecycle(request, deadline)
        return result

    def close(self, deadline: float | None = None) -> None:
        if self._closed:
            self._clear_credentials()
            return
        failures: list[BaseException] = []
        cleanup_complete = True
        if self._started or self._cleanup_pending:
            deadline = (
                deadline
                if deadline is not None
                else self._clock.monotonic() + self._config.shutdown_timeout
            )
            try:
                self._supervisor.terminate(deadline)
            except Exception as error:
                failures.append(error)
                self._diagnostics.record_cleanup_failure("terminate_process", error)
            try:
                running = self._supervisor.is_running()
            except Exception as error:
                failures.append(error)
                running = True
            if running:
                try:
                    self._kill_supervisor(deadline)
                except Exception as error:
                    failures.append(error)
                    self._diagnostics.record_cleanup_failure("terminate_process", error)
            try:
                cleanup_complete = not self._supervisor.is_running()
            except Exception as error:
                failures.append(error)
                cleanup_complete = False
            if cleanup_complete:
                try:
                    cleanup_complete = not self._reaper_owns_process()
                    cleanup_complete = cleanup_complete and self._supervisor_cleanup_complete()
                except Exception as error:
                    failures.append(error)
                    cleanup_complete = False
            if cleanup_complete:
                self._cleanup_pending = False
                self._diagnostics.set_cleanup_outcome("failed" if failures else "complete")
            else:
                self._cleanup_pending = True
                failures.append(RuntimeRecoveryPendingError("sidecar cleanup is still pending"))
                self._diagnostics.set_cleanup_outcome("pending")
        try:
            self._transport.close()
        except Exception as error:
            failures.append(error)
            self._diagnostics.record_cleanup_failure("close_resource", error)
        try:
            self._clear_credentials()
        except BaseException as error:
            failures.append(error)
            self._record_cleanup_failure(error)
        cleanup_complete = cleanup_complete and not self._credentials_clear_pending
        self._closed = cleanup_complete and not failures
        if failures:
            pending = next(
                (error for error in failures if isinstance(error, RuntimeRecoveryPendingError)),
                None,
            )
            if pending is not None:
                raise pending
            raise ExecutionError("sidecar cleanup failed", cause=failures[0]) from failures[0]

    def _clear_credentials(self) -> None:
        errors: list[BaseException] = []
        for component in (self._supervisor, self._transport):
            clear_credentials = getattr(component, "clear_credentials", None)
            if clear_credentials is None:
                continue
            try:
                clear_credentials()
            except BaseException as error:
                errors.append(error)
        self._credentials_clear_pending = bool(errors)
        if errors:
            raise ExceptionGroup("sidecar credential cleanup failed", errors)

    def _kill_after_start_failure(self, deadline: float) -> BaseException | None:
        """Roll back a process started by a failed readiness transaction."""
        return self._cleanup_started_process(deadline)

    def _cleanup_started_process(self, deadline: float) -> BaseException | None:
        failures: list[BaseException] = []
        try:
            self._supervisor.terminate(deadline)
        except BaseException as error:
            failures.append(error)
            self._diagnostics.record_cleanup_failure("terminate_process", error)
        try:
            running = self._supervisor.is_running()
        except BaseException as error:
            failures.append(error)
            self._diagnostics.record_cleanup_failure("get_process_state", error)
            running = True
        if running:
            try:
                self._kill_supervisor(deadline)
            except BaseException as error:
                failures.append(error)
                self._diagnostics.record_cleanup_failure("terminate_process", error)

        while self._clock.monotonic() < deadline:
            try:
                running = self._supervisor.is_running()
                cleanup_complete = self._supervisor_cleanup_complete()
            except BaseException as error:
                failures.append(error)
                running = True
                cleanup_complete = False
            if not running and cleanup_complete:
                break
            self._sleep(0.01)
        try:
            running = self._supervisor.is_running()
            cleanup_complete = self._supervisor_cleanup_complete()
        except BaseException as error:
            failures.append(error)
            self._diagnostics.record_cleanup_failure("get_process_state", error)
            running = True
            cleanup_complete = False
        if running:
            error = RuntimeError("sidecar process remained alive after startup cleanup")
            failures.append(error)
            self._diagnostics.record_cleanup_failure("wait_for_exit", error)
        if not cleanup_complete:
            error = RuntimeError("sidecar resource cleanup is still pending")
            failures.append(error)
            self._diagnostics.record_cleanup_failure("close_resource", error)
        try:
            self._clear_credentials()
        except BaseException as error:
            failures.append(error)
            self._record_cleanup_failure(error)
        cleanup_complete = cleanup_complete and not self._credentials_clear_pending
        if self._clock.monotonic() >= deadline:
            error = AsrTimeoutError("sidecar cleanup exceeded the request deadline")
            self._diagnostics.record_cleanup_failure("wait_for_exit", error)
            return error
        if failures:
            return ExceptionGroup("sidecar startup cleanup failed", failures)
        return None

    def _restart_after_crash(self, deadline: float) -> None:
        if self._clock.monotonic() >= deadline:
            raise AsrTimeoutError("sidecar restart exceeded the request deadline")
        old_process_error: BaseException | None = None
        running = True
        try:
            # Closing the transport invalidates the timed-out request before a new process
            # can accept the next attempt.
            try:
                self._transport.close()
            except BaseException as error:
                old_process_error = error
            try:
                self._supervisor.terminate(deadline)
            except BaseException as error:
                old_process_error = error
            finally:
                try:
                    running = self._supervisor.is_running()
                except BaseException as error:
                    running = True
                    if old_process_error is None:
                        old_process_error = error
                if running:
                    try:
                        self._kill_supervisor(deadline)
                    except BaseException as error:
                        if old_process_error is None:
                            old_process_error = error
                    try:
                        running = self._supervisor.is_running()
                    except BaseException as error:
                        running = True
                        if old_process_error is None:
                            old_process_error = error
            if not self._wait_for_supervisor_cleanup(deadline):
                raise AsrTimeoutError(
                    "sidecar cleanup exceeded the request deadline"
                ) from old_process_error
            if self._clock.monotonic() >= deadline:
                raise AsrTimeoutError("sidecar restart exceeded the request deadline")
            started = False
            try:
                self._supervisor.start()
                started = True
                self._sync_transport_credentials()
                if not self._supervisor.wait_ready(deadline):
                    raise RuntimeUnavailableError("sidecar did not become ready after a crash")
                if not self._supervisor.is_running():
                    raise ProcessCrashedError("sidecar exited during crash recovery")
                if self._config.require_model_attestation:
                    self._attest_configured_model(deadline)
            except BaseException as error:
                if started:
                    self._started = False
                    self._cleanup_pending = True
                    cleanup_error = self._cleanup_started_process(deadline)
                    if cleanup_error is not None:
                        expired = self._clock.monotonic() >= deadline
                        if isinstance(cleanup_error, AsrTimeoutError) or expired:
                            raise AsrTimeoutError(
                                "sidecar restart cleanup exceeded the request deadline",
                                cause=cleanup_error,
                            ) from error
                        raise ExecutionError(
                            "sidecar restart cleanup failed", cause=cleanup_error
                        ) from error
                    self._cleanup_pending = False
                raise
        except AsrError:
            raise
        except TimeoutError as error:
            raise AsrTimeoutError("sidecar restart timed out", cause=error) from error
        except OSError as error:
            raise RuntimeUnavailableError(
                "sidecar could not restart after a crash", cause=error
            ) from error
        except Exception as error:
            raise ExecutionError("sidecar crash recovery failed", cause=error) from error

    def _sync_transport_credentials(self) -> None:
        nonce = self._supervisor.nonce or generate_nonce()
        set_transport_nonce = getattr(self._transport, "set_nonce", None)
        if set_transport_nonce is not None:
            try:
                set_transport_nonce(nonce)
            except BaseException as error:
                failure = ConfigurationError("sidecar nonce credential setup failed", cause=error)
                self._diagnostics.record_failure("configuration", "set_nonce", failure)
                raise failure from error
        api_key = self._supervisor.api_key
        set_transport_api_key = getattr(self._transport, "set_api_key", None)
        if api_key is not None and set_transport_api_key is not None:
            try:
                set_transport_api_key(api_key)
            except BaseException as error:
                failure = ConfigurationError("sidecar API key credential setup failed", cause=error)
                self._diagnostics.record_failure("configuration", "set_api_key", failure)
                raise failure from error

    def _kill_supervisor(self, deadline: float) -> None:
        kill = self._supervisor.kill
        parameters = signature(kill).parameters
        if "deadline" in parameters or any(
            parameter.kind is parameter.VAR_KEYWORD for parameter in parameters.values()
        ):
            kill(deadline=deadline)
        else:
            kill()

    def _sleep(self, seconds: float) -> None:
        sleep = getattr(self._clock, "sleep", None)
        if sleep is None:
            time.sleep(seconds)
        else:
            sleep(seconds)

    def _record_cleanup_failure(self, error: BaseException) -> None:
        errors = error.exceptions if isinstance(error, BaseExceptionGroup) else (error,)
        for nested in errors:
            if isinstance(nested, BaseExceptionGroup):
                self._record_cleanup_failure(nested)
            else:
                self._diagnostics.record_cleanup_failure("close_resource", nested)

    def _wait_for_supervisor_cleanup(self, deadline: float) -> bool:
        while self._clock.monotonic() < deadline:
            try:
                if (
                    not self._supervisor.is_running()
                    and not self._reaper_owns_process()
                    and self._supervisor_cleanup_complete()
                ):
                    return True
            except BaseException:
                pass
            self._sleep(0.01)
        try:
            return (
                not self._supervisor.is_running()
                and not self._reaper_owns_process()
                and self._supervisor_cleanup_complete()
            )
        except BaseException:
            return False

    def _reaper_owns_process(self) -> bool:
        owns = getattr(self._supervisor, "reaper_owns_process", None)
        return bool(owns is not None and owns())

    def _supervisor_cleanup_complete(self) -> bool:
        complete = getattr(self._supervisor, "cleanup_complete", None)
        if complete is None:
            return not self._reaper_owns_process() and not self._supervisor.is_running()
        return bool(complete())

    def _check_request_lifecycle(self, request: AsrRequest, deadline: float) -> None:
        if request.cancellation is not None and request.cancellation.is_cancelled():
            raise CancellationError("ASR request was cancelled")
        if self._clock.monotonic() >= deadline:
            raise AsrTimeoutError("ASR request deadline exceeded")


def _validate_model_attestation(response: TransportResponse, expected_model_id: str) -> None:
    if response.status_code != 200:
        raise ConfigurationError("sidecar model attestation returned an unexpected status")
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError(
            "sidecar model attestation returned malformed JSON", cause=error
        ) from error
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ProtocolError("sidecar model attestation has an invalid model list")
    for model in payload["data"]:
        if isinstance(model, dict) and model.get("id") == expected_model_id:
            if _supports_transcription(model):
                return
            raise ConfigurationError("configured sidecar model lacks transcription capability")
    raise ConfigurationError("configured sidecar model was not returned by the runtime")


def _supports_transcription(model: dict[str, object]) -> bool:
    return model.get("capability") == "transcription"
