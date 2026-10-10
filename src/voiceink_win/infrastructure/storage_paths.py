"""Portable VoiceInk application-data and audio artifact paths."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import Lock

from voiceink_win.domain import CanonicalAudio, InvalidSourceError
from voiceink_win.domain.persistence import AudioArtifactQuotaError, InvalidAudioArtifactPathError

from .media_snapshot import WindowsAdapterRequiredError, WindowsMediaSecurityAdapter

DEFAULT_AUDIO_ARTIFACT_MAX_BYTES = 64 * 1024 * 1024 + 44
DEFAULT_AUDIO_ARTIFACT_QUOTA_BYTES = 512 * 1024 * 1024


def default_app_data_root(environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    local_app_data = values.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "VoiceInk"
    return Path.home() / "AppData" / "Local" / "VoiceInk"


@dataclass(frozen=True, slots=True)
class VoiceInkPaths:
    root: Path
    database: Path
    audio: Path

    @classmethod
    def from_root(cls, root: Path) -> VoiceInkPaths:
        resolved = Path(root).expanduser().resolve(strict=False)
        return cls(resolved, resolved / "voiceink.sqlite3", resolved / "audio")

    @classmethod
    def default(cls, environ: dict[str, str] | None = None) -> VoiceInkPaths:
        return cls.from_root(default_app_data_root(environ))


def normalise_relative_audio_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise InvalidAudioArtifactPathError("audio artifact path must be non-empty text")
    if "\\" in value:
        raise InvalidAudioArtifactPathError("audio artifact paths must use portable '/' separators")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise InvalidAudioArtifactPathError("audio artifact path must be relative")
    if any(part in ("", ".", "..") for part in posix.parts):
        raise InvalidAudioArtifactPathError("audio artifact path contains an unsafe segment")
    return posix.as_posix()


class AudioArtifactStore:
    """Store optional audio outside SQLite using atomic replacement."""

    def __init__(
        self,
        audio_root: Path,
        *,
        windows_adapter: WindowsMediaSecurityAdapter | None = None,
        max_artifact_bytes: int = DEFAULT_AUDIO_ARTIFACT_MAX_BYTES,
        quota_bytes: int = DEFAULT_AUDIO_ARTIFACT_QUOTA_BYTES,
    ) -> None:
        if max_artifact_bytes < 1 or quota_bytes < 1:
            raise ValueError("audio artifact limits are invalid")
        requested = Path(audio_root).expanduser().absolute()
        if os.name == "nt" and windows_adapter is None:
            raise WindowsAdapterRequiredError(
                "safe Windows artifact deletion requires a native security adapter"
            )
        _reject_symlink_components(requested)
        requested.mkdir(parents=True, exist_ok=True)
        _reject_symlink_components(requested)
        if os.name != "nt":
            requested.chmod(0o700)
        self._root = requested.resolve(strict=True)
        self._windows_adapter = windows_adapter
        self._max_artifact_bytes = max_artifact_bytes
        self._quota_bytes = quota_bytes
        self._lock = Lock()

    @property
    def folder_path(self) -> Path:
        """Return the validated app-owned folder for future folder actions."""
        return self._root

    def resolve(self, relative_path: str) -> Path:
        safe_path = normalise_relative_audio_path(relative_path)
        lexical = self._root / Path(*safe_path.split("/"))
        _reject_symlink_components(lexical)
        candidate = lexical.resolve(strict=False)
        try:
            candidate.relative_to(self._root)
        except ValueError as error:
            raise InvalidAudioArtifactPathError(
                "audio artifact path escapes the audio root"
            ) from error
        return candidate

    def write(self, relative_path: str, content: bytes | bytearray | memoryview) -> Path:
        target = self.resolve(relative_path)
        payload = bytes(content)
        if len(payload) > self._max_artifact_bytes:
            raise AudioArtifactQuotaError("audio artifact exceeds the per-file quota")
        with self._lock:
            return self._write_bytes_locked(target, payload)

    def save_normalized_audio(self, audio: CanonicalAudio, artifact_id: str) -> str:
        """Write a deterministic, normalized WAV copy without source-path metadata."""
        if not isinstance(audio, CanonicalAudio):
            raise TypeError("audio artifact must be CanonicalAudio")
        if not isinstance(artifact_id, str) or not artifact_id.strip():
            raise ValueError("audio artifact ID must not be empty")
        relative_path = f"history/{hashlib.sha256(artifact_id.encode('utf-8')).hexdigest()}.wav"
        target = self.resolve(relative_path)
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=".voiceink-", suffix=".wav", dir=target.parent
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(file_descriptor, "w+b") as temporary_file:
                    with wave.open(temporary_file, "wb") as wav_file:
                        wav_file.setnchannels(audio.channels)
                        wav_file.setsampwidth(2)
                        wav_file.setframerate(audio.sample_rate)
                        wav_file.writeframes(audio.pcm16le)
                    temporary_file.flush()
                    os.fsync(temporary_file.fileno())
                self._replace_with_quota(temporary_path, target)
            finally:
                temporary_path.unlink(missing_ok=True)
        return relative_path

    def reveal_path(self, relative_path: str) -> Path:
        """Return an existing regular artifact path after revalidating its identity."""
        target = self.resolve(relative_path)
        try:
            info = target.lstat()
        except FileNotFoundError:
            raise
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InvalidAudioArtifactPathError("audio artifact must be a single-link regular file")
        return target

    def _write_bytes_locked(self, target: Path, payload: bytes) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(prefix=".voiceink-", dir=target.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as temporary_file:
                temporary_file.write(payload)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            self._replace_with_quota(temporary_path, target)
        finally:
            temporary_path.unlink(missing_ok=True)
        return target

    def _replace_with_quota(self, temporary_path: Path, target: Path) -> None:
        size = temporary_path.stat().st_size
        if size > self._max_artifact_bytes:
            raise AudioArtifactQuotaError("audio artifact exceeds the per-file quota")
        existing_size = _regular_file_size(target)
        current_size = _stored_regular_bytes(self._root) - size
        if current_size - existing_size + size > self._quota_bytes:
            raise AudioArtifactQuotaError("audio artifact storage quota exceeded")
        os.replace(temporary_path, target)

    def delete(self, relative_path: str) -> None:
        """Remove one validated artifact without following paths outside the store."""
        safe_path = normalise_relative_audio_path(relative_path)
        with self._lock:
            if self._windows_adapter is not None:
                try:
                    self._windows_adapter.delete_artifact(self._root, safe_path)
                except InvalidSourceError as error:
                    raise InvalidAudioArtifactPathError(str(error)) from error
                return
            self._delete_posix(safe_path)

    def _delete_posix(self, relative_path: str) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(self._root, flags)
        parent_fd = root_fd
        opened: list[int] = []
        try:
            parts = relative_path.split("/")
            for part in parts[:-1]:
                child_fd = os.open(part, flags, dir_fd=parent_fd)
                opened.append(child_fd)
                parent_fd = child_fd
            target = parts[-1]
            before = os.stat(target, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise InvalidAudioArtifactPathError(
                    "audio artifact must be a single-link regular file"
                )
            descriptor = os.open(
                target,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            try:
                after = os.fstat(descriptor)
                if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    raise InvalidAudioArtifactPathError("audio artifact identity changed")
            finally:
                os.close(descriptor)
            os.unlink(target, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)
            os.close(root_fd)


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise InvalidAudioArtifactPathError("audio storage path contains a symlink")


def _regular_file_size(path: Path) -> int:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise InvalidAudioArtifactPathError("audio artifact must be a single-link regular file")
    return info.st_size


def _stored_regular_bytes(root: Path) -> int:
    total = 0
    for directory, _, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(directory) / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                total += info.st_size
    return total
