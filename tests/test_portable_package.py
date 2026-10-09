from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.portable_package import PackageBuildError, build_package
from voiceink_win.domain import ConfigurationError
from voiceink_win.infrastructure import load_packaged_runtime, load_runtime_manifest


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lock(ffmpeg: bytes, sidecar: bytes, model: bytes) -> dict[str, object]:
    return {
        "schema": "voiceink.native-smoke.artifact-lock.v1",
        "version": 1,
        "artifacts": {
            "ffmpeg": {
                "kind": "archive",
                "executable_sha256": _sha256(ffmpeg),
                "version": "7.1.1",
                "provenance_url": "https://example.invalid/ffmpeg",
                "license": "GPL-3.0-or-later",
            },
            "sidecar": {
                "kind": "archive",
                "executable_sha256": _sha256(sidecar),
                "version": "0.2.0",
                "provenance_url": "https://example.invalid/sidecar",
                "license": "Apache-2.0",
                "backend": "cpu",
            },
            "parakeet": {
                "kind": "model",
                "sha256": _sha256(model),
                "version": "model-1",
                "provenance_url": "https://example.invalid/model",
                "license": "CC-BY-4.0",
            },
        },
    }


def test_build_package_stages_and_discovers_verified_runtime(tmp_path: Path) -> None:
    ffmpeg = b"ffmpeg"
    sidecar = b"sidecar"
    model = b"model"
    shell_dist = tmp_path / "dist"
    shell_dist.mkdir()
    (shell_dist / "voiceink-shell.exe").write_bytes(b"shell")
    (shell_dist / "voiceink-shell-smoke.exe").write_bytes(b"smoke")
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(_lock(ffmpeg, sidecar, model)), encoding="utf-8")
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    sidecar_path = tmp_path / "nemo-speech.exe"
    model_path = tmp_path / "parakeet.gguf"
    ffmpeg_path.write_bytes(ffmpeg)
    sidecar_path.write_bytes(sidecar)
    model_path.write_bytes(model)

    output = build_package(
        shell_dist=shell_dist,
        ffmpeg=ffmpeg_path,
        sidecar=sidecar_path,
        model=model_path,
        output=tmp_path / "release" / "voiceink-shell-windows-x64",
        lock_template=lock_path,
    )

    with pytest.raises(ConfigurationError, match="trusted"):
        load_packaged_runtime(root=output)
    packaged = load_packaged_runtime(
        root=output,
        trusted_artifacts={
            "sidecar": {
                "version": "0.2.0",
                "provenance_url": "https://example.invalid/sidecar",
                "sha256": _sha256(sidecar),
                "license": "Apache-2.0",
                "path": "runtime/nemo-speech.exe",
            },
            "parakeet": {
                "version": "model-1",
                "provenance_url": "https://example.invalid/model",
                "sha256": _sha256(model),
                "license": "CC-BY-4.0",
                "path": "models/parakeet.gguf",
            },
            "ffmpeg": {
                "version": "7.1.1",
                "provenance_url": "https://example.invalid/ffmpeg",
                "sha256": _sha256(ffmpeg),
                "license": "GPL-3.0-or-later",
                "path": "tools/ffmpeg.exe",
            },
        },
    )
    assert packaged is not None
    loaded = load_runtime_manifest(packaged.manifest, packaged.artifact_lock)
    assert loaded.executable == output / "runtime" / "nemo-speech.exe"
    assert loaded.model == output / "models" / "parakeet.gguf"
    assert Path(packaged.environment["VOICEINK_FFMPEG_PATH"]) == output / "tools" / "ffmpeg.exe"
    assert (output / "voiceink-shell.cmd").is_file()


def test_build_package_rejects_changed_pinned_artifact(tmp_path: Path) -> None:
    ffmpeg = b"ffmpeg"
    sidecar = b"sidecar"
    model = b"model"
    shell_dist = tmp_path / "dist"
    shell_dist.mkdir()
    (shell_dist / "voiceink-shell.exe").write_bytes(b"shell")
    (shell_dist / "voiceink-shell-smoke.exe").write_bytes(b"smoke")
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(_lock(ffmpeg, sidecar, model)), encoding="utf-8")
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    sidecar_path = tmp_path / "nemo-speech.exe"
    model_path = tmp_path / "parakeet.gguf"
    ffmpeg_path.write_bytes(b"changed")
    sidecar_path.write_bytes(sidecar)
    model_path.write_bytes(model)

    with pytest.raises(PackageBuildError, match="FFmpeg checksum mismatch"):
        build_package(
            shell_dist=shell_dist,
            ffmpeg=ffmpeg_path,
            sidecar=sidecar_path,
            model=model_path,
            output=tmp_path / "release" / "voiceink-shell-windows-x64",
            lock_template=lock_path,
        )


def test_build_package_copies_sidecar_dll_dependencies(tmp_path: Path) -> None:
    ffmpeg = b"ffmpeg"
    sidecar = b"sidecar"
    model = b"model"
    shell_dist = tmp_path / "dist"
    shell_dist.mkdir()
    (shell_dist / "voiceink-shell.exe").write_bytes(b"shell")
    (shell_dist / "voiceink-shell-smoke.exe").write_bytes(b"smoke")
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(_lock(ffmpeg, sidecar, model)), encoding="utf-8")
    ffmpeg_path = tmp_path / "ffmpeg.exe"
    sidecar_path = tmp_path / "runtime" / "nemo-speech.exe"
    model_path = tmp_path / "parakeet.gguf"
    sidecar_path.parent.mkdir()
    ffmpeg_path.write_bytes(ffmpeg)
    sidecar_path.write_bytes(sidecar)
    (sidecar_path.parent / "ggml.dll").write_bytes(b"runtime")
    model_path.write_bytes(model)

    output = build_package(
        shell_dist=shell_dist,
        ffmpeg=ffmpeg_path,
        sidecar=sidecar_path,
        model=model_path,
        output=tmp_path / "release" / "voiceink-shell-windows-x64",
        lock_template=lock_path,
    )

    assert (output / "runtime" / "nemo-speech.exe").is_file()
    assert (output / "runtime" / "ggml.dll").read_bytes() == b"runtime"
