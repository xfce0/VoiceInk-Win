from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.native_smoke_lock import PARAKEET_MODEL_ID, load_native_smoke_lock, pinned_value

ROOT = Path(__file__).resolve().parents[1]


def _lock() -> dict[str, object]:
    digest = hashlib.sha256(b"pinned").hexdigest()
    return {
        "schema": "voiceink.native-smoke.artifact-lock.v1",
        "version": 1,
        "artifacts": {
            "ffmpeg": {
                "kind": "archive",
                "url": "https://example.invalid/ffmpeg.zip",
                "archive_sha256": digest,
                "executable_sha256": digest,
                "version": "1",
                "provenance_url": "https://example.invalid/ffmpeg",
                "license": "GPL-3.0-or-later",
            },
            "nemo-speech-cpp-windows-amd64": {
                "kind": "archive",
                "url": "https://example.invalid/sidecar.zip",
                "archive_sha256": digest,
                "executable_sha256": digest,
                "version": "1",
                "provenance_url": "https://example.invalid/sidecar",
                "license": "Apache-2.0",
            },
            "parakeet-tdt-0.6b-v3.oss-align.q8_0": {
                "kind": "model",
                "url": "https://example.invalid/model.gguf",
                "sha256": digest,
                "model_id": "parakeet-tdt-0.6b-v3.oss-align.q8_0",
                "version": "1",
                "provenance_url": "https://example.invalid/model",
                "license": "CC-BY-4.0",
            },
            "fixture": {
                "kind": "fixture",
                "url": "https://example.invalid/fixture",
                "sha256": digest,
                "provenance_url": "https://example.invalid/fixture-source",
                "license": "CC-BY-4.0",
            },
        },
    }


def test_native_smoke_lock_requires_all_tracked_pins(tmp_path: Path) -> None:
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(_lock()), encoding="utf-8")

    loaded = load_native_smoke_lock(path)

    assert loaded["artifacts"]["ffmpeg"]["executable_sha256"]


def test_native_smoke_lock_rejects_mutable_override() -> None:
    with pytest.raises(ValueError, match="does not match"):
        pinned_value("VOICEINK_FFMPEG_SHA256_OVERRIDE", "a" * 64, "b" * 64)


def test_tracked_lock_is_reviewable_template() -> None:
    path = ROOT / ".github/native-smoke/artifact-lock.template.json"

    loaded = load_native_smoke_lock(path, allow_template=True)

    assert loaded["schema"] == "voiceink.native-smoke.artifact-lock.v1"


def test_tracked_parakeet_id_matches_the_gguf_general_name() -> None:
    loaded = load_native_smoke_lock(ROOT / ".github/native-smoke/artifact-lock.template.json")

    assert PARAKEET_MODEL_ID == "parakeet-tdt-0.6b-v3.oss-align.q8_0"
    assert loaded["artifacts"][PARAKEET_MODEL_ID]["model_id"] == PARAKEET_MODEL_ID


def test_native_smoke_lock_rejects_model_id_mismatch_with_artifact_key(tmp_path: Path) -> None:
    value = _lock()
    value["artifacts"][PARAKEET_MODEL_ID]["model_id"] = "different-model"
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="canonical artifact key"):
        load_native_smoke_lock(path)
