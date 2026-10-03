"""Typed failures exposed by the ASR domain boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar


class AsrErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    CONFIGURATION = "configuration"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    MISSING_MODEL = "missing_model"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PROTOCOL = "protocol"
    EXECUTION = "execution"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    QUEUE_FULL = "queue_full"


class AsrError(Exception):
    """Base class for failures that must never become an empty success."""

    code: ClassVar[AsrErrorCode]
    retryable: ClassVar[bool] = False

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause


class InvalidInputError(AsrError):
    code = AsrErrorCode.INVALID_INPUT


class ConfigurationError(AsrError):
    code = AsrErrorCode.CONFIGURATION


class RuntimeUnavailableError(AsrError):
    code = AsrErrorCode.RUNTIME_UNAVAILABLE
    retryable = True


class MissingModelError(AsrError):
    code = AsrErrorCode.MISSING_MODEL


class AsrTimeoutError(AsrError):
    code = AsrErrorCode.TIMEOUT
    retryable = True


class CancellationError(AsrError):
    code = AsrErrorCode.CANCELLED


class ProtocolError(AsrError):
    code = AsrErrorCode.PROTOCOL


class ExecutionError(AsrError):
    code = AsrErrorCode.EXECUTION


class BackendUnavailableError(AsrError):
    code = AsrErrorCode.BACKEND_UNAVAILABLE
    retryable = True


class QueueFullError(AsrError):
    code = AsrErrorCode.QUEUE_FULL
    retryable = True


class ProcessCrashedError(ExecutionError):
    """The isolated native runtime stopped unexpectedly."""


DeadlineExceededError = AsrTimeoutError
TimeoutError = AsrTimeoutError
