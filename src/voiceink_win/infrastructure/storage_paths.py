"""Portable VoiceInk application-data and audio artifact paths."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from voiceink_win.domain.persistence import InvalidAudioArtifactPathError


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

    def __init__(self, audio_root: Path) -> None:
        self._root = Path(audio_root).expanduser().resolve(strict=False)

    def resolve(self, relative_path: str) -> Path:
        safe_path = normalise_relative_audio_path(relative_path)
        candidate = (self._root / Path(*safe_path.split("/"))).resolve(strict=False)
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
