"""Validated runtime manifest and trusted artifact-lock loading."""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from voiceink_win.domain import ConfigurationError

ARTIFACT_LOCK_SCHEMA = "voiceink.runtime.artifact-lock.v1"
RUNTIME_MANIFEST_SCHEMA = "voiceink.runtime.manifest.v1"
SUPPORTED_BACKENDS = frozenset({"cpu"})


def _invalid(message: str, *, cause: BaseException | None = None) -> ConfigurationError:
    return ConfigurationError(message, cause=cause)


def _read_json(source: Path | str | Mapping[str, Any], label: str) -> dict[str, Any]:
    if isinstance(source, Mapping):
        value = dict(source)
    else:
        try:
            with Path(source).open("r", encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise _invalid(f"{label} could not be loaded", cause=error) from error
    if not isinstance(value, dict):
        raise _invalid(f"{label} must contain a JSON object")
    return value


def _verify_source_digest(source: Path | str | Mapping[str, Any], expected: str | None) -> None:
    if expected is None or isinstance(source, Mapping):
        return
    expected = _sha256(expected, "artifact lock digest")
    digest = hashlib.sha256()
    try:
        with Path(source).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise _invalid("artifact lock could not be hashed", cause=error) from error
    if digest.hexdigest() != expected:
        raise _invalid("artifact lock checksum mismatch")


def _require_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise _invalid(f"{label} schema is malformed")


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"runtime manifest field {field} must be a non-empty string")
    return value


def _version(value: Any, field: str) -> int:
    if type(value) is not int or value != 1:
        raise _invalid(f"runtime manifest field {field} must be version 1")
    return value


def _sha256(value: Any, field: str) -> str:
    value = _string(value, field)
    if len(value) != 64 or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise _invalid(f"runtime manifest field {field} must be a SHA-256 hex digest")
    return value.lower()


def _https_url(value: Any, field: str) -> str:
    value = _string(value, field)
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise _invalid(f"runtime manifest field {field} must be an https URL")
    return value


def _is_windows_path(value: str) -> bool:
    return bool(PureWindowsPath(value).drive or "\\" in value)


def _path_key(value: str) -> str:
    if _is_windows_path(value):
        return ntpath.normcase(ntpath.normpath(value.replace("/", "\\"))).rstrip("\\")
    return os.path.normcase(os.path.abspath(value)).rstrip(os.sep)


def _path_is_absolute(value: str) -> bool:
    return (
        PureWindowsPath(value).is_absolute()
        if _is_windows_path(value)
        else Path(value).is_absolute()
    )


def _validate_path_text(value: Any, field: str) -> Path:
    value = _string(value, field)
    normalized = value.replace("/", "\\") if _is_windows_path(value) else value
    if not _path_is_absolute(value):
        raise _invalid(f"runtime manifest field {field} must be absolute")
    if normalized.startswith(("\\\\", "\\\\?\\", "\\\\.\\")):
        raise _invalid(f"runtime manifest field {field} must not be UNC or device path")
    if any(part == ".." for part in normalized.replace("\\", "/").split("/")):
        raise _invalid(f"runtime manifest field {field} must not escape its root")
    path = Path(value)
    _reject_links(path, field)
    return path


