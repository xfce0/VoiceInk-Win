from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from voiceink_win.domain import ConfigurationError
from voiceink_win.infrastructure import load_runtime_manifest


def _configuration(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], Path, Path]:
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
    manifest = {
        "schema": "voiceink.runtime.manifest.v1",
        "version": 1,
        "executable_artifact_id": "sidecar",
        "model_artifact_id": "parakeet-v3",
        "executable": str(executable),
        "model": str(model),
        "backend": "cpu",
        "endpoint": "ephemeral-loopback",
    }
    return manifest, lock, executable, model


def test_loader_resolves_paths_and_reads_hashes_only_from_lock(tmp_path: Path) -> None:
    manifest, lock, executable, model = _configuration(tmp_path)

    loaded = load_runtime_manifest(manifest, lock)

    assert loaded.executable == executable
    assert loaded.model == model
    assert loaded.executable_artifact.sha256 == hashlib.sha256(b"runtime").hexdigest()
    assert loaded.model_artifact.license == "CC-BY-4.0"
    assert "sha256" not in loaded.manifest.__dataclass_fields__


def test_loader_resolves_canonical_paths_when_manifest_selects_only_artifacts(
    tmp_path: Path,
) -> None:
    manifest, lock, executable, model = _configuration(tmp_path)
    manifest.pop("executable")
    manifest.pop("model")

    loaded = load_runtime_manifest(manifest, lock)

    assert loaded.executable == executable
    assert loaded.model == model


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sha256", "not-a-hash"),
        ("provenance_url", "http://example.invalid/runtime"),
        ("allowed_path", "relative/runtime.exe"),
    ],
)
def test_loader_rejects_malformed_lock_values(tmp_path: Path, field: str, value: str) -> None:
    manifest, lock, _, _ = _configuration(tmp_path)
    lock["artifacts"]["sidecar"][field] = value  # type: ignore[index]

    with pytest.raises(ConfigurationError):
        load_runtime_manifest(manifest, lock)


def test_loader_rejects_manifest_lock_path_mismatch(tmp_path: Path) -> None:
    manifest, lock, _, _ = _configuration(tmp_path)
    manifest["executable"] = str(tmp_path / "runtime" / "other.exe")

    with pytest.raises(ConfigurationError, match="allowlist"):
        load_runtime_manifest(manifest, lock)


def test_loader_rejects_escape_and_symlink_paths(tmp_path: Path) -> None:
    manifest, lock, _, model = _configuration(tmp_path)
    lock["artifacts"]["sidecar"]["allowed_path"] = str(tmp_path / "runtime" / ".." / "escape.exe")  # type: ignore[index]
    with pytest.raises(ConfigurationError):
        load_runtime_manifest(manifest, lock)

    target = tmp_path / "target.exe"
    target.write_bytes(b"runtime")
    link = tmp_path / "link.exe"
    link.symlink_to(target)
    lock["artifacts"]["sidecar"]["allowed_path"] = str(link)  # type: ignore[index]
    manifest["executable"] = str(link)
    with pytest.raises(ConfigurationError):
        load_runtime_manifest(manifest, lock)
    assert model.exists()


def test_loader_rejects_unsupported_backend(tmp_path: Path) -> None:
    manifest, lock, _, _ = _configuration(tmp_path)
    manifest["backend"] = "vulkan"

    with pytest.raises(ConfigurationError):
        load_runtime_manifest(manifest, lock)


def test_loader_rejects_changed_artifact_lock_file(tmp_path: Path) -> None:
    manifest, lock, _, _ = _configuration(tmp_path)
    lock_path = tmp_path / "artifact-lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    lock_sha256 = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    lock_path.write_text(lock_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="checksum"):
        load_runtime_manifest(manifest, lock_path, lock_sha256=lock_sha256)
