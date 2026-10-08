"""Bounded, sanitized diagnostics for one native runtime startup transaction."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from threading import Lock

MAX_STARTUP_RECORDS = 32
MAX_CLEANUP_FAILURES = 16
MAX_DIAGNOSTICS_BYTES = 64 * 1024
MAX_FIELD_LENGTH = 128

STARTUP_PHASES = frozenset(
    {
        "configuration",
        "artifact_verification",
        "artifact_lease",
        "process_create_suspended",
        "job_assignment",
        "artifact_revalidation",
        "resume",
        "readiness_probe",
        "attestation_passthrough",
        "cleanup",
    }
)
STARTUP_OPERATIONS = frozenset(
    {
        "validate_endpoint",
        "open_artifact",
        "verify_artifact",
        "create_process",
        "assign_job",
        "revalidate_artifact",
        "resume_process",
        "probe_readiness",
        "observe_attestation",
        "get_process_state",
        "terminate_process",
        "wait_for_exit",
        "reap_process",
        "close_resource",
    }
)

_SAFE_TOKEN = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class PrimaryFailure:
    """The first typed startup failure, frozen for the transaction lifetime."""

    startup_phase: str
    operation: str
    error_code: str
    error_type: str
    win32_error_code: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def _token(value: object, fallback: str) -> str:
    text = value.value if hasattr(value, "value") else value
    if not isinstance(text, str):
        text = fallback
    return _SAFE_TOKEN.sub("_", text).strip("_")[:MAX_FIELD_LENGTH] or fallback


def _code(value: object, fallback: str) -> str:
    text = value.value if hasattr(value, "value") else value
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", text):
        return fallback
    return text


def _win32_error_code(error: BaseException) -> int | None:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        value = getattr(current, "win32_error_code", None)
        if value is None:
            value = getattr(current, "winerror", None)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 0xFFFFFFFF:
            return value
        for attribute in ("cause", "__cause__", "__context__"):
            cause = getattr(current, attribute, None)
            if isinstance(cause, BaseException):
                pending.append(cause)
    return None


def error_code(error: BaseException, fallback: str = "execution") -> str:
    return _code(getattr(error, "code", None), fallback)


def error_type(error: BaseException) -> str:
    return _token(type(error).__name__, "Exception")


class StartupDiagnostics:
    """Thread-safe diagnostics with fixed vocabulary and hard size bounds."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._records: list[dict[str, object]] = []
        self._cleanup_failures: list[dict[str, object]] = []
        self._primary_failure: PrimaryFailure | None = None
        self._cleanup_outcome = "complete"
        self._diagnostics_truncated = False

    @property
    def primary_failure(self) -> PrimaryFailure | None:
        with self._lock:
            return self._primary_failure

    @property
    def cleanup_outcome(self) -> str:
        with self._lock:
            return self._cleanup_outcome

    def reset(self) -> None:
        with self._lock:
            self._records.clear()
            self._cleanup_failures.clear()
            self._primary_failure = None
            self._cleanup_outcome = "complete"
            self._diagnostics_truncated = False

    def record(
        self,
        startup_phase: str,
        operation: str,
        error_code: str,
        *,
        error: BaseException | None = None,
        win32_error_code: int | None = None,
    ) -> None:
        phase = _vocabulary(startup_phase, STARTUP_PHASES, "phase")
        operation_name = _vocabulary(operation, STARTUP_OPERATIONS, "operation")
        record: dict[str, object] = {
            "startup_phase": phase,
            "operation": operation_name,
            "error_code": _code(error_code, "unknown"),
        }
        if error is not None:
            record["error_type"] = error_type(error)
            win32_error_code = _win32_error_code(error)
        if win32_error_code is not None and 0 <= win32_error_code <= 0xFFFFFFFF:
            record["win32_error_code"] = win32_error_code
        with self._lock:
            if len(self._records) >= MAX_STARTUP_RECORDS:
                self._diagnostics_truncated = True
                return
            self._records.append(record)

    def record_failure(
        self,
        startup_phase: str,
        operation: str,
        error: BaseException,
        *,
        error_code_value: str | None = None,
    ) -> None:
        code = error_code_value or error_code(error)
        self.record(startup_phase, operation, code, error=error)
        failure = PrimaryFailure(
            startup_phase=_vocabulary(startup_phase, STARTUP_PHASES, "phase"),
            operation=_vocabulary(operation, STARTUP_OPERATIONS, "operation"),
            error_code=_code(code, "execution"),
            error_type=error_type(error),
            win32_error_code=_win32_error_code(error),
        )
        with self._lock:
            if self._primary_failure is None:
                self._primary_failure = failure

    def record_cleanup_failure(self, operation: str, error: BaseException) -> None:
        operation_name = _vocabulary(operation, STARTUP_OPERATIONS, "operation")
        failure = {
            "startup_phase": "cleanup",
            "operation": operation_name,
            "error_code": error_code(error, "cleanup_failed"),
            "error_type": error_type(error),
        }
        win32_code = _win32_error_code(error)
        if win32_code is not None:
            failure["win32_error_code"] = win32_code
        with self._lock:
            if len(self._cleanup_failures) >= MAX_CLEANUP_FAILURES:
                self._diagnostics_truncated = True
                return
            self._cleanup_failures.append(failure)

    def set_cleanup_outcome(self, outcome: str) -> None:
        if outcome not in {"complete", "failed", "pending"}:
            raise ValueError("cleanup outcome must be complete, failed, or pending")
        with self._lock:
            self._cleanup_outcome = outcome

    def as_dict(self) -> dict[str, object]:
        with self._lock:
            result: dict[str, object] = {
                "primary_failure": (
                    self._primary_failure.as_dict() if self._primary_failure is not None else None
                ),
                "cleanup_outcome": self._cleanup_outcome,
                "records": [dict(record) for record in self._records],
                "cleanup_failures": [dict(record) for record in self._cleanup_failures],
            }
            if self._diagnostics_truncated:
                result["diagnostics_truncated"] = True
        serialized = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > MAX_DIAGNOSTICS_BYTES:
            result["records"] = []
            result["cleanup_failures"] = []
            result["diagnostics_truncated"] = True
        return result


def _vocabulary(value: str, vocabulary: frozenset[str], label: str) -> str:
    if value not in vocabulary:
        raise ValueError(f"unsupported diagnostic {label}: {value}")
    return value


__all__ = [
    "MAX_CLEANUP_FAILURES",
    "MAX_DIAGNOSTICS_BYTES",
    "MAX_FIELD_LENGTH",
    "MAX_STARTUP_RECORDS",
    "PrimaryFailure",
    "STARTUP_OPERATIONS",
    "STARTUP_PHASES",
    "StartupDiagnostics",
    "error_code",
]
