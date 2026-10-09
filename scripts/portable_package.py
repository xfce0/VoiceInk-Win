"""Stage a verified relocatable CPU runtime package for Windows x64."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK_TEMPLATE = ROOT / ".github" / "native-smoke" / "artifact-lock.template.json"
PACKAGE_README = ROOT / "packaging" / "voiceink-shell-windows-x64" / "README.txt"


class PackageBuildError(RuntimeError):
    """Raised when a runtime package cannot be staged safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise PackageBuildError(f"{label} must be a regular file: {path}")
    return path


def _copy_verified(source: Path, destination: Path, expected: str, label: str) -> None:
    source = _regular_file(source, label)
    actual = _sha256(source)
    if actual.lower() != expected.lower():
        raise PackageBuildError(f"{label} checksum mismatch")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _load_lock(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PackageBuildError(f"artifact lock could not be loaded: {path}") from error
    if not isinstance(value, dict) or not isinstance(value.get("artifacts"), dict):
        raise PackageBuildError("artifact lock has no artifacts object")
    return value


def build_package(
    *,
    shell_dist: Path,
    ffmpeg: Path,
    sidecar: Path,
    model: Path,
    output: Path,
    lock_template: Path = DEFAULT_LOCK_TEMPLATE,
) -> Path:
    """Build a package from already downloaded, hash-pinned artifacts."""
    if output.exists():
        raise PackageBuildError(f"refusing to replace existing output: {output}")
    shell_files = {
        name: _regular_file(shell_dist / name, f"shell artifact {name}")
        for name in (
            "voiceink-shell.exe",
            "voiceink-shell-smoke.exe",
        )
    }
    readme = _regular_file(PACKAGE_README, "package README")
    lock = _load_lock(lock_template)
    artifacts = lock["artifacts"]
    assert isinstance(artifacts, dict)
    ffmpeg_lock = artifacts.get("ffmpeg")
    model_candidates = [
        value
        for value in artifacts.values()
        if isinstance(value, dict) and value.get("kind") == "model"
    ]
    if not isinstance(ffmpeg_lock, dict) or ffmpeg_lock.get("kind") != "archive":
        raise PackageBuildError("artifact lock must contain an archive artifact named ffmpeg")
    if len(model_candidates) != 1:
        raise PackageBuildError("artifact lock must contain exactly one model artifact")
    model_lock = model_candidates[0]
    executable_candidates = [
        value
        for value in artifacts.values()
        if isinstance(value, dict)
        and value.get("kind") == "archive"
        and "executable_sha256" in value
    ]
    if len(executable_candidates) != 2:
        raise PackageBuildError("artifact lock must contain FFmpeg and one sidecar archive")
    sidecar_lock = next(value for value in executable_candidates if value is not ffmpeg_lock)
    package_root = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output.name}.", dir=output.parent) as temporary:
        staged = Path(temporary)
        shutil.copy2(shell_files["voiceink-shell.exe"], staged / "voiceink-shell.exe")
        shutil.copy2(shell_files["voiceink-shell-smoke.exe"], staged / "voiceink-shell-smoke.exe")
        shutil.copy2(readme, staged / "README.txt")
        _copy_verified(
            ffmpeg, staged / "tools" / "ffmpeg.exe", ffmpeg_lock["executable_sha256"], "FFmpeg"
        )
        _copy_verified(
            sidecar,
            staged / "runtime" / "nemo-speech.exe",
            sidecar_lock["executable_sha256"],
            "NeMo sidecar",
        )
        _copy_verified(
            model, staged / "models" / "parakeet.gguf", model_lock["sha256"], "Parakeet model"
        )
        (staged / "workspace").mkdir()
        (staged / "imports").mkdir()

        descriptor = {
            "schema": "voiceink.runtime.package.v1",
            "version": 1,
            "executable_artifact_id": next(
                key for key, value in artifacts.items() if value is sidecar_lock
            ),
            "model_artifact_id": next(
                key for key, value in artifacts.items() if value is model_lock
            ),
            "backend": sidecar_lock["backend"],
            "endpoint": "ephemeral-loopback",
            "artifacts": {
                next(key for key, value in artifacts.items() if value is sidecar_lock): {
                    "kind": "executable",
                    "version": sidecar_lock["version"],
                    "provenance_url": sidecar_lock["provenance_url"],
                    "sha256": sidecar_lock["executable_sha256"],
                    "license": sidecar_lock["license"],
                    "path": "runtime/nemo-speech.exe",
                },
                next(key for key, value in artifacts.items() if value is model_lock): {
                    "kind": "model",
                    "version": model_lock["version"],
                    "provenance_url": model_lock["provenance_url"],
                    "sha256": model_lock["sha256"],
                    "license": model_lock["license"],
                    "path": "models/parakeet.gguf",
                },
            },
            "ffmpeg": {
                "version": ffmpeg_lock["version"],
                "provenance_url": ffmpeg_lock["provenance_url"],
                "sha256": ffmpeg_lock["executable_sha256"],
                "license": ffmpeg_lock["license"],
                "path": "tools/ffmpeg.exe",
            },
            "workspace_root": "workspace",
            "import_roots": ["imports"],
        }
        (staged / "voiceink-package.json").write_text(
            json.dumps(descriptor, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (staged / "voiceink-shell.cmd").write_text(
            "@echo off\n"
            'set "VOICEINK_PACKAGE_ROOT=%~dp0"\n'
            'set "VOICEINK_RUNTIME_MANIFEST="\n'
            'set "VOICEINK_ARTIFACT_LOCK="\n'
            'set "VOICEINK_ARTIFACT_LOCK_SHA256="\n'
            'set "VOICEINK_FFMPEG_PATH="\n'
            'set "VOICEINK_IMPORT_WORKSPACE_ROOT="\n'
            'set "VOICEINK_IMPORT_ROOTS="\n'
            '"%~dp0voiceink-shell.exe" %*\n',
            encoding="ascii",
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        staged.rename(output)
    return package_root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shell-dist", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock-template", type=Path, default=DEFAULT_LOCK_TEMPLATE)
    arguments = parser.parse_args()
    try:
        output = build_package(
            shell_dist=arguments.shell_dist,
            ffmpeg=arguments.ffmpeg,
            sidecar=arguments.sidecar,
            model=arguments.model,
            output=arguments.output,
            lock_template=arguments.lock_template,
        )
    except PackageBuildError as error:
        print(f"portable package: {error}")
        return 1
    print(f"Portable package ready: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
