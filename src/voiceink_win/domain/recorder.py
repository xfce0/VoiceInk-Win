"""Platform-independent recorder presentation values."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class RecorderState(StrEnum):
    UNAVAILABLE = "unavailable"
    IDLE = "idle"
    REQUESTING_ACCESS = "requesting_access"
    STARTING = "starting"
    RECORDING_SILENT = "recording_silent"
    RECORDING_SOUNDING = "recording_sounding"
    STOPPING = "stopping"
    PROCESSING = "processing"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    RECOVERY_PENDING = "recovery_pending"
    SUCCEEDED = "succeeded"
    EMPTY = "empty"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class RecorderSnapshot:
    """Immutable state published to the Qt presentation layer."""

    state: RecorderState = RecorderState.UNAVAILABLE
    message: str = ""
    transcript: str = ""
    level: float = 0.0
    generation: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.state, RecorderState):
            raise TypeError("recorder state must be a RecorderState")
        if not isinstance(self.message, str) or not isinstance(self.transcript, str):
            raise TypeError("recorder message and transcript must be strings")
        if not math.isfinite(self.level) or not 0.0 <= self.level <= 1.0:
            raise ValueError("recorder level must be finite and within [0.0, 1.0]")
        if self.generation < 0:
            raise ValueError("recorder generation must not be negative")


ACTIVE_RECORDER_STATES = frozenset(
    {
        RecorderState.REQUESTING_ACCESS,
        RecorderState.STARTING,
        RecorderState.RECORDING_SILENT,
        RecorderState.RECORDING_SOUNDING,
        RecorderState.STOPPING,
        RecorderState.PROCESSING,
        RecorderState.CANCEL_REQUESTED,
        RecorderState.RECOVERY_PENDING,
    }
)
TERMINAL_RECORDER_STATES = frozenset(
    {
        RecorderState.CANCELLED,
        RecorderState.SUCCEEDED,
        RecorderState.EMPTY,
        RecorderState.ERROR,
    }
)
