from __future__ import annotations

import json
from pathlib import Path

from voiceink_win.domain import ConfigurationError
from voiceink_win.infrastructure import JsonlEventWriter, safe_failure, sanitize_report_value


def test_jsonl_writer_emits_sanitized_events(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    writer = JsonlEventWriter(path, run_id="run-1")
    writer.event(
        "runtime.verified",
        allowed_path=str(tmp_path / "secret.exe"),
        error="raw exception text",
        transcript="do not write this",
        artifact_id="sidecar",
    )
    writer.close()

    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["event"] == "runtime.verified"
    assert event["run_id"] == "run-1"
    assert "secret.exe" not in path.read_text(encoding="utf-8")
    assert "do not write this" not in path.read_text(encoding="utf-8")
    assert "raw exception text" not in path.read_text(encoding="utf-8")


def test_safe_failure_contains_only_stable_fields() -> None:
    error = ConfigurationError(f"secret path {Path('/tmp/private/runtime.exe')}")

    failure = safe_failure(error)

    assert failure == {"error_code": "configuration", "error_type": "ConfigurationError"}
    assert "runtime.exe" not in json.dumps(failure)


def test_report_sanitizer_removes_paths_and_sensitive_values() -> None:
    value = sanitize_report_value(
        {"model_path": "/tmp/model.gguf", "nested": "at /tmp/model.gguf", "audio": b"pcm"}
    )

    assert value == {"nested": "at <path>"}
