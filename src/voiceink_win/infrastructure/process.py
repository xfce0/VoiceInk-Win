"""Stdlib process and artifact boundary for the local ASR sidecar."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from voiceink_win.domain import (
    ConfigurationError,
    MissingModelError,
    RuntimeRecoveryPendingError,
    RuntimeUnavailableError,
)

from .authentication import ASR_API_KEY_ENV, ASR_NONCE_ENV, ASR_NONCE_HEADER, generate_nonce
from .startup_diagnostics import StartupDiagnostics


class ProcessHandle(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class ReadinessProbe(Protocol):
    def ready(self, timeout: float) -> bool: ...

    def set_nonce(self, nonce: str) -> None: ...

    def set_api_key(self, api_key: str) -> None: ...

    def clear_credentials(self) -> None: ...


class MonotonicClock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class _ProcessLifecycleState(StrEnum):
    NOT_CREATED = "not_created"
    CREATED_SUSPENDED = "created_suspended"
    RUNNING = "running"
    STOP_REQUESTED = "stop_requested"
    TERMINATION_FAILED = "termination_failed"
    EXITED = "exited"
    REAPED = "reaped"


def _run_process_reaper(
    process: ProcessHandle,
    on_done: Callable[[], None] | None = None,
    done: Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Thread:
    """Keep ownership of a process whose wait outlived its cleanup deadline."""

    def reap() -> None:
        while True:
            try:
                if process.poll() is not None:
                    break
                process.wait()
            except BaseException:
                sleep(0.05)
        if done is not None:
            done.set()
        if on_done is not None:
            on_done()

    reaper = Thread(target=reap, name="process-cleanup-reaper", daemon=True)
    reaper.start()
    return reaper


@dataclass(slots=True)
class _ProcessReaperGeneration:
    number: int
    process: ProcessHandle
    done: Event = field(default_factory=Event)
    thread: Thread | None = None
    pending_job: object | None = None
    observed_job: object | None = None


@dataclass(frozen=True, slots=True)
class RuntimeArtifactManifest:
    version: str
    provenance_url: str
    sha256: str
    license: str
    allowed_path: Path

    def __post_init__(self) -> None:
        if (
            not self.version.strip()
            or not self.provenance_url.startswith("https://")
            or len(self.sha256) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in self.sha256)
            or not self.license.strip()
            or not self.allowed_path.is_absolute()
        ):
            raise ConfigurationError("runtime artifact manifest is incomplete")


def _validate_loopback_endpoint(endpoint: str) -> tuple[str, int]:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError("sidecar endpoint must be an HTTP loopback URL")
    if parsed.port is None or not 1 <= parsed.port <= 65535:
        raise ConfigurationError("sidecar endpoint must include a valid port")
    return parsed.hostname, parsed.port


class RuntimeArtifactVerifier:
    """Verify immutable runtime and model artifacts before process startup."""

    def sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        descriptor = self._open_regular(path)
        try:
            before = os.fstat(descriptor)
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
            if self._identity(before) != self._identity(after) or before.st_size != after.st_size:
                raise ConfigurationError("runtime artifact changed while it was read")
        except OSError as error:
            raise ConfigurationError("runtime artifact cannot be read", cause=error) from error
        finally:
            os.close(descriptor)
        return digest.hexdigest()

    def verify(self, path: Path, expected_sha256: str, *, label: str = "runtime artifact") -> None:
        if len(expected_sha256) != 64 or any(
            char not in "0123456789abcdefABCDEF" for char in expected_sha256
        ):
            raise ConfigurationError(f"{label} checksum is invalid")
        self._validate_path(path, label)
        actual = self.sha256(path)
        if actual.lower() != expected_sha256.lower():
            raise ConfigurationError(f"{label} checksum does not match")

    def verify_manifest(self, path: Path, manifest: RuntimeArtifactManifest, *, label: str) -> None:
        self._validate_path(path, label)
        allowed = self._canonical_path(manifest.allowed_path, label)
        actual = self._canonical_path(path, label)
        if actual != allowed:
            raise ConfigurationError(f"{label} is outside the approved path")
        self.verify(path, manifest.sha256, label=label)

    def identity(self, path: Path) -> tuple[int, int, int]:
        self._validate_path(path, "runtime artifact")
        info = os.stat(path, follow_symlinks=False)
        return self._identity(info)

    def verify_executable(self, path: Path, expected_sha256: str) -> None:
        self.verify(path, expected_sha256, label="runtime executable")
        if os.name != "nt" and not os.access(path, os.X_OK):
            raise ConfigurationError("runtime executable is not executable")

    def verify_model(self, path: Path, expected_sha256: str) -> None:
        try:
            self.verify(path, expected_sha256, label="runtime model")
        except ConfigurationError as error:
            if not path.exists():
                raise MissingModelError("runtime model is missing") from error
            raise ConfigurationError("runtime model verification failed", cause=error) from error

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, int, int]:
        return info.st_dev, info.st_ino, info.st_size

    @staticmethod
    def _canonical_path(path: Path, label: str) -> Path:
        if not path.is_absolute():
            raise ConfigurationError(f"{label} must be an absolute path")
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            try:
                info = os.lstat(current)
            except OSError as error:
                raise ConfigurationError(f"{label} is missing", cause=error) from error
            if stat.S_ISLNK(info.st_mode):
                raise ConfigurationError(f"{label} cannot contain a symbolic link")
            if os.name == "nt":
                attributes = ctypes.windll.kernel32.GetFileAttributesW(str(current))
                if attributes == 0xFFFFFFFF or attributes & 0x400:
                    raise ConfigurationError(f"{label} cannot contain a reparse point")
        return path.absolute()

    def _validate_path(self, path: Path, label: str) -> None:
        canonical = self._canonical_path(path, label)
        descriptor = self._open_regular(canonical)
        os.close(descriptor)

    @staticmethod
    def _open_regular(path: Path) -> int:
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(path, flags)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                os.close(descriptor)
                raise ConfigurationError("runtime artifact is not a regular file")
            return descriptor
        except FileNotFoundError as error:
            raise ConfigurationError("runtime artifact is missing", cause=error) from error
        except OSError as error:
            raise ConfigurationError("runtime artifact cannot be opened", cause=error) from error


@dataclass(frozen=True, slots=True)
class SubprocessConfig:
    executable: Path
    model: Path
    executable_sha256: str
    model_sha256: str
    executable_manifest: RuntimeArtifactManifest
    model_manifest: RuntimeArtifactManifest
    endpoint: str
    backend: str = "cpu"
    extra_args: tuple[str, ...] = ()
    model_id: str = "parakeet-tdt-0.6b-v3.oss-align.q8_0"

    def __post_init__(self) -> None:
        _validate_loopback_endpoint(self.endpoint)
        if self.backend != "cpu":
            prefix, _, index = self.backend.partition(":")
            if prefix != "cuda" or not index.isdigit():
                raise ConfigurationError("sidecar backend must be cpu or cuda:<index>")
        if self.executable_manifest.allowed_path.absolute() != self.executable.absolute():
            raise ConfigurationError("runtime executable manifest path does not match executable")
        if self.model_manifest.allowed_path.absolute() != self.model.absolute():
            raise ConfigurationError("runtime model manifest path does not match model")
        if self.executable_manifest.sha256.lower() != self.executable_sha256.lower():
            raise ConfigurationError("runtime executable checksum manifest mismatch")
        if self.model_manifest.sha256.lower() != self.model_sha256.lower():
            raise ConfigurationError("runtime model checksum manifest mismatch")
        if not self.model_id.strip():
            raise ConfigurationError("runtime model_id must not be empty")
        forbidden = {
            "--model",
            "--host",
            "--port",
            "--backend",
            "--asr-model",
            "--api-key",
            "--http.api-key",
            "--asr.model.name",
            "--asr.backend.gpu",
            "--http.host",
            "--http.port",
            "--device",
        }
        if any(argument.split("=", 1)[0] in forbidden for argument in self.extra_args):
            raise ConfigurationError(
                "runtime extra_args cannot override security-critical arguments"
            )

    def argv(self) -> list[str]:
        host, port = _validate_loopback_endpoint(self.endpoint)
        args = [
            str(self.executable),
            "serve",
            "--asr-model",
            str(self.model),
            "--host",
            host,
            "--port",
            str(port),
            "--no-ui",
            "--asr.model.name",
            self.model_id,
            "--device",
            self.backend,
        ]
        args.extend(self.extra_args)
        return args


_READY_DEVICE_ALIASES = {"cpu": frozenset({"cpu"})}


def _ready_device_matches_backend(device: object, backend: str) -> bool:
    if not isinstance(device, str):
        return False
    aliases = _READY_DEVICE_ALIASES.get(backend)
    if aliases is not None:
        return device in aliases
    if backend.startswith("cuda:") and backend[5:].isdigit():
        return device == backend
    return False


class UrllibReadinessProbe:
    def __init__(self, endpoint: str, path: str = "/ready", expected_backend: str = "cpu") -> None:
        _validate_loopback_endpoint(endpoint)
        self._url = f"{endpoint.rstrip('/')}/{path.lstrip('/')}"
        self._expected_backend = expected_backend
        self._nonce: str | None = None
        self._api_key: str | None = None

    def set_nonce(self, nonce: str) -> None:
        if not isinstance(nonce, str) or not nonce:
            raise ConfigurationError("sidecar readiness nonce must be non-empty")
        self._nonce = nonce

    def set_api_key(self, api_key: str) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ConfigurationError("sidecar readiness API key must be non-empty")
        self._api_key = api_key

    def clear_credentials(self) -> None:
        self._nonce = None
        self._api_key = None

    def ready(self, timeout: float) -> bool:
        headers: dict[str, str] = {}
        if self._nonce is not None:
            headers[ASR_NONCE_HEADER] = self._nonce
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = Request(self._url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                if not 200 <= response.status < 300:
                    return False
                payload = json.loads(response.read(64 * 1024).decode("utf-8"))
                return (
                    isinstance(payload, dict)
                    and payload.get("ready") is True
                    and isinstance(payload.get("capabilities"), list)
                    and "transcription" in payload["capabilities"]
                    and _ready_device_matches_backend(payload.get("device"), self._expected_backend)
                )
        except (HTTPError, URLError, OSError, TimeoutError, ValueError, UnicodeDecodeError):
            return False


class SubprocessSupervisor:
    """Launch and stop a sidecar with a safe argv and bounded process lifecycle."""

    def __init__(
        self,
        config: SubprocessConfig,
        *,
        verifier: RuntimeArtifactVerifier | None = None,
        readiness_probe: ReadinessProbe | None = None,
        popen_factory: Callable[..., ProcessHandle] = subprocess.Popen,
        clock: MonotonicClock | None = None,
        diagnostics: StartupDiagnostics | None = None,
    ) -> None:
        self.config = config
        self._verifier = verifier or RuntimeArtifactVerifier()
        self._readiness_probe = readiness_probe or UrllibReadinessProbe(
            config.endpoint, expected_backend=config.backend
        )
        self._popen_factory = popen_factory
        self._clock = clock or _SystemClock()
        self._diagnostics = diagnostics or StartupDiagnostics()
        self._process: ProcessHandle | None = None
        self._process_state = _ProcessLifecycleState.NOT_CREATED
        self._termination_lock = Lock()
        self._windows_job = None
        self._artifact_locks: list[_WindowsArtifactReadLock] = []
        self._artifact_recovery: set[_WindowsArtifactReadLock] = set()
        self._artifact_recovery_lock = Lock()
        self._artifact_reaper: Thread | None = None
        self._job_recovery: set[object] = set()
        self._job_recovery_lock = Lock()
        self._job_reaper: Thread | None = None
        self._process_reaper_generation: _ProcessReaperGeneration | None = None
        self._process_reaper_generation_number = 0
        self._process_reaper_lock = Lock()
        self._process_reaper_done = Event()
        self._process_reaper_done.set()
        self._nonce: str | None = None
        self._api_key: str | None = None
        self._credential_recovery_pending = False
        self._unsafe_tree_recovery_pending = False
        self.process_tree_mode = "windows-taskkill" if os.name == "nt" else "posix-process-group"

    @property
    def diagnostics(self) -> StartupDiagnostics:
        return self._diagnostics

    @property
    def nonce(self) -> str | None:
        return self._nonce

    @property
    def api_key(self) -> str | None:
        return self._api_key

    def start(self) -> None:
        if self._has_pending_cleanup_resources():
            raise RuntimeRecoveryPendingError("sidecar cleanup is still pending")
        if self.is_running() and not self.reaper_owns_process():
            return
        if self._process is not None:
            if self.reaper_owns_process():
                raise RuntimeUnavailableError("old sidecar process cleanup is still pending")
            errors: list[BaseException] = []
            if self._windows_job is not None:
                try:
                    self._close_job(self._windows_job)
                except BaseException as error:
                    errors.append(error)
            try:
                self._close_artifact_locks()
            except BaseException as error:
                errors.append(error)
            if errors:
                raise ExceptionGroup("previous sidecar cleanup failed", errors)
            self._process = None
            self._process_state = _ProcessLifecycleState.NOT_CREATED
        self._diagnostics.reset()
        phase = "configuration"
        operation = "validate_endpoint"
        try:
            self._diagnostics.record(phase, operation, "validated")
            self._nonce = generate_nonce()
            operation = "set_nonce"
            self._readiness_probe.set_nonce(self._nonce)
            self._api_key = generate_nonce()
            operation = "set_api_key"
            self._readiness_probe.set_api_key(self._api_key)
            sidecar_environment = {
                key: os.environ[key]
                for key in (
                    "PATH",
                    "SystemRoot",
                    "TEMP",
                    "TMP",
                    "USERPROFILE",
                    "LOCALAPPDATA",
                    "CUDA_PATH",
                    "CUDA_VISIBLE_DEVICES",
                )
                if key in os.environ
            }
            sidecar_environment[ASR_NONCE_ENV] = self._nonce
            sidecar_environment[ASR_API_KEY_ENV] = self._api_key
            kwargs = {
                "shell": False,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "env": sidecar_environment,
            }
            if os.name != "nt":
                kwargs["start_new_session"] = True
            phase = "artifact_verification"
            operation = "verify_artifact"
            self._verifier.verify_manifest(
                self.config.executable, self.config.executable_manifest, label="runtime executable"
            )
            self._verifier.verify_manifest(
                self.config.model, self.config.model_manifest, label="runtime model"
            )
            verified_identities = (
                self._verifier.identity(self.config.executable),
                self._verifier.identity(self.config.model),
            )
            self._diagnostics.record(phase, operation, "verified")
            identities = (
                self._verifier.identity(self.config.executable),
                self._verifier.identity(self.config.model),
            )
            if identities != verified_identities:
                raise ConfigurationError("runtime artifact identity changed before launch")
            if os.name == "nt":
                phase = "artifact_lease"
                operation = "open_artifact"
                for artifact in (self.config.executable, self.config.model):
                    self._artifact_locks.append(_open_artifact_read_lock(artifact))
                self._diagnostics.record(phase, operation, "acquired")
            windows_job = None
            resume = None
            if os.name == "nt":
                from .media_process import WindowsJobObject, _resume_suspended_process

                kwargs["creationflags"] = 0x00000004  # CREATE_SUSPENDED
                phase = "job_assignment"
                operation = "assign_job"
                windows_job = WindowsJobObject(
                    max_processes=None,
                    max_process_memory_bytes=None,
                )
                resume = _resume_suspended_process
                self._windows_job = windows_job
            phase = "process_create_suspended" if os.name == "nt" else "process_create_suspended"
            operation = "create_process"
            self._process = self._popen_factory(self.config.argv(), **kwargs)
            self._process_state = (
                _ProcessLifecycleState.CREATED_SUSPENDED
                if windows_job is not None
                else _ProcessLifecycleState.RUNNING
            )
            self._diagnostics.record(phase, operation, "created")
            if windows_job is not None and resume is not None:
                phase = "job_assignment"
                operation = "assign_job"
                windows_job.assign(self._process)
                self._diagnostics.record(phase, operation, "assigned")
                phase = "artifact_revalidation"
                operation = "revalidate_artifact"
                for lock, manifest in zip(
                    self._artifact_locks,
                    (self.config.executable_manifest, self.config.model_manifest),
                    strict=True,
                ):
                    lock.revalidate(manifest.sha256, manifest.allowed_path)
                self._diagnostics.record(phase, operation, "revalidated")
                phase = "resume"
                operation = "resume_process"
                resume(self._process.pid)
                self._process_state = _ProcessLifecycleState.RUNNING
                self._diagnostics.record(phase, operation, "resumed")
                self.process_tree_mode = "windows-job-object-adapter"
        except BaseException as error:
            failure = (
                error
                if isinstance(error, ConfigurationError)
                else ConfigurationError("sidecar process could not be started", cause=error)
            )
            self._diagnostics.record_failure(phase, operation, failure)
            cleanup_errors: list[BaseException] = []
            try:
                self._stop_owned_process(self._clock.monotonic() + 1.0)
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
                self._diagnostics.record_cleanup_failure("terminate_process", cleanup_error)
            finally:
                if self._windows_job is not None:
                    try:
                        job = self._windows_job
                        if not self._defer_job_until_reaped(job):
                            self._close_job(job)
                    except BaseException as cleanup_error:
                        cleanup_errors.append(cleanup_error)
                        self._diagnostics.record_cleanup_failure("close_resource", cleanup_error)
                try:
                    self._close_artifact_locks()
                except BaseException as cleanup_error:
                    cleanup_errors.append(cleanup_error)
                    self._diagnostics.record_cleanup_failure("close_resource", cleanup_error)
            try:
                self.clear_credentials()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
                self._diagnostics.record_cleanup_failure("close_resource", cleanup_error)
            cleanup_pending = not self.cleanup_complete()
            if cleanup_pending:
                pending = RuntimeRecoveryPendingError("sidecar cleanup is still pending")
                cleanup_errors.append(pending)
                self._diagnostics.record_cleanup_failure("wait_for_exit", pending)
            self._diagnostics.set_cleanup_outcome(
                "pending" if cleanup_pending else "failed" if cleanup_errors else "complete"
            )
            raise failure from error

    def clear_credentials(self) -> None:
        errors: list[BaseException] = []
        self._nonce = None
        self._api_key = None
        clear_credentials = getattr(self._readiness_probe, "clear_credentials", None)
        if clear_credentials is not None:
            try:
                clear_credentials()
            except BaseException as error:
                errors.append(error)
        self._credential_recovery_pending = bool(errors)
        if errors:
            raise ExceptionGroup("sidecar credential cleanup failed", errors)

    def wait_ready(self, deadline: float) -> bool:
        while self._clock.monotonic() < deadline:
            if self._process is None or self._process.poll() is not None:
                return False
            remaining = deadline - self._clock.monotonic()
            if self._readiness_probe.ready(min(0.25, remaining)):
                return True
            self._clock.sleep(min(0.02, remaining))
        return False

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def terminate(self, deadline: float) -> None:
        self._stop_owned_process(deadline)

    def kill(self, deadline: float | None = None) -> None:
        cleanup_deadline = deadline
        if cleanup_deadline is None:
            cleanup_deadline = self._clock.monotonic() + 1.0
        self._stop_owned_process(cleanup_deadline)

    def _stop_owned_process(self, deadline: float) -> None:
        process = self._process
        if process is None:
            self._diagnostics.record("cleanup", "get_process_state", "process_not_owned")
            return
        with self._termination_lock:
            if self._process_state is _ProcessLifecycleState.REAPED:
                self._diagnostics.record("cleanup", "get_process_state", "process_reaped")
                return
            if self._process_state is _ProcessLifecycleState.STOP_REQUESTED:
                self._diagnostics.record(
                    "cleanup", "terminate_process", "termination_already_requested"
                )
                return
            try:
                already_stopped = process.poll() is not None
            except BaseException:
                already_stopped = False
            escalation = self._process_state is _ProcessLifecycleState.TERMINATION_FAILED
            if not already_stopped:
                self._process_state = _ProcessLifecycleState.STOP_REQUESTED
        if already_stopped:
            self._process_state = _ProcessLifecycleState.EXITED
            self._diagnostics.record("cleanup", "get_process_state", "process_already_stopped")
            try:
                process.wait(timeout=max(0.0, deadline - self._clock.monotonic()))
                self._process_state = _ProcessLifecycleState.REAPED
                self._diagnostics.record("cleanup", "reap_process", "reaped")
            except (TimeoutError, subprocess.TimeoutExpired):
                self._diagnostics.record("cleanup", "wait_for_exit", "deadline_exceeded")
                self._start_process_reaper(process)
            finally:
                self._close_cleanup_resources()
            return
        self._diagnostics.record(
            "cleanup", "terminate_process", "escalation_requested" if escalation else "requested"
        )
        try:
            self._terminate_process_once(process, deadline, escalation=escalation)
            if not _process_is_alive(process):
                self._process_state = _ProcessLifecycleState.REAPED
        except RuntimeRecoveryPendingError:
            self._unsafe_tree_recovery_pending = True
            if _process_is_alive(process):
                self._process_state = _ProcessLifecycleState.TERMINATION_FAILED
                self._start_process_reaper(process, pending_job=self._windows_job)
            else:
                self._process_state = _ProcessLifecycleState.REAPED
            raise
        except BaseException:
            if _process_is_alive(process):
                self._process_state = _ProcessLifecycleState.TERMINATION_FAILED
                self._start_process_reaper(process, pending_job=self._windows_job)
            else:
                self._process_state = _ProcessLifecycleState.REAPED
            raise
        finally:
            if (
                not _process_is_alive(process)
                and self._process_state is not _ProcessLifecycleState.REAPED
            ):
                self._process_state = _ProcessLifecycleState.REAPED
                self._diagnostics.record("cleanup", "reap_process", "reaped")
            self._close_cleanup_resources()

    def _terminate_process_once(
        self, process: ProcessHandle, deadline: float, *, escalation: bool = False
    ) -> None:
        if self._windows_job is not None:
            job = self._windows_job
            reaper_started = False

            def start_reaper(process: ProcessHandle) -> None:
                nonlocal reaper_started
                reaper_started = True
                self._start_process_reaper(process, pending_job=job)

            try:
                _terminate_job_process(
                    job,
                    process,
                    deadline,
                    clock=self._clock,
                    start_reaper=start_reaper,
                )
                if _process_is_alive(process):
                    self._process_state = _ProcessLifecycleState.TERMINATION_FAILED
                else:
                    self._process_state = _ProcessLifecycleState.REAPED
            except BaseException:
                if _process_is_alive(process):
                    raise
                self._process_state = _ProcessLifecycleState.REAPED
                self._diagnostics.record("cleanup", "get_process_state", "process_already_stopped")
            return
        if os.name == "nt":
            try:
                if self._windows_job is None:
                    raise RuntimeRecoveryPendingError(
                        "Windows process tree ownership is unavailable"
                    )
                if escalation:
                    self._kill_windows_tree(process, deadline=deadline, clock=self._clock)
                    process.kill()
                else:
                    process.terminate()
            except RuntimeRecoveryPendingError:
                raise
            except BaseException:
                if _process_is_alive(process):
                    raise
                self._process_state = _ProcessLifecycleState.REAPED
                self._diagnostics.record("cleanup", "terminate_process", "process_already_stopped")
                return
            if not escalation:
                self._kill_windows_tree(process, deadline=deadline, clock=self._clock)
        else:
            if escalation:
                self._signal_process_group(process, signal.SIGKILL)
                process.kill()
            else:
                self._signal_process_group(process, signal.SIGTERM)
        remaining = max(0.0, deadline - self._clock.monotonic())
        try:
            process.wait(timeout=remaining)
            self._process_state = _ProcessLifecycleState.REAPED
        except (TimeoutError, subprocess.TimeoutExpired):
            self._diagnostics.record("cleanup", "wait_for_exit", "deadline_exceeded")
            if not _process_is_alive(process):
                return
            if os.name != "nt":
                self._signal_process_group(process, signal.SIGKILL)
            try:
                process.kill()
            except BaseException:
                if _process_is_alive(process):
                    self._start_process_reaper(process)
                raise
            remaining = max(0.0, deadline - self._clock.monotonic())
            if remaining:
                try:
                    process.wait(timeout=remaining)
                except (TimeoutError, subprocess.TimeoutExpired):
                    if _process_is_alive(process):
                        self._start_process_reaper(process)
                    else:
                        self._process_state = _ProcessLifecycleState.REAPED
            else:
                if _process_is_alive(process):
                    self._start_process_reaper(process)
                else:
                    self._process_state = _ProcessLifecycleState.REAPED
        except BaseException:
            if _process_is_alive(process):
                self._process_state = _ProcessLifecycleState.TERMINATION_FAILED
                self._start_process_reaper(process, pending_job=self._windows_job)
            else:
                self._process_state = _ProcessLifecycleState.REAPED
            raise

    def _close_cleanup_resources(self) -> None:
        errors: list[BaseException] = []
        try:
            self._close_artifact_locks()
        except BaseException as error:
            errors.append(error)
            self._diagnostics.record_cleanup_failure("close_resource", error)
        job = self._windows_job
        if job is not None and not self._defer_job_until_reaped(job):
            try:
                self._close_job(job)
            except BaseException as error:
                errors.append(error)
                self._diagnostics.record_cleanup_failure("close_resource", error)
        if errors:
            raise ExceptionGroup("sidecar termination cleanup failed", errors)

    def _start_process_reaper(
        self, process: ProcessHandle, *, pending_job: object | None = None
    ) -> None:
        with self._process_reaper_lock:
            generation = self._process_reaper_generation
            if generation is not None:
                if generation.process is not process:
                    raise RuntimeError("process reaper has no matching generation")
                if not generation.done.is_set():
                    if pending_job is not None:
                        generation.pending_job = pending_job
                    return
            self._process_reaper_generation_number += 1
            generation = _ProcessReaperGeneration(
                self._process_reaper_generation_number,
                process,
            )
            generation.pending_job = pending_job
            self._process_reaper_generation = generation
            self._process_reaper_done.clear()
            generation.thread = Thread(
                target=self._reap_process,
                args=(generation,),
                name="process-cleanup-reaper",
                daemon=True,
            )
            generation.thread.start()

    def _reap_process(self, generation: _ProcessReaperGeneration) -> None:
        while True:
            try:
                if generation.process.poll() is not None:
                    break
                generation.process.wait()
            except BaseException:
                self._clock.sleep(0.05)
        with self._process_reaper_lock:
            pending_job = generation.pending_job
            generation.pending_job = None
            generation.observed_job = pending_job
        if pending_job is not None:
            try:
                pending_job.close()
                if self._windows_job is pending_job:
                    self._windows_job = None
            except BaseException:
                with self._job_recovery_lock:
                    self._job_recovery.add(pending_job)
                if self._windows_job is pending_job:
                    self._windows_job = None
                self._start_job_reaper()
        with self._process_reaper_lock:
            if self._process_reaper_generation is generation:
                self._process_reaper_generation = None
                self._process_reaper_done.set()
        with self._termination_lock:
            if self._process is generation.process:
                self._process_state = _ProcessLifecycleState.REAPED
        generation.done.set()

    def reaper_owns_process(self) -> bool:
        process = self._process
        if process is None:
            return False
        with self._process_reaper_lock:
            generation = self._process_reaper_generation
            return generation is not None and generation.process is process

    def cleanup_complete(self) -> bool:
        with self._artifact_recovery_lock:
            artifacts_pending = bool(self._artifact_recovery)
        with self._job_recovery_lock:
            jobs_pending = bool(self._job_recovery)
        with self._process_reaper_lock:
            process_reaper_pending = self._process_reaper_generation is not None
        process_pending = (
            self._process is not None and self._process_state is not _ProcessLifecycleState.REAPED
        )
        if process_pending:
            process_pending = _process_is_alive(self._process)
        return not (
            process_reaper_pending
            or process_pending
            or getattr(self, "_credential_recovery_pending", False)
            or getattr(self, "_unsafe_tree_recovery_pending", False)
            or artifacts_pending
            or jobs_pending
            or self._artifact_locks
            or self._windows_job is not None
            or (self._artifact_reaper is not None and self._artifact_reaper.is_alive())
            or (self._job_reaper is not None and self._job_reaper.is_alive())
            or not self._process_reaper_done.is_set()
        )

    def _has_pending_cleanup_resources(self) -> bool:
        return not self.cleanup_complete()

    def _close_artifact_locks(self) -> None:
        locks = self._artifact_locks
        self._artifact_locks = []
        errors: list[BaseException] = []
        for lock in locks:
            try:
                lock.close()
            except BaseException as error:
                errors.append(error)
                with self._artifact_recovery_lock:
                    self._artifact_recovery.add(lock)
        if errors:
            self._start_artifact_reaper()
            raise ExceptionGroup("runtime artifact lock cleanup failed", errors)

    def _start_artifact_reaper(self) -> None:
        with self._artifact_recovery_lock:
            if not self._artifact_recovery:
                return
            if self._artifact_reaper is not None and self._artifact_reaper.is_alive():
                return

            def reap() -> None:
                while True:
                    with self._artifact_recovery_lock:
                        locks = tuple(self._artifact_recovery)
                    if not locks:
                        return
                    for lock in locks:
                        try:
                            lock.close()
                        except BaseException:
                            continue
                        with self._artifact_recovery_lock:
                            self._artifact_recovery.discard(lock)
                    self._sleep(0.05)

            self._artifact_reaper = Thread(
                target=reap, name="runtime-artifact-lock-reaper", daemon=True
            )
            self._artifact_reaper.start()

    def _close_job(self, job) -> None:
        try:
            job.close()
        except BaseException:
            with self._job_recovery_lock:
                self._job_recovery.add(job)
            self._start_job_reaper()
            if self._windows_job is job:
                self._windows_job = None
            raise
        if self._windows_job is job:
            self._windows_job = None

    def _defer_job_until_reaped(self, job) -> bool:
        process = self._process
        with self._process_reaper_lock:
            generation = self._process_reaper_generation
            if generation is None or generation.process is not process:
                return False
            if generation.pending_job is job or generation.observed_job is job:
                return True
            if generation.done.is_set():
                return False
            generation.pending_job = job
            return True

    def _start_job_reaper(self) -> None:
        with self._job_recovery_lock:
            if not self._job_recovery:
                return
            if self._job_reaper is not None and self._job_reaper.is_alive():
                return

            def reap() -> None:
                while True:
                    with self._job_recovery_lock:
                        jobs = tuple(self._job_recovery)
                    if not jobs:
                        return
                    for job in jobs:
                        try:
                            job.close()
                        except BaseException:
                            continue
                        with self._job_recovery_lock:
                            self._job_recovery.discard(job)
                    self._sleep(0.05)

            self._job_reaper = Thread(target=reap, name="windows-job-cleanup-reaper", daemon=True)
            self._job_reaper.start()

    def _sleep(self, seconds: float) -> None:
        clock = getattr(self, "_clock", None)
        if clock is None:
            time.sleep(seconds)
        else:
            clock.sleep(seconds)

    @staticmethod
    def _kill_windows_tree(
        process: ProcessHandle,
        deadline: float | None = None,
        *,
        clock: MonotonicClock | None = None,
    ) -> None:
        del process, deadline, clock
        if sys.platform != "win32":
            return
        raise RuntimeRecoveryPendingError(
            "Windows PID-tree fallback is disabled because child PID ownership is unverified"
        )

    def _signal_process_group(self, process: ProcessHandle, sig: signal.Signals) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return


def _terminate_job_process(
    job,
    process: ProcessHandle,
    deadline: float,
    *,
    clock: MonotonicClock,
    start_reaper: Callable[[ProcessHandle], None],
) -> None:
    """Terminate the job, reap the root, and fall back to a direct kill."""
    termination_error: BaseException | None = None
    try:
        job.terminate()
    except BaseException as error:
        termination_error = error
    try:
        process.wait(timeout=max(0.0, deadline - clock.monotonic()))
    except (TimeoutError, subprocess.TimeoutExpired):
        try:
            process.kill()
            remaining = max(0.0, deadline - clock.monotonic())
            process.wait(timeout=remaining)
        except BaseException as error:
            if termination_error is None:
                termination_error = error
            if _process_is_alive(process):
                start_reaper(process)
    except BaseException as error:
        if termination_error is None:
            termination_error = error
        if _process_is_alive(process):
            start_reaper(process)
    if _process_is_alive(process):
        start_reaper(process)
    if termination_error is not None:
        raise termination_error


def _process_is_alive(process: ProcessHandle) -> bool:
    try:
        return process.poll() is None
    except BaseException:
        return True


class _WindowsArtifactReadLock:
    def __init__(
        self,
        api,
        handles: list[int],
        identity: tuple[int, int, int, int, int],
        native_path: str,
        registry=None,
    ) -> None:
        self._api = api
        self._handles = handles
        self._identity = identity
        self._native_path = native_path
        self._lock = Lock()
        if registry is None:
            from .windows_snapshot import NativeHandleLeaseRegistry

            registry = NativeHandleLeaseRegistry()
        self._registry = registry
        from .windows_snapshot import _HandleLease

        self._leases = {handle: _HandleLease(self._api, handle) for handle in handles}
        for lease in self._leases.values():
            self._registry.register(lease)

    def revalidate(self, expected_sha256: str, expected_path: Path) -> None:
        from .ffmpeg import _hash_windows_handle, _native_path_key
        from .media_snapshot import _WindowsFileInformation

        info = _WindowsFileInformation()
        handle = self._handles[-1]
        if not self._api.dll.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise ConfigurationError("runtime artifact identity could not be revalidated")
        identity = (
            int(info.volume_serial),
            int(info.index_high),
            int(info.index_low),
            int(info.size_high),
            int(info.size_low),
        )
        if identity != self._identity:
            raise ConfigurationError("runtime artifact identity changed before launch")
        if _native_path_key(self._native_path) != _native_path_key(expected_path):
            raise ConfigurationError("runtime artifact parent path changed before launch")
        if _hash_windows_handle(self._api, handle).lower() != expected_sha256.lower():
            raise ConfigurationError("runtime artifact changed before launch")

    def close(self) -> None:
        with self._lock:
            remaining: list[int] = []
            errors: list[BaseException] = []
            for handle in reversed(self._handles):
                try:
                    self._registry.release(self._leases[handle])
                except BaseException as error:
                    remaining.append(handle)
                    errors.append(error)
            self._handles = list(reversed(remaining))
            for handle in set(self._leases) - set(self._handles):
                self._leases.pop(handle, None)
            if errors:
                raise ExceptionGroup("runtime artifact handle cleanup failed", errors)


def _open_artifact_read_lock(path: Path) -> _WindowsArtifactReadLock:
    from .ffmpeg import _open_windows_artifact_path
    from .windows_snapshot import NativeHandleLeaseRegistry, WindowsKernel32

    api = WindowsKernel32()
    registry = NativeHandleLeaseRegistry()
    handles, identity, native_path = _open_windows_artifact_path(path, api, registry=registry)
    return _WindowsArtifactReadLock(api, handles, identity, native_path, registry)


def _require_matching_process_creation_time(expected: int | None, actual: int | None) -> None:
    if expected is None or actual is None or expected != actual:
        raise RuntimeRecoveryPendingError(
            "Windows child process identity could not be verified before termination"
        )
