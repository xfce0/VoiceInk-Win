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
        explorer_path: str | None = None,
    ) -> None:
        self._platform_name = os.name if platform_name is None else platform_name
        self._launcher = launcher or self._launch
        self._explorer_path = explorer_path

    def is_available(self) -> bool:
        return self._platform_name == "nt"

    def reveal(self, resolved_artifact: Path) -> None:
        self._launcher(
            [self._explorer_path or self._system_explorer_path(), f'/select,"{resolved_artifact}"']
        )

    @staticmethod
    def _system_explorer_path() -> str:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
        if not length:
            raise OSError("could not resolve the Windows system directory")
        return str(Path(buffer.value) / "explorer.exe")

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
        self._playback_error: RuntimeError | None = None
        self._failure_callback: Callable[[Exception], None] | None = None
        self._play_in_progress = False
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
            error_signal = getattr(self._player, "errorOccurred", None)
            if error_signal is not None:
                error_signal.connect(self._on_playback_error)
            is_available = getattr(self._player, "isAvailable", None)
            if callable(is_available) and not is_available():
                self._player = None
                self._audio_output = None
        except Exception:
            self._player = None
            self._audio_output = None

    def is_available(self) -> bool:
        return self._platform_name == "nt" and self._player is not None

    def play(
        self,
        resolved_artifact: Path,
        on_failure: Callable[[Exception], None] | None = None,
    ) -> None:
        if not self.is_available() or self._url_factory is None:
            raise RuntimeError("history audio playback is unavailable")
        self._playback_error = None
        self._failure_callback = on_failure
        self._play_in_progress = True
        try:
            self._player.setSource(self._url_factory(str(resolved_artifact)))
            self._player.play()
        finally:
            self._play_in_progress = False
        if self._playback_error is not None:
            raise self._playback_error
        playback_state = getattr(self._player, "playbackState", None)
        if callable(playback_state):
            state = playback_state()
            state_name = getattr(state, "name", str(state))
            if state_name not in {"PlayingState", "Playing"}:
                raise RuntimeError("history audio playback did not start")

    def _on_playback_error(self, *_args: Any) -> None:
        self._playback_error = RuntimeError("history audio playback failed")
        if self._play_in_progress or self._failure_callback is None:
            return
        self._failure_callback(self._playback_error)

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
        self._failure_callback = None


__all__ = [
    "WindowsHistoryArtifactRevealAdapter",
    "WindowsHistoryAudioPlaybackAdapter",
]
