from __future__ import annotations

import hashlib
import json
from pathlib import Path

from voiceink_win.infrastructure import discover_model_metadata


def _runtime_environment(tmp_path: Path) -> dict[str, str]:
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
                "version": "runtime-1",
                "provenance_url": "https://example.invalid/runtime",
                "sha256": hashlib.sha256(b"runtime").hexdigest(),
                "license": "Apache-2.0",
                "allowed_path": str(executable),
            },
            "parakeet-v3": {
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
                "model_artifact_id": "parakeet-v3",
                "executable": str(executable),
                "model": str(model),
                "backend": "cpu",
                "endpoint": "ephemeral-loopback",
            }
        ),
        encoding="utf-8",
    )
    return {
        "VOICEINK_RUNTIME_MANIFEST": str(manifest_path),
        "VOICEINK_ARTIFACT_LOCK": str(lock_path),
        "VOICEINK_ARTIFACT_LOCK_SHA256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
    }


def test_discovery_uses_validated_runtime_metadata_and_redacts_paths(tmp_path: Path) -> None:
    environment = _runtime_environment(tmp_path)

    metadata = discover_model_metadata(environ=environment)

    assert metadata.name == "Parakeet TDT 0.6B V3"
    assert metadata.model_id == "parakeet-v3"
    assert metadata.version == "model-1"
    assert metadata.backend == "cpu"
    assert metadata.trusted is True
    assert metadata.available is True
    assert metadata.safe_path == "<runtime>/models/parakeet.gguf"
    assert str(tmp_path) not in metadata.safe_path


def test_discovery_reports_unavailable_when_package_and_runtime_are_absent() -> None:
    metadata = discover_model_metadata(environ={"VOICEINK_PACKAGE_ROOT": "/missing/package"})

    assert metadata.trusted is False
    assert metadata.available is False
    assert metadata.safe_path == "<runtime unavailable>"


def test_discovery_keeps_trust_but_reports_missing_runtime_artifact(tmp_path: Path) -> None:
    environment = _runtime_environment(tmp_path)
    (tmp_path / "models" / "parakeet.gguf").unlink()

    metadata = discover_model_metadata(environ=environment)

    assert metadata.trusted is True
    assert metadata.available is False
    assert metadata.safe_path == "<runtime>/models/parakeet.gguf"
