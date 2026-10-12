"""Production composition for the desktop shell and imported-media page."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from inspect import Parameter, signature
from threading import Lock, Thread, current_thread
from typing import Protocol

from voiceink_win.application import (
    GlobalHotkeySettingsService,
    GlobalToggleShortcutService,
    HistoryDeletionService,
    HistoryMediaActionService,
    MicrophoneRecorderController,
    PersistenceService,
    ShellController,
    TranscribePageController,
)
from voiceink_win.domain import TranscribeAvailability
from voiceink_win.infrastructure import (
    AudioArtifactStore,
    SQLitePersistence,
    VoiceInkPaths,
    WindowsHistoryArtifactRevealAdapter,
    WindowsHistoryAudioPlaybackAdapter,
    create_global_shortcut_port,
    packaged_runtime_available,
)

logger = logging.getLogger(__name__)


class DesktopComposition(Protocol):
    @property
    def controller(self) -> ShellController: ...

    @property
    def recorder(self) -> MicrophoneRecorderController: ...

    @property
    def transcribe_controller(self) -> TranscribePageController: ...

    @property
    def persistence(self) -> PersistenceService: ...

    @property
    def artifact_cleanup(self) -> Callable[[str], None]: ...

    @property
    def media_actions(self) -> HistoryMediaActionService: ...

    @property
    def history_deletion(self) -> HistoryDeletionService: ...

    @property
    def global_shortcut(self) -> GlobalToggleShortcutService: ...

    @property
    def global_hotkey_settings(self) -> GlobalHotkeySettingsService: ...

    def close(self) -> None: ...

    def reload_backend(self) -> Future[bool]: ...

    def unload_backend(self) -> Future[bool]: ...


@dataclass(slots=True)
class _DesktopComposition:
    controller: ShellController
    recorder: MicrophoneRecorderController
    transcribe_controller: TranscribePageController
    persistence: PersistenceService
    artifact_cleanup: Callable[[str], None]
    audio_artifact_port: object
    media_actions: HistoryMediaActionService
    history_deletion: HistoryDeletionService
    global_shortcut: GlobalToggleShortcutService
    global_hotkey_settings: GlobalHotkeySettingsService
    readiness_timeout: float | None = field(default=None, repr=False)
    _backend: object | None = field(default=None, init=False, repr=False)
    _cleanup_pending: object | None = field(default=None, init=False, repr=False)
    _bootstrap_thread: Thread | None = field(default=None, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _closing: bool = field(default=False, init=False)
    _lifecycle_active: bool = field(default=False, init=False, repr=False)

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

        try:
            self.global_hotkey_settings.close()
        except Exception:
            logger.exception("failed to unregister global shortcut")
        self.transcribe_controller.close(timeout=3.0)
        self.recorder.close(3.0)
        with self._lock:
            backend = self._backend
            self._backend = None
        if backend is not None:
            self._close_backend_or_track(backend)
        try:
            self.media_actions.close()
        except Exception:
            logger.exception("failed to close history media adapters")
        try:
            self.history_deletion.close()
        except Exception:
            logger.exception("failed to drain history artifact cleanup")
        try:
            self.persistence.close().result(timeout=3.0)
        except Exception:
            logger.exception("failed to close SQLite persistence")

    def reload_backend(self) -> Future[bool]:
        """Rebuild the backend after a runtime artifact changes on disk."""
        result: Future[bool] = Future()

        def reload() -> None:
            try:
                with self._lock:
                    if self._closing:
                        raise RuntimeError("desktop composition is closing")
                    if self._lifecycle_active:
                        raise RuntimeError("desktop backend lifecycle operation is running")
                    if self._bootstrap_thread is not None and self._bootstrap_thread.is_alive():
                        raise RuntimeError("desktop backend bootstrap is still running")
                    self._lifecycle_active = True
                    current = self._backend
                    pending = self._cleanup_pending
                    if pending is not None:
                        self._cleanup_pending = None
                self.recorder.attach_service(None)
                if pending is not None and not self._close_backend_safely(pending):
                    with self._lock:
                        self._cleanup_pending = pending
                    raise RuntimeError("failed to finish stopping the transcription runtime")
                if current is not None:
                    if not self._close_backend_safely(current):
                        raise RuntimeError("failed to stop the current transcription runtime")
                    with self._lock:
                        if self._backend is current:
                            self._backend = None
                self.transcribe_controller.mark_unavailable(
                    "Loading the updated transcription runtime..."
                )
                self._bootstrap_backend()
                result.set_result(
                    self.transcribe_controller.snapshot.availability
                    is TranscribeAvailability.AVAILABLE
                )
            except BaseException as error:
                result.set_exception(error)
            finally:
                with self._lock:
                    self._lifecycle_active = False

        thread = Thread(target=reload, name="desktop-backend-reload", daemon=True)
        with self._lock:
            self._bootstrap_thread = thread
        thread.start()
        return result

    def unload_backend(self) -> Future[bool]:
        """Stop the backend before a managed runtime artifact is removed."""
        result: Future[bool] = Future()

        def unload() -> None:
            try:
                with self._lock:
                    if self._closing:
                        raise RuntimeError("desktop composition is closing")
                    if self._lifecycle_active:
                        raise RuntimeError("desktop backend lifecycle operation is running")
                    if self._bootstrap_thread is not None and self._bootstrap_thread.is_alive():
                        raise RuntimeError("desktop backend bootstrap is still running")
                    self._lifecycle_active = True
                    current = self._backend
                    pending = self._cleanup_pending
                    if pending is not None:
                        self._cleanup_pending = None
                self.recorder.attach_service(None)
                if pending is not None and not self._close_backend_safely(pending):
                    with self._lock:
                        self._cleanup_pending = pending
                    raise RuntimeError("failed to finish stopping the transcription runtime")
                if current is not None:
                    if not self._close_backend_safely(current):
                        raise RuntimeError("failed to stop the current transcription runtime")
                    with self._lock:
                        if self._backend is current:
                            self._backend = None
                self.transcribe_controller.mark_unavailable(
                    "Transcription runtime is unavailable until a model is downloaded."
                )
                result.set_result(True)
            except BaseException as error:
                result.set_exception(error)
            finally:
                with self._lock:
                    self._lifecycle_active = False

        thread = Thread(target=unload, name="desktop-backend-unload", daemon=True)
        with self._lock:
            self._bootstrap_thread = thread
        thread.start()
        return result

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

            backend = _build_backend(
                build_application_from_environment,
                self.persistence,
                audio_artifact_port=self.audio_artifact_port,
                readiness_timeout=self.readiness_timeout,
            )
            backend.start()
            if not backend.imported_media_available:
                self._close_backend_or_track(backend)
                self.transcribe_controller.mark_unavailable(
                    "Imported media transcription is unavailable: configuration is incomplete."
                )
                return
            self.recorder.attach_service(getattr(backend, "microphone", None))
        except (
            ConfigurationError,
            MissingModelError,
            RuntimeUnavailableError,
            WindowsAdapterRequiredError,
            OSError,
        ) as error:
            if backend is not None:
                self._close_backend_or_track(backend)
            diagnostics = getattr(backend, "diagnostics", None)
            logger.warning(
                "imported-media runtime unavailable: %s; diagnostics=%s",
                error,
                diagnostics,
            )
            self.transcribe_controller.mark_unavailable(
                "Imported media transcription is unavailable: runtime prerequisites failed."
            )
            return
        except Exception:
            if backend is not None:
                self._close_backend_or_track(backend)
            logger.exception("unexpected imported-media bootstrap failure")
            self.transcribe_controller.mark_unavailable(
                "Imported media transcription is unavailable: startup failed."
            )
            return

        with self._lock:
            closing = self._closing
            if not closing:
                self._backend = backend
        if closing or not self.transcribe_controller.attach_backend(backend):
            self._close_backend_or_track(backend)

    def _close_backend_or_track(self, backend: object) -> None:
        if self._close_backend_safely(backend):
            return
        with self._lock:
            self._cleanup_pending = backend

    @staticmethod
    def _close_backend_safely(backend: object) -> bool:
        try:
            backend.close()
            return True
        except Exception:
            logger.exception("failed to close imported-media runtime")
            return False


def _build_backend(
    builder: Callable[..., object],
    history_port: PersistenceService,
    *,
    readiness_timeout: float | None = None,
    audio_artifact_port: object | None = None,
) -> object:
    """Support narrow test builders without weakening production history wiring."""
    parameters = signature(builder).parameters
    accepts_keywords = any(
        parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    kwargs: dict[str, object] = {}
    if "history_port" in parameters or accepts_keywords:
        kwargs["history_port"] = history_port
    if readiness_timeout is not None and ("readiness_timeout" in parameters or accepts_keywords):
        kwargs["readiness_timeout"] = readiness_timeout
    if audio_artifact_port is not None and (
        "audio_artifact_port" in parameters or accepts_keywords
    ):
        kwargs["audio_artifact_port"] = audio_artifact_port
    return builder(**kwargs)


def _build_desktop_composition(readiness_timeout: float | None) -> DesktopComposition:
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
    media_actions = HistoryMediaActionService(
        artifacts,
        WindowsHistoryAudioPlaybackAdapter(),
        WindowsHistoryArtifactRevealAdapter(),
    )
    history_deletion = HistoryDeletionService(persistence, artifacts.delete)
    history_deletion.start()
    transcribe_controller = TranscribePageController(
        None,
        history_port=persistence,
        availability=TranscribeAvailability.LOADING,
        unavailable_message="Loading imported-media transcription runtime...",
    )
    controller = ShellController.unavailable()
    recorder = MicrophoneRecorderController()
    global_shortcut = GlobalToggleShortcutService(recorder, create_global_shortcut_port())
    composition = _DesktopComposition(
        controller=controller,
        recorder=recorder,
        transcribe_controller=transcribe_controller,
        persistence=persistence,
        artifact_cleanup=artifacts.delete,
        audio_artifact_port=artifacts,
        media_actions=media_actions,
        history_deletion=history_deletion,
        global_shortcut=global_shortcut,
        global_hotkey_settings=GlobalHotkeySettingsService(global_shortcut, persistence),
        readiness_timeout=readiness_timeout,
    )
    if not _imported_media_environment_present():
        transcribe_controller.mark_unavailable("Imported media runtime is not configured.")
    else:
        composition.start()
    return composition


def build_desktop_composition() -> DesktopComposition:
    return _build_desktop_composition(None)


def build_package_smoke_desktop_composition(readiness_timeout: float) -> DesktopComposition:
    return _build_desktop_composition(readiness_timeout)


def _imported_media_environment_present() -> bool:
    names = (
        "VOICEINK_RUNTIME_MANIFEST",
        "VOICEINK_ARTIFACT_LOCK",
        "VOICEINK_ARTIFACT_LOCK_SHA256",
        "VOICEINK_FFMPEG_PATH",
        "VOICEINK_IMPORT_WORKSPACE_ROOT",
        "VOICEINK_FFMPEG_VERSION",
        "VOICEINK_FFMPEG_PROVENANCE_URL",
        "VOICEINK_FFMPEG_SHA256",
        "VOICEINK_FFMPEG_LICENSE",
    )
    return all(os.environ.get(name, "").strip() for name in names) or packaged_runtime_available()


__all__ = [
    "DesktopComposition",
    "build_desktop_composition",
    "build_package_smoke_desktop_composition",
]
