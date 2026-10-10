"""Windows adapters for retained history media actions."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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
        self._available = self._platform_name == "nt"
        if self._available and self._explorer_path is None:
            try:
                self._explorer_path = self._system_explorer_path()
                self._available = Path(self._explorer_path).is_file()
            except Exception as error:
                self._available = False
                logger.warning(
                    "history artifact reveal unavailable",
                    extra={
                        "reason_code": "PLATFORM_UNAVAILABLE",
                        "failure_stage": "initialization",
                        "exception_type": type(error).__name__,
                    },
                )

    def is_available(self) -> bool:
        return self._available

    def reveal(self, resolved_artifact: Path) -> None:
        if not self.is_available() or self._explorer_path is None:
            raise RuntimeError("history artifact reveal is unavailable")
        self._launcher([self._explorer_path, f'/select,"{resolved_artifact}"'])

    @staticmethod
    def _system_explorer_path() -> str:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
        if not length:
            raise OSError("could not resolve the Windows directory")
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
        empty_url_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._platform_name = os.name if platform_name is None else platform_name
        self._player: Any | None = None
        self._audio_output: Any | None = None
        self._url_factory = url_factory
        self._empty_url_factory = empty_url_factory
        self._playback_error: RuntimeError | None = None
        self._failure_callback: Callable[[Exception], None] | None = None
        self._play_in_progress = False
        self._playback_request = 0
        self._error_signal: Any | None = None
        self._error_slot: Callable[..., None] | None = None
        self._status_signal: Any | None = None
        self._status_slot: Callable[..., None] | None = None
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
                self._empty_url_factory = QUrl
            self._player = player_factory()
            self._audio_output = audio_output_factory()
            self._player.setAudioOutput(self._audio_output)
            self._error_signal = getattr(self._player, "errorOccurred", None)
            self._status_signal = getattr(self._player, "mediaStatusChanged", None)
            is_available = getattr(self._player, "isAvailable", None)
            if callable(is_available) and not is_available():
                self._player = None
                self._audio_output = None
        except Exception as error:
            logger.warning(
                "history audio adapter unavailable",
                extra={
                    "reason_code": "PLATFORM_UNAVAILABLE",
                    "failure_stage": "initialization",
                    "exception_type": type(error).__name__,
                },
            )
            self._player = None
            self._audio_output = None

    def is_available(self) -> bool:
        return self._platform_name == "nt" and self._player is not None

    def set_failure_callback(self, callback: Callable[[Exception], None] | None) -> None:
        self._failure_callback = callback

    def play(self, resolved_artifact: Path) -> None:
        if not self.is_available() or self._url_factory is None:
            raise RuntimeError("history audio playback is unavailable")
        self._playback_error = None
        self._playback_request += 1
        request = self._playback_request
        if self._error_signal is not None:
            self._disconnect_signal(self._error_signal, self._error_slot)
            self._error_slot = lambda *_args: self._on_playback_error(request)
            self._error_signal.connect(self._error_slot)
        if self._status_signal is not None:
            self._disconnect_signal(self._status_signal, self._status_slot)
            self._status_slot = lambda status: self._on_media_status(request, status)
            self._status_signal.connect(self._status_slot)
        self._play_in_progress = True
        try:
            self._player.setSource(self._url_factory(str(resolved_artifact)))
            self._player.play()
        except Exception:
            self._invalidate_playback()
            raise
        finally:
            self._play_in_progress = False
        if self._playback_error is not None:
            error = self._playback_error
            self._invalidate_playback()
            raise error

    def _on_playback_error(self, request: int) -> None:
        if request != self._playback_request:
            return
        self._playback_error = RuntimeError("history audio playback failed")
        if self._play_in_progress or self._failure_callback is None:
            return
        callback = self._failure_callback
        self._failure_callback = None
        self._disconnect_playback_signals()
        callback(self._playback_error)

    def _on_media_status(self, request: int, status: Any) -> None:
        if request != self._playback_request:
            return
        status_name = getattr(status, "name", str(status))
        if status_name == "EndOfMedia":
            self._playback_request += 1
            self._failure_callback = None
            self._disconnect_playback_signals()

    @staticmethod
    def _disconnect_signal(signal: Any | None, slot: Callable[..., None] | None) -> None:
        if signal is None or slot is None:
            return
        disconnect = getattr(signal, "disconnect", None)
        if callable(disconnect):
            disconnect(slot)

    def _disconnect_playback_signals(self) -> None:
        self._disconnect_signal(self._error_signal, self._error_slot)
        self._disconnect_signal(self._status_signal, self._status_slot)
        self._error_slot = None
        self._status_slot = None

    def _invalidate_playback(self) -> None:
        self._failure_callback = None
        self._playback_request += 1
        self._disconnect_playback_signals()

    def close(self) -> None:
        player = self._player
        audio_output = self._audio_output
        self._player = None
        self._audio_output = None
        self._invalidate_playback()
        if player is not None:
            try:
                if self._empty_url_factory is not None:
                    player.setSource(self._empty_url_factory())
            except Exception:
                logger.warning("failed to reset history audio source during close")
            try:
                player.stop()
            except Exception:
                logger.warning("failed to stop history audio during close")
            try:
                delete_later = getattr(player, "deleteLater", None)
                if delete_later is not None:
                    delete_later()
            except Exception:
                logger.warning("failed to release history audio player during close")
        if audio_output is not None:
            try:
                delete_later = getattr(audio_output, "deleteLater", None)
                if delete_later is not None:
                    delete_later()
            except Exception:
                logger.warning("failed to release history audio output during close")
        self._error_signal = None
        self._error_slot = None
        self._status_signal = None
        self._status_slot = None


__all__ = [
    "WindowsHistoryArtifactRevealAdapter",
    "WindowsHistoryAudioPlaybackAdapter",
]
