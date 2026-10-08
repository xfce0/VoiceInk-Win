"""Production composition for the desktop shell and imported-media page."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol

from voiceink_win.application import ShellController, TranscribePageController
from voiceink_win.application.transcribe_output import LocalTextFilePort

logger = logging.getLogger(__name__)


class DesktopComposition(Protocol):
    @property
    def controller(self) -> ShellController: ...

    @property
    def transcribe_controller(self) -> TranscribePageController: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class _UnavailableDesktopComposition:
    controller: ShellController
    transcribe_controller: TranscribePageController

    def close(self) -> None:
        self.transcribe_controller.close()


@dataclass(slots=True)
class _BackendDesktopComposition:
    backend: object
    controller: ShellController
    transcribe_controller: TranscribePageController

    def close(self) -> None:
        self.transcribe_controller.close()
        self.backend.close()


def build_desktop_composition() -> DesktopComposition:
    """Build imported-media transcription when its trusted runtime is configured.

    Recording remains deliberately unavailable: this composition only wires the
    existing imported-media backend and never enables microphone/WASAPI paths.
    """
    if not _imported_media_environment_present():
        return _unavailable("imported-media runtime is not configured")

    backend = None
    try:
        from voiceink_win.composition import build_application_from_environment
        from voiceink_win.domain import (
            ConfigurationError,
            MissingModelError,
            RuntimeUnavailableError,
        )
        from voiceink_win.infrastructure import WindowsAdapterRequiredError

        backend = build_application_from_environment()
        backend.start()
    except (
        ConfigurationError,
        MissingModelError,
        RuntimeUnavailableError,
        WindowsAdapterRequiredError,
        OSError,
    ) as error:
        if backend is not None:
            try:
                backend.close()
            except Exception:
                logger.exception("failed to clean up unavailable imported-media runtime")
        logger.warning("imported-media runtime unavailable", extra={"reason": str(error)})
        return _unavailable("imported-media runtime is unavailable")

    if not backend.imported_media_available:
        backend.close()
        return _unavailable("imported-media configuration is unavailable")

    from voiceink_win.presentation.clipboard import QtClipboardPort

    return _BackendDesktopComposition(
        backend=backend,
        controller=ShellController.unavailable(),
        transcribe_controller=TranscribePageController(
            backend,
            clipboard=QtClipboardPort(),
            text_files=LocalTextFilePort(),
        ),
    )


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


def _unavailable(reason: str) -> _UnavailableDesktopComposition:
    return _UnavailableDesktopComposition(
        controller=ShellController.unavailable(),
        transcribe_controller=TranscribePageController(
            None,
            unavailable_message=reason,
        ),
    )


__all__ = ["DesktopComposition", "build_desktop_composition"]
