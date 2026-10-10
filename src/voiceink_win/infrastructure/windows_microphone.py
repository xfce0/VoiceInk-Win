"""Windows microphone adapter boundary.

The native helper is intentionally not implemented until the WASAPI contract
and artifact gates provide evidence. This adapter therefore exposes a stable
unavailable state instead of a fake or in-process fallback.
"""

from __future__ import annotations

import os

from voiceink_win.domain import (
    AudioCaptureSession,
    InputDevice,
    MicrophoneAvailability,
    MicrophoneStatus,
    MicrophoneUnavailableError,
)


class WindowsAudioInputAdapter:
    """Reserved boundary for the future isolated WASAPI helper."""

    def __init__(self) -> None:
        self._status = MicrophoneStatus(
            MicrophoneAvailability.UNAVAILABLE,
            (
                "Windows WASAPI capture is unavailable: the native helper and "
                "required contract gates are not enabled."
                if os.name == "nt"
                else "Windows WASAPI capture is unavailable on this platform."
            ),
        )

    def status(self) -> MicrophoneStatus:
        return self._status

    def enumerate_devices(self, deadline: float | None = None) -> tuple[InputDevice, ...]:
        del deadline
        raise MicrophoneUnavailableError(self._status.message)

    def open(
        self, selection_token: str | None = None, deadline: float | None = None
    ) -> AudioCaptureSession:
        del selection_token, deadline
        raise MicrophoneUnavailableError(self._status.message)
