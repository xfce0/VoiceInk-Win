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
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from voiceink_win.domain import ConfigurationError, MissingModelError, RuntimeUnavailableError

from .authentication import ASR_NONCE_ENV, ASR_NONCE_HEADER, generate_nonce, validate_nonce


class ProcessHandle(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class ReadinessProbe(Protocol):
    def ready(self, timeout: float) -> bool: ...


class MonotonicClock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


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
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
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
    model_id: str = "parakeet-tdt-v3"

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
        forbidden = {"--model", "--host", "--port", "--backend"}
        if any(argument.split("=", 1)[0] in forbidden for argument in self.extra_args):
            raise ConfigurationError(
                "runtime extra_args cannot override security-critical arguments"
            )

    def argv(self) -> list[str]:
        host, port = _validate_loopback_endpoint(self.endpoint)
        return [
            str(self.executable),
            "--model",
            str(self.model),
            "--host",
            host,
            "--port",
            str(port),
            "--backend",
            self.backend,
            *self.extra_args,
        ]


class UrllibReadinessProbe:
    def __init__(self, endpoint: str, path: str = "/health") -> None:
        _validate_loopback_endpoint(endpoint)
        self._url = f"{endpoint.rstrip('/')}/{path.lstrip('/')}"
        self._nonce: str | None = None
        self._attestation: dict[str, object] | None = None

    def set_nonce(self, nonce: str) -> None:
        if not isinstance(nonce, str) or not nonce:
            raise ConfigurationError("sidecar readiness nonce must be non-empty")
        self._nonce = nonce

    def configure_attestation(
        self, *, pid: int, nonce: str, model_id: str, model_sha256: str, backend: str
    ) -> None:
        self.set_nonce(nonce)
        self._attestation = {
            "pid": pid,
            "nonce": nonce,
            "model_id": model_id,
            "model_sha256": model_sha256.lower(),
            "backend": backend,
        }

    def ready(self, timeout: float) -> bool:
        if self._nonce is None or self._attestation is None:
            return False
        request = Request(self._url, headers={ASR_NONCE_HEADER: self._nonce}, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                if not 200 <= response.status < 300:
                    return False
                payload = json.loads(response.read(64 * 1024).decode("utf-8"))
                expected = self._attestation
                return (
                    isinstance(payload, dict)
                    and payload.get("schema") == "voiceink.asr.health.v1"
                    and payload.get("protocol_version") == 1
                    and payload.get("ready") is True
                    and isinstance(payload.get("pid"), int)
                    and payload["pid"] == expected["pid"]
                    and validate_nonce(payload.get("nonce"), expected["nonce"])
                    and payload.get("model_id") == expected["model_id"]
                    and str(payload.get("model_sha256", "")).lower() == expected["model_sha256"]
                    and payload.get("backend") == expected["backend"]
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
    ) -> None:
        self.config = config
        self._verifier = verifier or RuntimeArtifactVerifier()
        self._readiness_probe = readiness_probe or UrllibReadinessProbe(config.endpoint)
        self._popen_factory = popen_factory
        self._clock = clock or _SystemClock()
        self._process: ProcessHandle | None = None
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
        self.process_tree_mode = "windows-taskkill" if os.name == "nt" else "posix-process-group"

    @property
    def nonce(self) -> str | None:
        return self._nonce

    def start(self) -> None:
        if self._has_pending_cleanup_resources():
            raise RuntimeUnavailableError("sidecar cleanup is still pending")
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
        self._nonce = generate_nonce()
        set_probe_nonce = getattr(self._readiness_probe, "set_nonce", None)
        if set_probe_nonce is not None:
            set_probe_nonce(self._nonce)
        try:
            if os.name == "nt":
                for artifact in (self.config.executable, self.config.model):
                    self._artifact_locks.append(_open_artifact_read_lock(artifact))
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
        except BaseException:
            self._close_artifact_locks()
            raise
        kwargs = {
            "shell": False,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": {**os.environ, ASR_NONCE_ENV: self._nonce},
        }
        if os.name != "nt":
            kwargs["start_new_session"] = True
        try:
            identities = (
                self._verifier.identity(self.config.executable),
                self._verifier.identity(self.config.model),
            )
            if identities != verified_identities:
                raise ConfigurationError("runtime artifact identity changed before launch")
            windows_job = None
            resume = None
            if os.name == "nt":
                from .media_process import WindowsJobObject, _resume_suspended_process

                kwargs["creationflags"] = 0x00000004  # CREATE_SUSPENDED
                windows_job = WindowsJobObject(
                    max_processes=None,
                    max_process_memory_bytes=None,
                )
                resume = _resume_suspended_process
                self._windows_job = windows_job
                for lock, manifest in zip(
                    self._artifact_locks,
                    (self.config.executable_manifest, self.config.model_manifest),
                    strict=True,
                ):
                    lock.revalidate(manifest.sha256, manifest.allowed_path)
            self._process = self._popen_factory(self.config.argv(), **kwargs)
            configure_attestation = getattr(self._readiness_probe, "configure_attestation", None)
            if configure_attestation is not None:
                configure_attestation(
                    pid=self._process.pid,
                    nonce=self._nonce,
                    model_id=self.config.model_id,
                    model_sha256=self.config.model_sha256,
                    backend=self.config.backend,
                )
            if windows_job is not None and resume is not None:
                windows_job.assign(self._process)
                resume(self._process.pid)
                self.process_tree_mode = "windows-job-object-adapter"
        except BaseException as error:
            try:
                reaper_started = False
                if self._windows_job is not None and self._process is not None:

                    def start_reaper(process: ProcessHandle) -> None:
                        nonlocal reaper_started
                        reaper_started = True
                        self._start_process_reaper(process, pending_job=self._windows_job)

                    _terminate_job_process(
                        self._windows_job,
                        self._process,
                        self._clock.monotonic() + 1.0,
                        clock=self._clock,
                        start_reaper=start_reaper,
                    )
                elif os.name == "nt" and self._process is not None and self._process.poll() is None:
                    self._kill_windows_tree(self._process, clock=self._clock)
                    self._process.kill()
                    cleanup_deadline = self._clock.monotonic() + 1.0
                    try:
                        self._process.wait(
                            timeout=max(0.0, cleanup_deadline - self._clock.monotonic())
                        )
                    except (TimeoutError, subprocess.TimeoutExpired):
                        self._start_process_reaper(self._process)
            finally:
                errors: list[BaseException] = []
                if self._windows_job is not None:
                    try:
                        if not reaper_started:
                            self._close_job(self._windows_job)
                    except BaseException as cleanup_error:
                        errors.append(cleanup_error)
                try:
                    self._close_artifact_locks()
                except BaseException as cleanup_error:
                    errors.append(cleanup_error)
            if errors:
                raise ExceptionGroup("sidecar startup cleanup failed", errors) from error
            self._nonce = None
            if isinstance(error, ConfigurationError):
                raise
            raise ConfigurationError("sidecar process could not be started", cause=error) from error

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
        process = self._process
        if process is None:
            return
        if self._windows_job is not None:
            job = self._windows_job
            errors: list[BaseException] = []
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
            except BaseException as error:
                errors.append(error)
            try:
                self._close_artifact_locks()
            except BaseException as error:
                errors.append(error)
            try:
                if not reaper_started:
                    self._close_job(job)
            except BaseException as error:
                errors.append(error)
            if errors:
                raise ExceptionGroup("sidecar termination cleanup failed", errors)
            return
        if process.poll() is not None:
            if os.name == "nt":
                self.kill(deadline=deadline)
            else:
                try:
                    process.wait(timeout=max(0.0, deadline - self._clock.monotonic()))
                except (TimeoutError, subprocess.TimeoutExpired):
                    return
                except BaseException:
                    if _process_is_alive(process):
                        self._start_process_reaper(process)
                    raise
                finally:
                    self._close_artifact_locks()
            return
        if os.name == "nt":
            process.terminate()
            self._kill_windows_tree(process, deadline=deadline, clock=self._clock)
        else:
            self._signal_process_group(process, signal.SIGTERM)
        remaining = max(0.0, deadline - self._clock.monotonic())
        try:
            process.wait(timeout=remaining)
        except (TimeoutError, subprocess.TimeoutExpired):
            self.kill(deadline=deadline)
            return
        except BaseException:
            if _process_is_alive(process):
                self._start_process_reaper(process)
            raise
        finally:
            self._close_artifact_locks()

    def kill(self, deadline: float | None = None) -> None:
        process = self._process
        if process is None:
            return
        if self._windows_job is not None:
            job = self._windows_job
            errors: list[BaseException] = []
            reaper_started = False

            def start_reaper(process: ProcessHandle) -> None:
                nonlocal reaper_started
                reaper_started = True
                self._start_process_reaper(process, pending_job=job)

            try:
                cleanup_deadline = deadline
                if cleanup_deadline is None:
                    cleanup_deadline = self._clock.monotonic() + 1.0
                _terminate_job_process(
                    job,
                    process,
                    cleanup_deadline,
                    clock=self._clock,
                    start_reaper=start_reaper,
                )
            except BaseException as error:
                errors.append(error)
            try:
                if not reaper_started:
                    self._close_job(job)
            except BaseException as error:
                errors.append(error)
            try:
                self._close_artifact_locks()
            except BaseException as error:
                errors.append(error)
            if errors:
                raise ExceptionGroup("sidecar kill cleanup failed", errors)
            return
        if os.name == "nt":
            self._kill_windows_tree(process, clock=self._clock)
        else:
            self._signal_process_group(process, signal.SIGKILL)
        cleanup_deadline = deadline
        if cleanup_deadline is None:
            cleanup_deadline = self._clock.monotonic() + 1.0
        try:
            process.wait(timeout=max(0.0, cleanup_deadline - self._clock.monotonic()))
        except (TimeoutError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except BaseException:
                if _process_is_alive(process):
                    self._start_process_reaper(process)
                raise
            remaining = max(0.0, cleanup_deadline - self._clock.monotonic())
            if remaining:
                try:
                    process.wait(timeout=remaining)
                except (TimeoutError, subprocess.TimeoutExpired):
                    if _process_is_alive(process):
                        self._start_process_reaper(process)
            else:
                if _process_is_alive(process):
                    self._start_process_reaper(process)
        except BaseException:
            if _process_is_alive(process):
                self._start_process_reaper(process)
            raise
        finally:
            self._close_artifact_locks()

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
        return not (
            process_reaper_pending
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
        if sys.platform != "win32":
            return
        expected_creation_time = _process_creation_time(getattr(process, "_handle", None))
        if expected_creation_time is None:
            raise OSError("cannot safely identify the Windows process tree")
        _kill_windows_descendants(
            process.pid,
            expected_creation_time,
            deadline=deadline,
            clock=clock or _SystemClock(),
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


def _process_creation_time(handle) -> int | None:
    if handle is None:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    filetime = wintypes.FILETIME
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(filetime),
        ctypes.POINTER(filetime),
        ctypes.POINTER(filetime),
        ctypes.POINTER(filetime),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    creation = filetime()
    exit_time = filetime()
    kernel_time = filetime()
    user_time = filetime()
    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        return None
    return (creation.dwHighDateTime << 32) | creation.dwLowDateTime


def _kill_windows_descendants(
    root_pid: int,
    expected_creation_time: int | None,
    *,
    deadline: float | None = None,
    clock: MonotonicClock | None = None,
) -> None:
    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    invalid_handle = ctypes.c_void_p(-1).value
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    from .windows_snapshot import NativeHandleLeaseRegistry

    native_handles = NativeHandleLeaseRegistry(clock=clock)
    if expected_creation_time is None:
        return
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot in (None, invalid_handle):
        return
    try:
        native_handles.register_handle(snapshot, kernel32.CloseHandle)
        root_handle = kernel32.OpenProcess(0x1000, False, root_pid)
        if root_handle in (None, invalid_handle):
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: the root already exited.
                return
            raise ctypes.WinError(error)
        native_handles.register_handle(root_handle, kernel32.CloseHandle)
        if _process_creation_time(root_handle) != expected_creation_time:
            return
        children: dict[int, list[int]] = {}
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                children.setdefault(int(entry.th32ParentProcessID), []).append(
                    int(entry.th32ProcessID)
                )
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        native_handles.close()
    descendants: list[int] = []
    pending = list(children.get(root_pid, ()))
    while pending:
        pid = pending.pop()
        descendants.append(pid)
        pending.extend(children.get(pid, ()))
    handles = []
    try:
        for pid in [root_pid, *reversed(descendants)]:
            handle = kernel32.OpenProcess(0x0001 | 0x00100000, False, pid)
            if handle not in (None, invalid_handle):
                handles.append(handle)
                native_handles.register_handle(handle, kernel32.CloseHandle)
            elif pid != root_pid:
                raise ctypes.WinError(ctypes.get_last_error())
        now = (clock or _SystemClock()).monotonic
        cleanup_deadline = deadline if deadline is not None else now() + 1.0
        for handle in handles:
            if not kernel32.TerminateProcess(handle, 1):
                raise ctypes.WinError(ctypes.get_last_error())
            remaining = cleanup_deadline - now()
            if remaining <= 0:
                raise TimeoutError("Windows process tree cleanup deadline expired")
            remaining_ms = max(0, int(remaining * 1000))
            if kernel32.WaitForSingleObject(handle, remaining_ms) != 0:
                raise TimeoutError("Windows process tree did not terminate before the deadline")
    finally:
        native_handles.close()