def _reject_links(path: Path, field: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as error:
            raise _invalid(
                f"runtime manifest field {field} cannot be inspected", cause=error
            ) from error
        if stat.S_ISLNK(info.st_mode):
            raise _invalid(f"runtime manifest field {field} cannot contain a symlink")
        if os.name == "nt":
            import ctypes

            attributes = ctypes.windll.kernel32.GetFileAttributesW(str(current))
            if attributes == 0xFFFFFFFF or attributes & 0x400:
                raise _invalid(f"runtime manifest field {field} cannot contain a reparse point")


def _within_root(path: str, root: str, field: str) -> None:
    path_key = _path_key(path)
    root_key = _path_key(root)
    if path_key == root_key:
        raise _invalid(
            f"runtime manifest field {field} must identify an artifact below install_root"
        )
    prefix = root_key + ("\\" if _is_windows_path(root) else os.sep)
    if not path_key.startswith(prefix):
        raise _invalid(f"runtime manifest field {field} is outside install_root")


@dataclass(frozen=True, slots=True)
class ArtifactLockEntry:
    artifact_id: str
    kind: str
    version: str
    provenance_url: str
    sha256: str
    license: str
    allowed_path: Path

    @classmethod
    def from_json(cls, artifact_id: str, value: Any) -> ArtifactLockEntry:
        if not isinstance(value, Mapping):
            raise _invalid("artifact-lock entry must be an object")
        _require_keys(
            value,
            {"kind", "version", "provenance_url", "sha256", "license", "allowed_path"},
            "artifact-lock entry",
        )
        kind = _string(value["kind"], "artifact kind")
        if kind not in {"executable", "model"}:
            raise _invalid("artifact-lock kind is unsupported")
        return cls(
            _string(artifact_id, "artifact ID"),
            kind,
            _string(value["version"], "artifact version"),
            _https_url(value["provenance_url"], "provenance_url"),
            _sha256(value["sha256"], "sha256"),
            _string(value["license"], "license"),
            _validate_path_text(value["allowed_path"], "allowed_path"),
        )


@dataclass(frozen=True, slots=True)
class ArtifactLock:
    install_root: Path
    artifacts: dict[str, ArtifactLockEntry]

    @classmethod
    def from_json(cls, source: Path | str | Mapping[str, Any]) -> ArtifactLock:
        value = _read_json(source, "artifact lock")
        _require_keys(value, {"schema", "version", "install_root", "artifacts"}, "artifact-lock")
        if value["schema"] != ARTIFACT_LOCK_SCHEMA:
            raise _invalid("artifact-lock schema is unsupported")
        _version(value["version"], "version")
        install_root = _validate_path_text(value["install_root"], "install_root")
        artifacts_value = value["artifacts"]
        if not isinstance(artifacts_value, Mapping) or not artifacts_value:
            raise _invalid("artifact-lock artifacts must be a non-empty object")
        artifacts = {
            _string(artifact_id, "artifact ID"): ArtifactLockEntry.from_json(artifact_id, entry)
            for artifact_id, entry in artifacts_value.items()
        }
        for entry in artifacts.values():
            _within_root(str(entry.allowed_path), str(install_root), "allowed_path")
        return cls(install_root, artifacts)


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    executable_artifact_id: str
    model_artifact_id: str
    executable: Path | None
    model: Path | None
    backend: str
    endpoint: str

    @classmethod
    def from_json(cls, source: Path | str | Mapping[str, Any]) -> RuntimeManifest:
        value = _read_json(source, "runtime manifest")
        required_keys = {
            "schema",
            "version",
            "executable_artifact_id",
            "model_artifact_id",
            "backend",
            "endpoint",
        }
        path_keys = {"executable", "model"}
        if set(value) not in (required_keys, required_keys | path_keys):
            raise _invalid("runtime manifest schema is malformed")
        if value["schema"] != RUNTIME_MANIFEST_SCHEMA:
            raise _invalid("runtime manifest schema is unsupported")
        _version(value["version"], "version")
        backend = _string(value["backend"], "backend")
        if backend != "cpu" and not (backend.startswith("cuda:") and backend[6:].isdigit()):
            raise _invalid("runtime manifest backend is unsupported")
        endpoint = _string(value["endpoint"], "endpoint")
        if endpoint != "ephemeral-loopback":
            raise _invalid("runtime manifest endpoint is unsupported")
        return cls(
            _string(value["executable_artifact_id"], "executable_artifact_id"),
            _string(value["model_artifact_id"], "model_artifact_id"),
            _validate_path_text(value["executable"], "executable")
            if "executable" in value
            else None,
            _validate_path_text(value["model"], "model") if "model" in value else None,
            backend,
            endpoint,
        )


@dataclass(frozen=True, slots=True)
class LoadedRuntimeManifest:
    manifest: RuntimeManifest
    lock: ArtifactLock
    executable_artifact: ArtifactLockEntry
    model_artifact: ArtifactLockEntry

    @property
    def executable(self) -> Path:
        return self.manifest.executable or self.executable_artifact.allowed_path

    @property
    def model(self) -> Path:
        return self.manifest.model or self.model_artifact.allowed_path

    @property
    def backend(self) -> str:
        return self.manifest.backend

    @property
    def model_id(self) -> str:
        return self.manifest.model_artifact_id


class RuntimeManifestLoader:
    """Load a manifest while keeping all trust data in the artifact lock."""

    def load(
        self,
        manifest: Path | str | Mapping[str, Any],
        lock: ArtifactLock | Path | str | Mapping[str, Any],
        *,
        lock_sha256: str | None = None,
    ) -> LoadedRuntimeManifest:
        if not isinstance(lock, ArtifactLock):
            _verify_source_digest(lock, lock_sha256)
        artifact_lock = lock if isinstance(lock, ArtifactLock) else ArtifactLock.from_json(lock)
        runtime_manifest = RuntimeManifest.from_json(manifest)
        try:
            executable_artifact = artifact_lock.artifacts[runtime_manifest.executable_artifact_id]
            model_artifact = artifact_lock.artifacts[runtime_manifest.model_artifact_id]
        except KeyError as error:
            raise _invalid("runtime manifest references an unknown artifact ID") from error
        if executable_artifact.kind != "executable" or model_artifact.kind != "model":
            raise _invalid("runtime manifest artifact kinds do not match their selected roles")
        if runtime_manifest.executable is not None and _path_key(
            str(runtime_manifest.executable)
        ) != _path_key(str(executable_artifact.allowed_path)):
            raise _invalid("runtime executable does not match the artifact-lock allowlist")
        if runtime_manifest.model is not None and _path_key(
            str(runtime_manifest.model)
        ) != _path_key(str(model_artifact.allowed_path)):
            raise _invalid("runtime model does not match the artifact-lock allowlist")
        return LoadedRuntimeManifest(
            runtime_manifest, artifact_lock, executable_artifact, model_artifact
        )


def load_artifact_lock(source: Path | str | Mapping[str, Any]) -> ArtifactLock:
    return ArtifactLock.from_json(source)


def load_runtime_manifest(
    manifest: Path | str | Mapping[str, Any],
    lock: ArtifactLock | Path | str | Mapping[str, Any],
    *,
    lock_sha256: str | None = None,
) -> LoadedRuntimeManifest:
    return RuntimeManifestLoader().load(manifest, lock, lock_sha256=lock_sha256)


def load_runtime_configuration(
    manifest: Path | str | Mapping[str, Any],
    lock: ArtifactLock | Path | str | Mapping[str, Any],
    *,
    lock_sha256: str | None = None,
) -> LoadedRuntimeManifest:
    """Alias used by composition roots that load the complete configuration."""

    return load_runtime_manifest(manifest, lock, lock_sha256=lock_sha256)
