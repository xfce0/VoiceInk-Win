"""Stdlib process and artifact boundary for the local ASR sidecar."""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from voiceink_win.domain import ConfigurationError, MissingModelError


class ProcessHandle(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class ReadinessProbe(Protocol):
    def ready(self, timeout: float) -> bool: ...


def _validate_loopback_endpoint(endpoint: str) -> tuple[str, int]:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ConfigurationError("sidecar endpoint must be an HTTP loopback URL")
    if parsed.port is None:
        raise ConfigurationError("sidecar endpoint must include a port")
    return parsed.hostname, parsed.port


class RuntimeArtifactVerifier:
    """Verify immutable runtime and model artifacts before process startup."""

    def sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as artifact:
                for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            raise ConfigurationError("runtime artifact cannot be read", cause=error) from error
        return digest.hexdigest()

    def verify(self, path: Path, expected_sha256: str, *, label: str = "runtime artifact") -> None:
        if len(expected_sha256) != 64 or any(
            char not in "0123456789abcdefABCDEF" for char in expected_sha256
        ):
            raise ConfigurationError(f"{label} checksum is invalid")
        if not path.is_file():
            raise ConfigurationError(f"{label} is missing")
        actual = self.sha256(path)
        if actual.lower() != expected_sha256.lower():
            raise ConfigurationError(f"{label} checksum does not match")

    def verify_executable(self, path: Path, expected_sha256: str) -> None:
        self.verify(path, expected_sha256, label="runtime executable")
        if os.name != "nt" and not os.access(path, os.X_OK):
            raise ConfigurationError("runtime executable is not executable")

    def verify_model(self, path: Path, expected_sha256: str) -> None:
        if not path.is_file():
            raise MissingModelError("runtime model is missing")
        try:
            self.verify(path, expected_sha256, label="runtime model")
        except ConfigurationError as error:
            raise ConfigurationError("runtime model verification failed", cause=error) from error


@dataclass(frozen=True, slots=True)
class SubprocessConfig:
    executable: Path
    model: Path
    executable_sha256: str
    model_sha256: str
    endpoint: str = "http://127.0.0.1:8123"
    backend: str = "cpu"
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_loopback_endpoint(self.endpoint)
        if self.backend != "cpu":
            prefix, _, index = self.backend.partition(":")
            if prefix != "cuda" or not index.isdigit():
                raise ConfigurationError("sidecar backend must be cpu or cuda:<index>")

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

    def ready(self, timeout: float) -> bool:
        request = Request(self._url, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                response.read(1)
                return 200 <= response.status < 300
        except (HTTPError, URLError, OSError, TimeoutError):
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
    ) -> None:
        self.config = config
        self._verifier = verifier or RuntimeArtifactVerifier()
        self._readiness_probe = readiness_probe or UrllibReadinessProbe(config.endpoint)
        self._popen_factory = popen_factory
        self._process: ProcessHandle | None = None
        self.process_tree_mode = "windows-taskkill" if os.name == "nt" else "posix-process-group"

    def start(self) -> None:
        if self.is_running():
            return
        self._verifier.verify_executable(self.config.executable, self.config.executable_sha256)
        self._verifier.verify_model(self.config.model, self.config.model_sha256)
        kwargs = {
            "shell": False,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name != "nt":
            kwargs["start_new_session"] = True
        try:
            self._process = self._popen_factory(self.config.argv(), **kwargs)
        except OSError as error:
            raise ConfigurationError("sidecar process could not be started", cause=error) from error

    def wait_ready(self, deadline: float) -> bool:
        while time.monotonic() < deadline:
            if self._process is None or self._process.poll() is not None:
                return False
            remaining = deadline - time.monotonic()
            if self._readiness_probe.ready(min(0.25, remaining)):
                return True
            time.sleep(min(0.02, remaining))
        return False

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def terminate(self, deadline: float) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            process.terminate()
        else:
            self._signal_process_group(process, signal.SIGTERM)
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except TimeoutError:
            return

    def kill(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                shell=False,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            self._signal_process_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=1.0)
        except TimeoutError:
            process.kill()

    def _signal_process_group(self, process: ProcessHandle, sig: signal.Signals) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
