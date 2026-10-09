from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.wasapi_contract import (
    BUILD_PATH,
    DECISION_GATES,
    LOCK_PATH,
    PROTOCOL_PATH,
    _load_json,
    _validate_lock,
    _validate_protocol,
    validate_scaffold,
)

ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_blocked_wasapi_scaffold_is_self_consistent() -> None:
    assert validate_scaffold() == []


def test_protocol_contract_preserves_native_helper_boundary() -> None:
    protocol = _load(PROTOCOL_PATH)
    frame = protocol["frame"]
    boundary = protocol["audio_boundary"]

    assert protocol["implementation_allowed"] is False
    assert frame["magic"] == "VIKA"
    assert frame["max_payload_bytes"] == 64 * 1024
    assert frame["canonical_chunk_bytes"] == 32 * 1024
    assert frame["aggregate_ipc_queue_bytes"] == 4 * 1024 * 1024
    assert boundary["native_packets_cross_boundary"] is False
    assert boundary["native_handles_cross_boundary"] is False
    assert boundary["endpoint_ids_cross_boundary"] is False


def test_build_contract_forbids_fake_and_python_capture() -> None:
    contract = _load(BUILD_PATH)

    assert contract["status"] == "blocked"
    assert contract["implementation_allowed"] is False
    assert contract["target"]["compiler"] == "MSVC"
    assert contract["target"]["language_standard"] == "C++17"
    forbidden = set(contract["forbidden_implementations"])
    assert {"fake_microphone_backend", "python_ctypes_capture", "in_process_wasapi"} <= forbidden


def test_all_decision_and_enablement_gates_are_declared() -> None:
    protocol = _load(PROTOCOL_PATH)
    lock = _load(LOCK_PATH)

    assert protocol["required_gates"] == list(DECISION_GATES)
    assert lock["required_gates"] == list(DECISION_GATES)
    assert protocol["enablement_gates"] == ["G3b", "G4b"]
    assert lock["enablement_gates"] == ["G3b", "G4b"]


def test_pinned_validation_rejects_unresolved_helper_lock_template() -> None:
    errors = validate_scaffold(allow_template=False)

    assert any("template placeholder" in error for error in errors)


def test_protocol_validation_rejects_weakened_transport_and_boundary() -> None:
    protocol = _load_json(PROTOCOL_PATH)
    protocol["transport"]["remote_clients"] = "accepted"
    protocol["authentication"]["verify_before_open"] = False
    protocol["audio_boundary"]["native_handles_cross_boundary"] = True
    protocol["reject_before_payload_allocation"] = []
    errors: list[str] = []

    _validate_protocol(protocol, errors)

    assert any("remote_clients" in error for error in errors)
    assert any("verify_before_open" in error for error in errors)
    assert any("native_handles_cross_boundary" in error for error in errors)
    assert any("pre-allocation" in error for error in errors)


def test_pinned_lock_validation_rejects_invalid_provenance_and_identity() -> None:
    lock = _load_json(LOCK_PATH)
    helper = lock["artifacts"]["voiceink-audio-helper-windows-x64"]
    helper.update(
        {
            "version": "1.0.0",
            "provenance_url": "http://example.invalid/helper",
            "sha256": "not-a-sha",
            "license": "GPL-3.0-only",
            "authenticode_publisher_thumbprint": "not-a-thumbprint",
        }
    )
    errors: list[str] = []

    _validate_lock(lock, errors, allow_template=False)

    assert any("HTTPS URL" in error for error in errors)
    assert any("SHA-256" in error for error in errors)
    assert any("thumbprint" in error for error in errors)


def test_contract_does_not_add_a_microphone_runtime_module() -> None:
    source_files = tuple((ROOT / "src").rglob("*.py"))

    assert not any(
        path.name in {"wasapi.py", "microphone.py", "audio_helper.py"} for path in source_files
    )


@pytest.mark.parametrize("path", [PROTOCOL_PATH, BUILD_PATH, LOCK_PATH])
def test_contract_files_are_ascii_json(path: Path) -> None:
    raw = path.read_bytes()

    raw.decode("ascii")
    json.loads(raw)
