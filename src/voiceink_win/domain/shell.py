"""Platform-independent state values for the desktop shell."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ShellState(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"
    TRANSCRIPT_READY = "transcript_ready"
    EMPTY = "empty"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ShellSnapshot:
    """The complete user-visible state of one shell recording session."""

    state: ShellState = ShellState.IDLE
    transcript: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.state, ShellState):
            raise TypeError("shell state must be a ShellState")
        if not isinstance(self.transcript, str) or not isinstance(self.error, str):
            raise TypeError("shell transcript and error must be strings")
        if self.state is ShellState.TRANSCRIPT_READY and not self.transcript.strip():
            raise ValueError("transcript-ready state must contain transcript text")
        if self.state is not ShellState.TRANSCRIPT_READY and self.transcript:
            raise ValueError("only transcript-ready state may contain transcript text")
        if self.state is ShellState.ERROR and not self.error.strip():
            raise ValueError("error state must contain an error message")
        if self.state is not ShellState.ERROR and self.error:
            raise ValueError("only error state may contain an error message")
