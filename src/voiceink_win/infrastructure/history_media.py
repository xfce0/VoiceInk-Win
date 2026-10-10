"""Windows adapters for retained history media actions."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any


class WindowsHistoryArtifactRevealAdapter:
    """Select one validated artifact in Windows Explorer."""

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        launcher: Callable[[list[str]], Any] | None = None,
    ) -> None:
        self._platform_name = os.name if platform_name is None else platform_name
        self._launcher = launcher or self._launch

    def is_available(self) -> bool:
        return self._platform_name == "nt"

    def reveal(self, resolved_artifact: Path) -> None:
        self._launcher(["explorer.exe", f'/select,"{resolved_artifact}"'])

    @staticmethod
    def _launch(command: list[str]) -> None:
        subprocess.Popen(command, close_fds=True)


class WindowsHistoryAudioPlaybackAdapter:
    """Play one validated WAV through a retained Qt Multimedia player."""

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        player_factory: Callable[[], Any] | None = None,
        audio_output_factory: Callable[[], Any] | None = None,
        url_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._platform_name = os.name if platform_name is None else platform_name
        self._player: Any | None = None
        self._audio_output: Any | None = None
        self._url_factory = url_factory
        if self._platform_name != "nt":
            return
        try:
            if player_factory is None or audio_output_factory is None or url_factory is None:
                from PySide6.QtCore import QCoreApplication, QUrl
                from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

                if QCoreApplication.instance() is None:
                    return
                player_factory = QMediaPlayer
                audio_output_factory = QAudioOutput
                self._url_factory = QUrl.fromLocalFile
            self._player = player_factory()
            self._audio_output = audio_output_factory()
            self._player.setAudioOutput(self._audio_output)
            is_available = getattr(self._player, "isAvailable", None)
            if callable(is_available) and not is_available():
                self._player = None
                self._audio_output = None
        except (ImportError, OSError, RuntimeError):
            self._player = None
            self._audio_output = None

    def is_available(self) -> bool:
        return self._platform_name == "nt" and self._player is not None

    def play(self, resolved_artifact: Path) -> None:
        if not self.is_available() or self._url_factory is None:
            raise RuntimeError("history audio playback is unavailable")
        self._player.setSource(self._url_factory(str(resolved_artifact)))
        self._player.play()

    def close(self) -> None:
        if self._player is not None:
            self._player.stop()
            delete_later = getattr(self._player, "deleteLater", None)
            if delete_later is not None:
                delete_later()
        if self._audio_output is not None:
            delete_later = getattr(self._audio_output, "deleteLater", None)
            if delete_later is not None:
                delete_later()
        self._player = None
        self._audio_output = None


__all__ = [
    "WindowsHistoryArtifactRevealAdapter",
    "WindowsHistoryAudioPlaybackAdapter",
]
