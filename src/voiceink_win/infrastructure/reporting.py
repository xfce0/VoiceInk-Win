"""Small safe JSONL helpers for native validation reports."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9._-])(?:[A-Za-z]:[\\/]|\\\\|/)(?:[^\\/\s\"'<>|,:;)]*[\\/])*[^\\/\s\"'<>|,:;)]*"
)
_REDACTED_KEYS = frozenset(
    {
        "allowed_path",
        "executable_path",
        "model_path",
        "source_path",
        "audio",
        "pcm",
        "transcript",
        "error",
        "exception",
        "traceback",
        "authorization",
        "api_key",
        "api-key",
        "nonce",
    }
)
_SAFE_ERROR_CODE = re.compile(r"[^a-z0-9_.-]+")
_SAFE_ERROR_TYPE = re.compile(r"[^A-Za-z0-9_.-]+")


def sanitize_report_value(value: Any, *, key: str = "") -> Any:
    if key.casefold() in _REDACTED_KEYS:
        return "<redacted>"
    if isinstance(value, Path):
        return "<path>"
    if isinstance(value, dict):
        return {
            str(item_key): sanitize_report_value(item, key=str(item_key))
            for item_key, item in value.items()
            if str(item_key).casefold() not in _REDACTED_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_report_value(item) for item in value]
    if isinstance(value, str):
        return _ABSOLUTE_PATH.sub("<path>", value)
    return value


def safe_failure(error: BaseException) -> dict[str, str]:
    code = getattr(error, "code", None)
    code = getattr(code, "value", code)
    if not isinstance(code, str) or not code:
        code = "native_smoke_failure"
    code = _SAFE_ERROR_CODE.sub("_", code.casefold()).strip("_") or "native_smoke_failure"
    error_type = _SAFE_ERROR_TYPE.sub("_", type(error).__name__).strip("_")
    return {"error_code": code, "error_type": error_type or "Exception"}


class JsonlEventWriter:
    def __init__(self, path: Path, *, run_id: str | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("w", encoding="utf-8")
        self.run_id = run_id or uuid.uuid4().hex

    def event(self, name: str, **fields: Any) -> None:
        payload = {
            "schema": "voiceink.native-smoke.event.v1",
            "event": name,
            "run_id": self.run_id,
            "timestamp_utc": datetime.now(UTC).isoformat(),
            **sanitize_report_value(fields),
        }
        self._stream.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()
