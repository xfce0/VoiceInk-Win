from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from voiceink_win.domain import ConfigurationError, MissingModelError
from voiceink_win.infrastructure import (
    RuntimeArtifactVerifier,
    SubprocessConfig,
    SubprocessSupervisor,
)


class FakeProbe:
    def ready(self, timeout: float) -> bool:
        return timeout > 0


class FakeProcess:
    pid = 12345

    def __init__(self) -> None:
        self.running = True
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self.running else 0

    def terminate(self) -> None:
        self.terminated = True
        self.running = False

    def kill(self) -> None:
        self.killed = True
        self.running = False

    def wait(self, timeout: float | None = None) -> int:
        self.running = False
        return 0


def write_artifact(path: Path, content: bytes) -> str:
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def test_artifact_verifier_checks_executable_and_model_sha256(tmp_path: Path) -> None:
    executable = tmp_path / "sidecar"
    model = tmp_path / "model.gguf"
    executable_hash = write_artifact(executable, b"executable")
    model_hash = write_artifact(model, b"model")
    executable.chmod(0o755)
    verifier = RuntimeArtifactVerifier()

    verifier.verify_executable(executable, executable_hash)
    verifier.verify_model(model, model_hash)

    with pytest.raises(ConfigurationError):
        verifier.verify(model, "0" * 64, label="model")


def test_artifact_verifier_distinguishes_missing_model(tmp_path: Path) -> None:
    with pytest.raises(MissingModelError):
        RuntimeArtifactVerifier().verify_model(tmp_path / "missing.gguf", "0" * 64)


def test_subprocess_supervisor_uses_safe_argv_and_bounded_readiness(
    tmp_path: Path, monkeypatch
) -> None:
    executable = tmp_path / "sidecar"
    model = tmp_path / "model.gguf"
    executable_hash = write_artifact(executable, b"executable")
    model_hash = write_artifact(model, b"model")
    executable.chmod(0o755)
    config = SubprocessConfig(
        executable=executable,
        model=model,
        executable_sha256=executable_hash,
        model_sha256=model_hash,
        endpoint="http://127.0.0.1:8123",
        extra_args=("--threads", "2"),
    )
    process = FakeProcess()
    calls: list[tuple[list[str], dict[str, object]]] = []

    def popen(argv: list[str], **kwargs):
        calls.append((argv, kwargs))
        return process

    killpg_calls: list[tuple[int, object]] = []
    if os.name != "nt":
        monkeypatch.setattr(
            "voiceink_win.infrastructure.process.os.killpg",
            lambda pid, sig: killpg_calls.append((pid, sig)),
        )
    supervisor = SubprocessSupervisor(
        config,
        readiness_probe=FakeProbe(),
        popen_factory=popen,
    )

    supervisor.start()

    assert supervisor.wait_ready(time.monotonic() + 1.0)
    assert calls[0][0] == [
        str(executable),
        "--model",
        str(model),
        "--host",
        "127.0.0.1",
        "--port",
        "8123",
        "--backend",
        "cpu",
        "--threads",
        "2",
    ]
    assert calls[0][1]["shell"] is False
    expected_mode = "windows-taskkill" if os.name == "nt" else "posix-process-group"
    assert supervisor.process_tree_mode == expected_mode
    supervisor.terminate(time.monotonic() + 1.0)
    if os.name == "nt":
        assert process.terminated
    else:
        assert killpg_calls


def test_subprocess_config_rejects_non_loopback_endpoint(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        SubprocessConfig(
            executable=tmp_path / "sidecar",
            model=tmp_path / "model.gguf",
            executable_sha256="0" * 64,
            model_sha256="0" * 64,
            endpoint="http://example.invalid:8123",
        )
