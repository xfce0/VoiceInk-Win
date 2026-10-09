"""Discover and validate a relocatable VoiceInk runtime package."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from voiceink_win.domain import ConfigurationError

from .ffmpeg import FfmpegArtifactManifest

PACKAGE_DESCRIPTOR = "voiceink-package.json"
PACKAGE_SCHEMA = "voiceink.runtime.package.v1"
TRUSTED_PACKAGE_ARTIFACTS = {
    "nemo-speech-cpp-windows-amd64": {
        "version": "0.2.0",
        "provenance_url": "https://github.com/NVIDIA/NeMo-Speech.cpp/releases/tag/v0.2.0",
        "sha256": "72ed6e35506150dc7edaa0507904688ace62a8d9a47c4976330b556d361492aa",
        "license": "Apache-2.0",
        "path": "runtime/nemo-speech.exe",
    },
    "parakeet-tdt-0.6b-v3.oss-align.q8_0": {
        "version": "541d1f99c6b0c3cd0b11a95167540bb8edefd82b",
        "provenance_url": "https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3/tree/541d1f99c6b0c3cd0b11a95167540bb8edefd82b",
        "sha256": "e3880d0aaaaf2c308ea2c35016b2b895c423eb3fda924c1b463d1c19b7f4d32e",
        "license": "CC-BY-4.0",
        "path": "models/parakeet.gguf",
    },
    "ffmpeg": {
        "version": "7.1.1",
        "provenance_url": "https://github.com/GyanD/codexffmpeg/releases/tag/7.1.1",
        "sha256": "b90225987bdd042cca09a1efb5e34e9848f2d1dbf5fbcd388753a44145522997",
        "license": "GPL-3.0-or-later",
        "path": "tools/ffmpeg.exe",
    },
}


def _invalid(message: str, *, cause: BaseException | None = None) -> ConfigurationError:
    return ConfigurationError(message, cause=cause)


def _read_descriptor(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise _invalid("packaged runtime descriptor could not be loaded", cause=error) from error
    if not isinstance(value, dict):
        raise _invalid("packaged runtime descriptor must contain an object")
    return value


def _relative_file(root: Path, value: object, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"packaged runtime field {field} must be a non-empty path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise _invalid(f"packaged runtime field {field} must stay below package root")
    candidate = root / relative
    if candidate.is_symlink():
        raise _invalid(f"packaged runtime field {field} cannot contain a symlink")
    path = candidate.resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise _invalid(f"packaged runtime field {field} escapes package root") from error
    if not path.is_file():
        raise _invalid(f"packaged runtime field {field} must identify a regular file")
    return path


def _relative_directory(root: Path, value: object, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"packaged runtime field {field} must be a non-empty path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise _invalid(f"packaged runtime field {field} must stay below package root")
    candidate = root / relative
    if candidate.is_symlink():
        raise _invalid(f"packaged runtime field {field} cannot contain a symlink")
    path = candidate.resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise _invalid(f"packaged runtime field {field} escapes package root") from error
    if not path.is_dir():
        raise _invalid(f"packaged runtime field {field} must identify a directory")
    return path


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"packaged runtime field {field} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class PackagedRuntime:
    """Runtime configuration materialized from a package-relative descriptor."""

    root: Path
    manifest: dict[str, object]
    artifact_lock: dict[str, object]
    environment: dict[str, str]


def load_packaged_runtime(
    *,
    root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    trusted_artifacts: Mapping[str, Mapping[str, str]] | None = None,
) -> PackagedRuntime | None:
    values = environ if environ is not None else os.environ
    configured_root = values.get("VOICEINK_PACKAGE_ROOT", "").strip()
    package_root = Path(configured_root) if configured_root else root
    if package_root is None:
        package_root = Path(sys.executable).resolve().parent
    package_root = package_root.resolve()
    descriptor_path = package_root / PACKAGE_DESCRIPTOR
    if not descriptor_path.is_file():
        return None

    descriptor = _read_descriptor(descriptor_path)
    expected = {
        "schema",
        "version",
        "executable_artifact_id",
        "model_artifact_id",
        "backend",
        "endpoint",
        "artifacts",
        "ffmpeg",
        "workspace_root",
        "import_roots",
    }
    if set(descriptor) != expected:
        raise _invalid("packaged runtime descriptor schema is malformed")
    if descriptor["schema"] != PACKAGE_SCHEMA or descriptor["version"] != 1:
        raise _invalid("packaged runtime descriptor schema is unsupported")
    trusted = trusted_artifacts or TRUSTED_PACKAGE_ARTIFACTS

    executable_id = _string(descriptor["executable_artifact_id"], "executable_artifact_id")
    model_id = _string(descriptor["model_artifact_id"], "model_artifact_id")
    artifacts = descriptor["artifacts"]
    if not isinstance(artifacts, Mapping) or set(artifacts) != {executable_id, model_id}:
        raise _invalid("packaged runtime artifacts are malformed")
    if executable_id not in trusted or model_id not in trusted:
        raise _invalid("packaged runtime artifact ID is not trusted")

    lock_artifacts: dict[str, dict[str, object]] = {}
    artifact_paths: dict[str, Path] = {}
    for artifact_id, raw_artifact in artifacts.items():
        if not isinstance(raw_artifact, Mapping):
            raise _invalid("packaged runtime artifact must be an object")
        artifact = dict(raw_artifact)
        expected_keys = {"kind", "version", "provenance_url", "sha256", "license", "path"}
        if set(artifact) != expected_keys:
            raise _invalid("packaged runtime artifact schema is malformed")
        trusted_artifact = trusted.get(str(artifact_id))
        if trusted_artifact is None:
            raise _invalid("packaged runtime artifact ID is not trusted")
        for field in ("version", "provenance_url", "sha256", "license", "path"):
            if artifact[field] != trusted_artifact.get(field):
                raise _invalid(f"packaged runtime artifact {artifact_id} does not match trust root")
        path = _relative_file(package_root, artifact.pop("path"), f"artifacts.{artifact_id}.path")
        artifact_paths[artifact_id] = path
        lock_artifacts[str(artifact_id)] = {
            **artifact,
            "allowed_path": str(path),
        }

    if lock_artifacts[executable_id]["kind"] != "executable":
        raise _invalid("packaged runtime executable artifact kind is invalid")
    if lock_artifacts[model_id]["kind"] != "model":
        raise _invalid("packaged runtime model artifact kind is invalid")

    ffmpeg = descriptor["ffmpeg"]
    if not isinstance(ffmpeg, Mapping):
        raise _invalid("packaged runtime FFmpeg metadata is malformed")
    ffmpeg_value = dict(ffmpeg)
    if set(ffmpeg_value) != {"version", "provenance_url", "sha256", "license", "path"}:
        raise _invalid("packaged runtime FFmpeg metadata is malformed")
    trusted_ffmpeg = trusted.get("ffmpeg")
    if trusted_ffmpeg is None or any(
        ffmpeg_value[field] != trusted_ffmpeg.get(field)
        for field in ("version", "provenance_url", "sha256", "license", "path")
    ):
        raise _invalid("packaged runtime FFmpeg metadata does not match trust root")
    ffmpeg_path = _relative_file(package_root, ffmpeg_value.pop("path"), "ffmpeg.path")
    ffmpeg_manifest = FfmpegArtifactManifest(
        version=_string(ffmpeg_value["version"], "ffmpeg.version"),
        provenance_url=_string(ffmpeg_value["provenance_url"], "ffmpeg.provenance_url"),
        sha256=_string(ffmpeg_value["sha256"], "ffmpeg.sha256"),
        license=_string(ffmpeg_value["license"], "ffmpeg.license"),
        allowed_path=ffmpeg_path,
    )
    workspace = _relative_directory(package_root, descriptor["workspace_root"], "workspace_root")
    import_roots_value = descriptor["import_roots"]
    if not isinstance(import_roots_value, list) or not import_roots_value:
        raise _invalid("packaged runtime import_roots must be a non-empty list")
    import_roots = tuple(
        _relative_directory(package_root, value, "import_roots") for value in import_roots_value
    )

    artifact_lock = {
        "schema": "voiceink.runtime.artifact-lock.v1",
        "version": 1,
        "install_root": str(package_root),
        "artifacts": lock_artifacts,
    }
    manifest = {
        "schema": "voiceink.runtime.manifest.v1",
        "version": 1,
        "executable_artifact_id": executable_id,
        "model_artifact_id": model_id,
        "executable": str(artifact_paths[executable_id]),
        "model": str(artifact_paths[model_id]),
        "backend": _string(descriptor["backend"], "backend"),
        "endpoint": _string(descriptor["endpoint"], "endpoint"),
    }
    environment = {
        "VOICEINK_FFMPEG_PATH": str(ffmpeg_path),
        "VOICEINK_FFMPEG_VERSION": ffmpeg_manifest.version,
        "VOICEINK_FFMPEG_PROVENANCE_URL": ffmpeg_manifest.provenance_url,
        "VOICEINK_FFMPEG_SHA256": ffmpeg_manifest.sha256,
        "VOICEINK_FFMPEG_LICENSE": ffmpeg_manifest.license,
        "VOICEINK_IMPORT_WORKSPACE_ROOT": str(workspace),
        "VOICEINK_IMPORT_ROOTS": os.pathsep.join(str(path) for path in import_roots),
    }
    return PackagedRuntime(package_root, manifest, artifact_lock, environment)


def packaged_runtime_available(*, environ: Mapping[str, str] | None = None) -> bool:
    values = environ if environ is not None else os.environ
    configured_root = values.get("VOICEINK_PACKAGE_ROOT", "").strip()
    root = Path(configured_root) if configured_root else Path(sys.executable).resolve().parent
    return (root / PACKAGE_DESCRIPTOR).is_file()


__all__ = [
    "PACKAGE_DESCRIPTOR",
    "PACKAGE_SCHEMA",
    "TRUSTED_PACKAGE_ARTIFACTS",
    "PackagedRuntime",
    "load_packaged_runtime",
    "packaged_runtime_available",
]
