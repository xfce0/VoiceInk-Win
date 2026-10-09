from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

import scripts.windows_release_package as release_package
from scripts.windows_release_package import (
    WindowsReleasePackageError,
    _download_verified,
    _extract_archive,
    build_release_package,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "windows-release-package.yml"
RUNNER = ROOT / "scripts" / "windows_release_package.py"


def test_windows_release_workflow_uses_tracked_pins_and_publishes_bundle() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "runs-on: windows-2022" in workflow
    assert "architecture: x64" in workflow
    assert "make windows-release-smoke" in workflow
    assert ".github/native-smoke/artifact-lock.template.json" in makefile
    assert "scripts/windows_release_package.py" in makefile
    assert "voiceink-shell-windows-x64.zip" in workflow
    assert "windows-release-smoke.json" in workflow
    assert "actions/upload-artifact@65462800fd760344b1a7b4382951275a0abb4808" in workflow
    assert "secrets." not in workflow
    assert "vars." not in workflow


def test_windows_release_runner_owns_lock_download_and_relocation_contract() -> None:
    source = RUNNER.read_text(encoding="utf-8")

    assert "load_native_smoke_lock(lock_path)" in source
    assert "archive_sha256" in source
    assert "executable_sha256" in source
    assert 'model["sha256"]' in source
    assert "voiceink release smoke " in source
    assert "voiceink-shell-smoke.exe" in source
    assert '"--package-smoke"' in source
    assert "build_package(" in source
    assert "make_archive" in source


def test_download_verified_rejects_changed_file(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"pinned bytes")

    with pytest.raises(WindowsReleasePackageError, match="checksum mismatch"):
        _download_verified(
            source.as_uri(),
            hashlib.sha256(b"different bytes").hexdigest(),
            tmp_path / "download.bin",
            "fixture",
        )


def test_extract_archive_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with ZipFile(archive, "w") as source:
        source.writestr("../outside.exe", b"unsafe")

    with pytest.raises(WindowsReleasePackageError, match="unsafe path"):
        _extract_archive(archive, tmp_path / "extract")
    assert not (tmp_path / "outside.exe").exists()


def test_build_release_package_publishes_after_relocation_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg = b"ffmpeg"
    sidecar = b"sidecar"
    model = b"model"

    def digest(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    lock = {
        "schema": "voiceink.native-smoke.artifact-lock.v1",
        "version": 1,
        "artifacts": {
            "ffmpeg": {
                "kind": "archive",
                "url": "https://example.invalid/ffmpeg.zip",
                "archive_sha256": digest(b"ffmpeg archive"),
                "executable_sha256": digest(ffmpeg),
                "version": "7.1.1",
                "provenance_url": "https://example.invalid/ffmpeg",
                "license": "GPL-3.0-or-later",
            },
            "nemo-speech-cpp-windows-amd64": {
                "kind": "archive",
                "url": "https://example.invalid/sidecar.zip",
                "archive_sha256": digest(b"sidecar archive"),
                "executable_sha256": digest(sidecar),
                "version": "0.2.0",
                "provenance_url": "https://example.invalid/sidecar",
                "license": "Apache-2.0",
                "backend": "cpu",
            },
            "parakeet-tdt-0.6b-v3.oss-align.q8_0": {
                "kind": "model",
                "url": "https://example.invalid/model.gguf",
                "sha256": digest(model),
                "model_id": "parakeet-tdt-0.6b-v3.oss-align.q8_0",
                "version": "model-1",
                "provenance_url": "https://example.invalid/model",
                "license": "CC-BY-4.0",
            },
            "fixture": {
                "kind": "fixture",
                "url": "https://example.invalid/fixture.wav",
                "sha256": digest(b"fixture"),
                "provenance_url": "https://example.invalid/fixture",
                "license": "CC-BY-4.0",
            },
        },
    }
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    shell_dist = tmp_path / "dist"
    shell_dist.mkdir()
    (shell_dist / "voiceink-shell.exe").write_bytes(b"shell")
    (shell_dist / "voiceink-shell-smoke.exe").write_bytes(b"smoke")
    sources = {
        "FFmpeg archive": tmp_path / "ffmpeg.exe",
        "sidecar archive": tmp_path / "sidecar.exe",
        "Parakeet model": tmp_path / "model.gguf",
    }
    sources["FFmpeg archive"].write_bytes(ffmpeg)
    sources["sidecar archive"].write_bytes(sidecar)
    sources["Parakeet model"].write_bytes(model)

    monkeypatch.setattr(
        release_package,
        "_download_verified",
        lambda _url, _expected, _destination, label: sources[label],
    )
    monkeypatch.setattr(
        release_package,
        "_extract_verified_executable",
        lambda _archive, _destination, _name, _expected, label: sources[
            "FFmpeg archive" if label == "FFmpeg" else "sidecar archive"
        ],
    )
    monkeypatch.setattr(release_package, "_run_relocation_smoke", lambda _package: None)

    output = tmp_path / "release" / "voiceink-shell-windows-x64"
    bundle = tmp_path / "release" / "voiceink-shell-windows-x64.zip"
    report = tmp_path / "release" / "windows-release-smoke.json"
    build_release_package(
        lock_path=lock_path,
        shell_dist=shell_dist,
        output=output,
        bundle=bundle,
        report=report,
    )

    assert output.is_dir()
    assert (output / "runtime" / "nemo-speech.exe").is_file()
    assert bundle.is_file()
    assert '"status": "passed"' in report.read_text(encoding="utf-8")
