"""Production composition for the desktop shell and imported-media page."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from inspect import Parameter, signature
from threading import Lock, Thread, current_thread
from typing import Protocol

from voiceink_win.application import (
    HistoryDeletionService,
    PersistenceService,
    ShellController,
    TranscribePageController,
)
from voiceink_win.domain import TranscribeAvailability
from voiceink_win.infrastructure import AudioArtifactStore, SQLitePersistence, VoiceInkPaths

logger = logging.getLogger(__name__)


class DesktopComposition(Protocol):
    @property
    def controller(self) -> ShellController: ...

    @property
    def transcribe_controller(self) -> TranscribePageController: ...

    @property
    def persistence(self) -> PersistenceService: ...

    @property
    def artifact_cleanup(self) -> Callable[[str], None]: ...

    @property
    def history_deletion(self) -> HistoryDeletionService: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class _DesktopComposition:
    controller: ShellController
    transcribe_controller: TranscribePageController
    persistence: PersistenceService
    artifact_cleanup: Callable[[str], None]
    history_deletion: HistoryDeletionService
    _backend: object | None = field(default=None, init=False, repr=False)
    _bootstrap_thread: Thread | None = field(default=None, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _closing: bool = field(default=False, init=False)

    def start(self) -> None:
        thread = Thread(
            target=self._bootstrap_backend,
            name="desktop-imported-media-bootstrap",
            daemon=True,
        )
        with self._lock:
            self._bootstrap_thread = thread
        thread.start()

    def close(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
            bootstrap = self._bootstrap_thread

        if bootstrap is not None and bootstrap is not current_thread():
            bootstrap.join(2.0)
            if bootstrap.is_alive():
                logger.error("desktop backend bootstrap did not stop before close deadline")

        self.transcribe_controller.close(timeout=3.0)
        with self._lock:
            backend = self._backend
            self._backend = None
        if backend is not None:
            self._close_backend_safely(backend)
        try:
            self.history_deletion.close()
        except Exception:
            logger.exception("failed to drain history artifact cleanup")
        try:
            self.persistence.close().result(timeout=3.0)
        except Exception:
            logger.exception("failed to close SQLite persistence")

    def _bootstrap_backend(self) -> None:
        backend = None
        try:
            from voiceink_win.composition import build_application_from_environment
            from voiceink_win.domain import (
                ConfigurationError,
                MissingModelError,
                RuntimeUnavailableError,
            )
            from voiceink_win.infrastructure import WindowsAdapterRequiredError

            backend = _build_backend(build_application_from_environment, self.persistence)
            backend.start()
        except (
            ConfigurationError,
            MissingModelError,
            RuntimeUnavailableError,
            WindowsAdapterRequiredError,
            OSError,
        ) as error:
            if backend is not None:
                self._close_backend_safely(backend)
            logger.warning("imported-media runtime unavailable", extra={"reason": str(error)})
            self.transcribe_controller.mark_unavailable(
                "Imported media transcription is unavailable: runtime prerequisites failed."
            )
            return
        except Exception:
            if backend is not None:
                self._close_backend_safely(backend)
            logger.exception("unexpected imported-media bootstrap failure")
            self.transcribe_controller.mark_unavailable(
                "Imported media transcription is unavailable: startup failed."
            )
            return

        if not backend.imported_media_available:
            self._close_backend_safely(backend)
            self.transcribe_controller.mark_unavailable(
                "Imported media transcription is unavailable: configuration is incomplete."
            )
            return

        with self._lock:
            closing = self._closing
            if not closing:
                self._backend = backend
        if closing or not self.transcribe_controller.attach_backend(backend):
            self._close_backend_safely(backend)

    @staticmethod
    def _close_backend_safely(backend: object) -> None:
        try:
            backend.close()
        except Exception:
            logger.exception("failed to close imported-media runtime")


def _build_backend(builder: Callable[..., object], history_port: PersistenceService) -> object:
    """Support narrow test builders without weakening production history wiring."""
    parameters = signature(builder).parameters
    history_parameter = parameters.get("history_port")
    accepts_keywords = any(
        parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    if history_parameter is not None or accepts_keywords:
        return builder(history_port=history_port)
    return builder()


def build_desktop_composition() -> DesktopComposition:
    """Return promptly and bootstrap imported media outside the Qt thread.

    Recording remains deliberately unavailable: this composition never enables
    microphone or WASAPI paths.
    """
    paths = VoiceInkPaths.default()
    sqlite = SQLitePersistence(paths.database)
    persistence = PersistenceService(sqlite)
    windows_adapter = None
    if os.name == "nt":
        from voiceink_win.infrastructure import NativeWindowsMediaSecurityAdapter

        windows_adapter = NativeWindowsMediaSecurityAdapter()
    artifacts = AudioArtifactStore(paths.audio, windows_adapter=windows_adapter)
    history_deletion = HistoryDeletionService(persistence, artifacts.delete)
    history_deletion.start()
    transcribe_controller = TranscribePageController(
        None,
        history_port=persistence,
        availability=TranscribeAvailability.LOADING,
        unavailable_message="Loading imported-media transcription runtime...",
    )
    composition = _DesktopComposition(
        controller=ShellController.unavailable(),
        transcribe_controller=transcribe_controller,
        persistence=persistence,
        artifact_cleanup=artifacts.delete,
        history_deletion=history_deletion,
    )
    if not _imported_media_environment_present():
        transcribe_controller.mark_unavailable("Imported media runtime is not configured.")
    else:
        composition.start()
    return composition


def _imported_media_environment_present() -> bool:
    names = (
        "VOICEINK_RUNTIME_MANIFEST",
        "VOICEINK_ARTIFACT_LOCK",
        "VOICEINK_ARTIFACT_LOCK_SHA256",
        "VOICEINK_FFMPEG_PATH",
        "VOICEINK_IMPORT_WORKSPACE_ROOT",
        "VOICEINK_IMPORT_ROOTS",
        "VOICEINK_FFMPEG_VERSION",
        "VOICEINK_FFMPEG_PROVENANCE_URL",
        "VOICEINK_FFMPEG_SHA256",
        "VOICEINK_FFMPEG_LICENSE",
    )
    return all(os.environ.get(name, "").strip() for name in names)


__all__ = ["DesktopComposition", "build_desktop_composition"]
