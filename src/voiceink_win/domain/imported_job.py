"""Lock-linearized imported-job state machine."""

from __future__ import annotations

from threading import Event, Lock

from .imported_errors import CancellationReason, ErrorCode
from .imported_models import Attempt, Cancelled, Failed, JobId, Stage, Success, TerminalResult

_TERMINAL = {Stage.SUCCEEDED, Stage.FAILED, Stage.CANCELLED}
_ALLOWED: dict[Stage, set[Stage]] = {
    Stage.ACCEPTED: {Stage.QUEUED},
    Stage.QUEUED: {Stage.NORMALIZING, Stage.CLEANING_UP},
    Stage.NORMALIZING: {Stage.TRANSCRIBING, Stage.CLEANING_UP},
    Stage.TRANSCRIBING: {Stage.RETRY_WAITING, Stage.CLEANING_UP},
    Stage.RETRY_WAITING: {Stage.QUEUED, Stage.CLEANING_UP},
    Stage.CLEANING_UP: _TERMINAL,
    Stage.SUCCEEDED: set(),
    Stage.FAILED: set(),
    Stage.CANCELLED: set(),
    Stage.NORMALIZATION: set(),
    Stage.TRANSCRIPTION: set(),
    Stage.CLEANUP: set(),
}


class ImportJob:
    """Mutable aggregate whose state transitions are CAS operations under one lock."""

    def __init__(self, job_id: JobId, *, deadline: float) -> None:
        self.job_id = job_id
        self.deadline = deadline
        self.attempt = Attempt(1)
        self.stage = Stage.ACCEPTED
        self.cancellation_reason: CancellationReason | None = None
        self.cancellation_requested = False
        self.deadline_requested = False
        self.cleanup_enqueue_requested = False
        self.result = None
        self.done = Event()
        self.lock = Lock()

    def transition(
        self,
        expected_attempt: Attempt,
        expected_state: Stage,
        new_state: Stage,
    ) -> bool:
        with self.lock:
            if (
                self.attempt != expected_attempt
                or self.stage is not expected_state
                or new_state not in _ALLOWED[expected_state]
            ):
                return False
            self.stage = new_state
            return True

    def enter_cleanup(self, expected_attempt: Attempt, expected_state: Stage) -> bool:
        return self.transition(expected_attempt, expected_state, Stage.CLEANING_UP)

    def complete(
        self, expected_attempt: Attempt, expected_state: Stage, result: TerminalResult
    ) -> bool:
        with self.lock:
            if (
                self.attempt != expected_attempt
                or self.stage is not expected_state
                or expected_state is not Stage.CLEANING_UP
                or result.job_id != self.job_id
                or result.attempt != expected_attempt
            ):
                return False
            if isinstance(result, Success):
                if self.cancellation_requested or self.deadline_requested:
                    return False
                if result.transcription.job_id != self.job_id:
                    return False
            elif isinstance(result, Cancelled):
                if not self.cancellation_requested or self.deadline_requested:
                    return False
            elif isinstance(result, Failed):
                if self.cancellation_requested and result.code is not ErrorCode.CLEANUP_WARNING:
                    return False
                if self.deadline_requested and result.code not in {
                    ErrorCode.DEADLINE_EXCEEDED,
                    ErrorCode.CLEANUP_WARNING,
                }:
                    return False
            terminal_stage = {
                "succeeded": Stage.SUCCEEDED,
                "failed": Stage.FAILED,
                "cancelled": Stage.CANCELLED,
            }.get(result.status)
            if terminal_stage is None:
                return False
            self.stage = terminal_stage
            self.result = result
            self.done.set()
            return True

    def cancel(
        self, expected_attempt: Attempt, expected_state: Stage, reason: CancellationReason
    ) -> bool:
        with self.lock:
            if self.attempt != expected_attempt or self.stage is not expected_state:
                return False
            if self.stage in _TERMINAL or self.cancellation_requested or self.deadline_requested:
                return False
            self.cancellation_requested = True
            self.cancellation_reason = reason
            if self.stage in {Stage.QUEUED, Stage.RETRY_WAITING}:
                if self.stage is Stage.RETRY_WAITING:
                    self.cleanup_enqueue_requested = True
                self.stage = Stage.CLEANING_UP
            return True

    def request_cancellation(self, reason: CancellationReason) -> bool:
        with self.lock:
            if self.stage in _TERMINAL or self.cancellation_requested or self.deadline_requested:
                return False
            self.cancellation_requested = True
            self.cancellation_reason = reason
            if self.stage in {Stage.QUEUED, Stage.RETRY_WAITING}:
                if self.stage is Stage.RETRY_WAITING:
                    self.cleanup_enqueue_requested = True
                self.stage = Stage.CLEANING_UP
            return True

    def force_cleanup(self, expected_attempt: Attempt) -> bool:
        with self.lock:
            if self.attempt != expected_attempt or self.stage in _TERMINAL:
                return False
            if self.stage is Stage.RETRY_WAITING:
                self.cleanup_enqueue_requested = True
            if self.stage is not Stage.CLEANING_UP:
                self.stage = Stage.CLEANING_UP
            return True

    def request_deadline(self) -> bool:
        with self.lock:
            if (
                self.stage in _TERMINAL
                or self.stage is Stage.CLEANING_UP
                or self.cancellation_requested
                or self.deadline_requested
            ):
                return False
            self.deadline_requested = True
            if self.stage in {Stage.QUEUED, Stage.RETRY_WAITING}:
                if self.stage is Stage.RETRY_WAITING:
                    self.cleanup_enqueue_requested = True
                self.stage = Stage.CLEANING_UP
            return True

    def intent(self) -> tuple[bool, bool, CancellationReason | None]:
        with self.lock:
            return self.cancellation_requested, self.deadline_requested, self.cancellation_reason

    def accepts_stage_result(self, expected_attempt: Attempt, expected_state: Stage) -> bool:
        """Atomically check that a stage may still publish its local result."""
        with self.lock:
            return (
                self.attempt == expected_attempt
                and self.stage is expected_state
                and not self.cancellation_requested
                and not self.deadline_requested
            )

    def prepare_retry(self, expected_attempt: Attempt) -> Attempt | None:
        with self.lock:
            if (
                self.attempt != expected_attempt
                or self.stage is not Stage.RETRY_WAITING
                or self.cancellation_requested
                or self.deadline_requested
            ):
                return None
            self.attempt = self.attempt.next()
            return self.attempt

    def take_cleanup_enqueue_request(self) -> bool:
        with self.lock:
            if self.stage is not Stage.CLEANING_UP or not self.cleanup_enqueue_requested:
                return False
            self.cleanup_enqueue_requested = False
            return True

    def is_terminal(self) -> bool:
        with self.lock:
            return self.stage in _TERMINAL
