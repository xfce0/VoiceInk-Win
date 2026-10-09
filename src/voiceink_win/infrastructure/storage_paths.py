"""Portable VoiceInk application-data and audio artifact paths."""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from voiceink_win.domain import InvalidSourceError
from voiceink_win.domain.persistence import InvalidAudioArtifactPathError

from .media_snapshot import WindowsAdapterRequiredError, WindowsMediaSecurityAdapter


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
    ) -> None:
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
        target.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(prefix=".voiceink-", dir=target.parent)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "wb") as temporary_file:
                temporary_file.write(bytes(content))
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, target)
        finally:
            temporary_path.unlink(missing_ok=True)
        return target

    def delete(self, relative_path: str) -> None:
        """Remove one validated artifact without following paths outside the store."""
        safe_path = normalise_relative_audio_path(relative_path)
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
