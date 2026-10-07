from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import voiceink_win.__main__ as cli
from voiceink_win.composition import RuntimePaths, build_application
from voiceink_win.domain import (
    AsrCapabilities,
    ConfigurationError,
    HealthStatus,
    RuntimeHealth,
    RuntimeUnavailableError,
)


def _runtime_files(tmp_path: Path) -> tuple[Path, Path, str]:
    executable = tmp_path / "runtime" / "nemo-speech.exe"
    model = tmp_path / "models" / "parakeet.gguf"
    executable.parent.mkdir()
    model.parent.mkdir()
    executable.write_bytes(b"runtime")
    model.write_bytes(b"model")
    lock = {
        "schema": "voiceink.runtime.artifact-lock.v1",
        "version": 1,
        "install_root": str(tmp_path),
        "artifacts": {
            "sidecar": {
                "kind": "executable",
                "version": "release-1",
                "provenance_url": "https://example.invalid/runtime",
                "sha256": hashlib.sha256(b"runtime").hexdigest(),
                "license": "Apache-2.0",
                "allowed_path": str(executable),
            },
            "parakeet-tdt-0.6b-v3.oss-align.q8_0": {
                "kind": "model",
                "version": "model-1",
                "provenance_url": "https://example.invalid/model",
                "sha256": hashlib.sha256(b"model").hexdigest(),
                "license": "CC-BY-4.0",
                "allowed_path": str(model),
            },
        },
    }
    lock_path = tmp_path / "artifact-lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    manifest_path = tmp_path / "runtime-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "voiceink.runtime.manifest.v1",
                "version": 1,
                "executable_artifact_id": "sidecar",
                "model_artifact_id": "parakeet-tdt-0.6b-v3.oss-align.q8_0",
                "executable": str(executable),
                "model": str(model),
                "backend": "cpu",
                "endpoint": "ephemeral-loopback",
            }
        ),
        encoding="utf-8",
    )
    return manifest_path, lock_path, hashlib.sha256(lock_path.read_bytes()).hexdigest()


def test_runtime_paths_require_all_trusted_configuration_values() -> None:
    with pytest.raises(ConfigurationError, match="VOICEINK_ARTIFACT_LOCK_SHA256"):
        RuntimePaths.from_environment(
            {
                "VOICEINK_RUNTIME_MANIFEST": "manifest.json",
                "VOICEINK_ARTIFACT_LOCK": "lock.json",
            }
        )


def test_build_application_wires_runtime_and_application_service(tmp_path: Path) -> None:
    manifest, artifact_lock, lock_sha256 = _runtime_files(tmp_path)

    application = build_application(
        manifest,
        artifact_lock,
        lock_sha256,
        endpoint="http://127.0.0.1:45678",
    )
    try:
        assert application.capabilities().model_id == "parakeet-tdt-0.6b-v3.oss-align.q8_0"
        assert application.health().status is HealthStatus.STARTING
    finally:
        application.close()

    assert application.health().status is HealthStatus.CLOSED


class _FakeApplication:
    def __init__(self, status: HealthStatus, close_error: Exception | None = None) -> None:
        self._status = status
        self._close_error = close_error

    @property
    def endpoint(self) -> str:
        return "http://127.0.0.1:45678"

    def start(self) -> None:
        return None

    def health(self) -> RuntimeHealth:
        return RuntimeHealth(self._status, "fake health", "cpu")

    def capabilities(self) -> AsrCapabilities:
        return AsrCapabilities("parakeet-tdt-0.6b-v3.oss-align.q8_0", ("cpu",), True)

    def close(self) -> None:
        if self._close_error is not None:
            raise self._close_error


def test_cli_rejects_non_ready_application(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "_build_from_args", lambda _args: _FakeApplication(HealthStatus.FAILED)
    )

    assert cli.main(["--once"]) == 3


def test_cli_reports_cleanup_failure_as_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    application = _FakeApplication(
        HealthStatus.READY,
        RuntimeUnavailableError("cleanup remains pending"),
    )
    monkeypatch.setattr(cli, "_build_from_args", lambda _args: application)

    assert cli.main(["--once"]) == 4
