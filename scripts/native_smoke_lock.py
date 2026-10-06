"""Validate the reviewable native smoke artifact lock."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = "voiceink.native-smoke.artifact-lock.v1"
_HEX = frozenset("0123456789abcdefABCDEF")
_PLACEHOLDER_PREFIX = "REPLACE_WITH_"


def load_native_smoke_lock(
    source: Path | str, *, allow_template: bool = False
) -> dict[str, object]:
    try:
        value = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("native smoke artifact lock could not be loaded") from error
    if not isinstance(value, dict):
        raise ValueError("native smoke artifact lock must be a JSON object")
    if set(value) != {"schema", "version", "artifacts"}:
        raise ValueError("native smoke artifact lock schema is malformed")
    if value["schema"] != SCHEMA or value["version"] != 1:
        raise ValueError("native smoke artifact lock schema is unsupported")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != {
        "ffmpeg",
        "nemo-speech-cpp-windows-amd64",
        "parakeet-tdt-v3",
        "fixture",
    }:
        raise ValueError("native smoke artifact lock must contain the required artifacts")
    _validate_archive(artifacts["ffmpeg"], "ffmpeg", allow_template)
    _validate_archive(
        artifacts["nemo-speech-cpp-windows-amd64"],
        "nemo-speech-cpp-windows-amd64",
        allow_template,
    )
    _validate_model(artifacts["parakeet-tdt-v3"], allow_template)
    _validate_fixture(artifacts["fixture"], allow_template)
    return value


def pinned_value(name: str, expected: str, override: str | None = None) -> str:
    """Return the tracked value and reject a repository-variable mismatch."""
    if override is not None and override.strip() and override.strip() != expected:
        raise ValueError(f"{name} does not match the tracked native smoke lock")
    return expected


def _validate_archive(value: object, label: str, allow_template: bool) -> None:
    _require_fields(
        value,
        {
            "kind",
            "url",
            "archive_sha256",
            "executable_sha256",
            "version",
            "provenance_url",
            "license",
        },
        label,
    )
    assert isinstance(value, dict)
    if value["kind"] != "archive":
        raise ValueError(f"{label} kind is invalid")
    _https(value["url"], f"{label}.url", allow_template)
    _https(value["provenance_url"], f"{label}.provenance_url", allow_template)
    _hash(value["archive_sha256"], f"{label}.archive_sha256", allow_template)
    _hash(value["executable_sha256"], f"{label}.executable_sha256", allow_template)
    _text(value["version"], f"{label}.version", allow_template)
    _text(value["license"], f"{label}.license", allow_template)


def _validate_model(value: object, allow_template: bool) -> None:
    _require_fields(
        value,
        {"kind", "url", "sha256", "model_id", "version", "provenance_url", "license"},
        "parakeet-tdt-v3",
    )
    assert isinstance(value, dict)
    if value["kind"] != "model":
        raise ValueError("model kind is invalid")
    _https(value["url"], "model.url", allow_template)
    _https(value["provenance_url"], "model.provenance_url", allow_template)
    _hash(value["sha256"], "model.sha256", allow_template)
    _text(value["model_id"], "model.model_id", allow_template)
    _text(value["version"], "model.version", allow_template)
    _text(value["license"], "model.license", allow_template)


def _validate_fixture(value: object, allow_template: bool) -> None:
    _require_fields(value, {"kind", "url", "sha256", "provenance_url", "license"}, "fixture")
    assert isinstance(value, dict)
    if value["kind"] != "fixture":
        raise ValueError("fixture kind is invalid")
    _https(value["url"], "fixture.url", allow_template)
    _https(value["provenance_url"], "fixture.provenance_url", allow_template)
    _hash(value["sha256"], "fixture.sha256", allow_template)
    _text(value["license"], "fixture.license", allow_template)


def _require_fields(value: object, fields: set[str], label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{label} lock entry is malformed")


def _text(value: object, field: str, allow_template: bool) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")
    if not allow_template and value.startswith(_PLACEHOLDER_PREFIX):
        raise ValueError(f"{field} still contains a template placeholder")


def _hash(value: object, field: str, allow_template: bool) -> None:
    if allow_template and isinstance(value, str) and value.startswith(_PLACEHOLDER_PREFIX):
        return
    if not isinstance(value, str) or len(value) != 64 or any(char not in _HEX for char in value):
        raise ValueError(f"{field} must be a SHA-256 hex digest")


def _https(value: object, field: str, allow_template: bool) -> None:
    if (
        not isinstance(value, str)
        or urlsplit(value).scheme != "https"
        or not urlsplit(value).netloc
    ):
        raise ValueError(f"{field} must be an HTTPS URL")
    if not allow_template and "<PINNED_" in value:
        raise ValueError(f"{field} still contains a template placeholder")


if __name__ == "__main__":
    try:
        load_native_smoke_lock(sys.argv[1])
    except (IndexError, ValueError) as error:
        print(f"native smoke artifact lock invalid: {error}", file=sys.stderr)
        raise SystemExit(2) from error
