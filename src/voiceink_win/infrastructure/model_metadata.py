"""Discover trusted Parakeet metadata without starting or downloading a runtime."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from voiceink_win.domain import ConfigurationError, ModelMetadata

from .packaged_runtime import TRUSTED_PACKAGE_ARTIFACTS, load_packaged_runtime
from .runtime_manifest import LoadedRuntimeManifest, load_runtime_manifest

PARAKEET_ARTIFACT_ID = "parakeet-tdt-0.6b-v3.oss-align.q8_0"
PARAKEET_NAME = "Parakeet TDT 0.6B V3"
UNAVAILABLE_PATH = "<runtime unavailable>"


def unavailable_model_metadata() -> ModelMetadata:
    """Return stable model identity while keeping unavailable state explicit."""
    artifact = TRUSTED_PACKAGE_ARTIFACTS[PARAKEET_ARTIFACT_ID]
    return ModelMetadata(
        name=PARAKEET_NAME,
        model_id=PARAKEET_ARTIFACT_ID,
        version=artifact["version"],
        backend="cpu",
        trusted=False,
        available=False,
        safe_path=UNAVAILABLE_PATH,
    )


def discover_model_metadata(*, environ: Mapping[str, str] | None = None) -> ModelMetadata:
    """Read existing package/runtime metadata and return only safe display values."""
    values = environ if environ is not None else os.environ
    runtime_names = (
        "VOICEINK_RUNTIME_MANIFEST",
        "VOICEINK_ARTIFACT_LOCK",
        "VOICEINK_ARTIFACT_LOCK_SHA256",
    )
    try:
        package_root_configured = values.get("VOICEINK_PACKAGE_ROOT", "").strip()
        packaged = None
        if package_root_configured or not all(
            values.get(name, "").strip() for name in runtime_names
        ):
            packaged = load_packaged_runtime(environ=values)
        if packaged is not None:
            loaded = load_runtime_manifest(packaged.manifest, packaged.artifact_lock)
            return _metadata_from_loaded(loaded, label="package")

        if not all(values.get(name, "").strip() for name in runtime_names):
            return unavailable_model_metadata()
        loaded = load_runtime_manifest(
            Path(values["VOICEINK_RUNTIME_MANIFEST"]),
            Path(values["VOICEINK_ARTIFACT_LOCK"]),
            lock_sha256=values["VOICEINK_ARTIFACT_LOCK_SHA256"],
        )
        return _metadata_from_loaded(loaded, label="runtime")
    except (ConfigurationError, OSError, TypeError, ValueError, KeyError):
        return unavailable_model_metadata()


def _metadata_from_loaded(loaded: LoadedRuntimeManifest, *, label: str) -> ModelMetadata:
    model_artifact = loaded.model_artifact
    return ModelMetadata(
        name=PARAKEET_NAME,
        model_id=loaded.model_id,
        version=model_artifact.version,
        backend=loaded.backend,
        trusted=True,
        available=loaded.executable.is_file() and loaded.model.is_file(),
        safe_path=_redact_path(loaded.model, loaded.lock.install_root, label),
    )


def _redact_path(path: Path, root: Path, label: str) -> str:
    try:
        relative = path.resolve().relative_to(root.resolve())
        return f"<{label}>/{relative.as_posix()}"
    except (OSError, ValueError):
        return f"<{label}>/{path.name or 'model'}"


__all__ = ["discover_model_metadata", "unavailable_model_metadata"]
