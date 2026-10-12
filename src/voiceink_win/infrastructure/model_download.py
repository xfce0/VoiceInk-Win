"""Download and remove the pinned Parakeet model artifact."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from urllib.request import Request, urlopen

from voiceink_win.domain import ConfigurationError, ModelMetadata

from .model_metadata import PARAKEET_ARTIFACT_ID, PARAKEET_NAME
from .packaged_runtime import PACKAGE_DESCRIPTOR, TRUSTED_PACKAGE_ARTIFACTS
from .storage_paths import _reject_symlink_components, default_app_data_root

MODEL_DOWNLOAD_TIMEOUT_SECONDS = 30
MODEL_MAX_BYTES = 2 * 1024 * 1024 * 1024
MODEL_RELATIVE_PATH = Path("models") / "parakeet.gguf"
MODEL_DOWNLOAD_URL = (
    "https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3/resolve/"
    "541d1f99c6b0c3cd0b11a95167540bb8edefd82b/parakeet-tdt-0.6b-v3.q8_0.gguf"
)


class ModelDownloadError(RuntimeError):
    """Raised when a model download or deletion cannot be completed."""


class ModelDownloadService:
    """Own the pinned model file used by the package-relative runtime."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        opener: Callable[..., object] = urlopen,
    ) -> None:
        values = environ if environ is not None else os.environ
        self._target = _model_target(values)
        self._artifact = TRUSTED_PACKAGE_ARTIFACTS[PARAKEET_ARTIFACT_ID]
        self._opener = opener
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="model-download")
        self._lock = Lock()
        self._cancel = Event()
        self._response: object | None = None
        self._closed = False
        self._active: Future[Path] | None = None

    @property
    def target(self) -> Path:
        return self._target

    @property
    def is_downloaded(self) -> bool:
        return _matches_hash(self._target, self._artifact["sha256"])

    @property
    def has_file(self) -> bool:
        return self._target.is_file() and not self._target.is_symlink()

    def download(self) -> Future[Path]:
        with self._lock:
            if self._closed:
                raise ModelDownloadError("model download service is closed")
            if self._active is not None and not self._active.done():
                raise ModelDownloadError("a model download is already in progress")
            self._cancel.clear()
            self._active = self._executor.submit(self._download)
            return self._active

    def delete(self) -> None:
        with self._lock:
            if self._active is not None and not self._active.done():
                raise ModelDownloadError("cannot delete a model while it is downloading")
            _ensure_safe_parent(self._target)
            if self._target.is_symlink():
                raise ModelDownloadError("model path cannot be a symbolic link")
            try:
                self._target.unlink(missing_ok=True)
            except OSError as error:
                raise ModelDownloadError("downloaded model could not be deleted") from error

    def metadata(self) -> ModelMetadata:
        return ModelMetadata(
            name=PARAKEET_NAME,
            model_id=PARAKEET_ARTIFACT_ID,
            version=self._artifact["version"],
            backend="cpu",
            trusted=self.is_downloaded,
            available=self.is_downloaded,
            safe_path="<downloaded>/models/parakeet.gguf",
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def cancel(self) -> None:
        with self._lock:
            self._cancel.set()
            response = self._response
        if response is not None:
            response.close()

    def _download(self) -> Path:
        _ensure_safe_parent(self._target)
        self._target.parent.mkdir(parents=True, exist_ok=True)
        _ensure_safe_parent(self._target)
        if self._target.is_symlink():
            raise ModelDownloadError("model path cannot be a symbolic link")
        request = Request(
            MODEL_DOWNLOAD_URL,
            headers={"User-Agent": "VoiceInk-Win model downloader"},
        )
        digest = hashlib.sha256()
        temporary_path: Path | None = None
        try:
            with self._opener(request, timeout=MODEL_DOWNLOAD_TIMEOUT_SECONDS) as response:
                with self._lock:
                    self._response = response
                headers = getattr(response, "headers", {})
                content_length = headers.get("Content-Length")
                if content_length is not None and int(content_length) > MODEL_MAX_BYTES:
                    raise ModelDownloadError("downloaded model exceeds the size limit")
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".parakeet-", suffix=".download", dir=self._target.parent
                )
                temporary_path = Path(temporary_name)
                with os.fdopen(descriptor, "wb") as destination:
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        if self._cancel.is_set():
                            raise ModelDownloadError("model download cancelled")
                        if destination.tell() + len(chunk) > MODEL_MAX_BYTES:
                            raise ModelDownloadError("downloaded model exceeds the size limit")
                        digest.update(chunk)
                        destination.write(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
                with self._lock:
                    self._response = None
            if digest.hexdigest() != self._artifact["sha256"]:
                raise ModelDownloadError(
                    "downloaded model checksum does not match the pinned artifact"
                )
            if self._cancel.is_set():
                raise ModelDownloadError("model download cancelled")
            with self._lock:
                if self._closed or self._cancel.is_set():
                    raise ModelDownloadError("model download cancelled")
                os.replace(temporary_path, self._target)
                temporary_path = None
            return self._target
        except ModelDownloadError:
            raise
        except (OSError, ValueError) as error:
            raise ModelDownloadError("model download failed") from error
        finally:
            with self._lock:
                self._response = None
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _model_target(environ: Mapping[str, str]) -> Path:
    configured_root = environ.get("VOICEINK_PACKAGE_ROOT", "").strip()
    if configured_root and (Path(configured_root) / PACKAGE_DESCRIPTOR).is_file():
        return Path(configured_root) / MODEL_RELATIVE_PATH
    executable_root = Path(sys.executable).resolve().parent
    if (executable_root / PACKAGE_DESCRIPTOR).is_file():
        return executable_root / MODEL_RELATIVE_PATH
    manifest = environ.get("VOICEINK_RUNTIME_MANIFEST", "").strip()
    lock = environ.get("VOICEINK_ARTIFACT_LOCK", "").strip()
    lock_sha256 = environ.get("VOICEINK_ARTIFACT_LOCK_SHA256", "").strip()
    runtime_configuration = (manifest, lock, lock_sha256)
    if any(runtime_configuration):
        if not all(runtime_configuration):
            raise ModelDownloadError("runtime manifest configuration is incomplete")
        try:
            from .runtime_manifest import load_runtime_manifest

            return load_runtime_manifest(Path(manifest), Path(lock), lock_sha256=lock_sha256).model
        except (ConfigurationError, OSError, TypeError, ValueError) as error:
            raise ModelDownloadError("runtime model target is invalid") from error
    return default_app_data_root(dict(environ)) / MODEL_RELATIVE_PATH


def _ensure_safe_parent(path: Path) -> None:
    try:
        _reject_symlink_components(path.parent)
    except OSError as error:
        raise ModelDownloadError("model path cannot be inspected safely") from error


def _matches_hash(path: Path, expected: str) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == expected


__all__ = ["ModelDownloadError", "ModelDownloadService"]
