"""Application state machine for the first desktop shell slice."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from voiceink_win.domain import AsrError, AsrErrorCode, ShellSnapshot, ShellState, TranscriptResult


class ShellTranscriptionBackend(Protocol):
    """Application seam for a transcription result without runtime details."""

    def transcribe(self) -> TranscriptResult: ...


ShellListener = Callable[[ShellSnapshot], None]


class ShellController:
    """Coordinate shell actions and expose immutable snapshots to presentation."""

    def __init__(self, backend: ShellTranscriptionBackend) -> None:
        self._backend = backend
        self._snapshot = ShellSnapshot()
        self._listeners: list[ShellListener] = []

    @property
    def snapshot(self) -> ShellSnapshot:
        return self._snapshot

    def subscribe(self, listener: ShellListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def start_recording(self) -> bool:
        if self._snapshot.state not in {
            ShellState.IDLE,
            ShellState.TRANSCRIPT_READY,
            ShellState.EMPTY,
            ShellState.ERROR,
        }:
            return False
        self._publish(ShellSnapshot(state=ShellState.RECORDING))
        return True

    def stop_recording(self) -> bool:
        if self._snapshot.state is not ShellState.RECORDING:
            return False
        self._publish(ShellSnapshot(state=ShellState.PROCESSING))
        return True

    def complete_processing(self) -> bool:
        if self._snapshot.state is not ShellState.PROCESSING:
            return False
        try:
            result = self._backend.transcribe()
        except AsrError as error:
            self._publish(ShellSnapshot(state=ShellState.ERROR, error=_safe_error_message(error)))
        except Exception:
            self._publish(
                ShellSnapshot(
                    state=ShellState.ERROR,
                    error="Transcription failed. Check the runtime and try again.",
                )
            )
        else:
            transcript = result.text.strip()
            self._publish(
                ShellSnapshot(
                    state=ShellState.TRANSCRIPT_READY if transcript else ShellState.EMPTY,
                    transcript=transcript,
                )
            )
        return True

    def reset(self) -> None:
        self._publish(ShellSnapshot())

    def _publish(self, snapshot: ShellSnapshot) -> None:
        self._snapshot = snapshot
        for listener in tuple(self._listeners):
            listener(snapshot)


def _safe_error_message(error: AsrError) -> str:
    messages = {
        AsrErrorCode.RUNTIME_UNAVAILABLE: (
            "Local transcription is unavailable. Check the runtime and try again."
        ),
        AsrErrorCode.MISSING_MODEL: (
            "The transcription model is not ready. Check the runtime setup and try again."
        ),
        AsrErrorCode.TIMEOUT: "Transcription took too long. Try a shorter recording.",
        AsrErrorCode.CANCELLED: "The recording was cancelled before text was ready.",
        AsrErrorCode.BACKEND_UNAVAILABLE: "The selected transcription backend is unavailable.",
        AsrErrorCode.PROTOCOL: "The transcription runtime returned an invalid response.",
    }
    return messages.get(error.code, "Transcription failed. Check the runtime and try again.")
