"""Validate the disabled Windows WASAPI contract scaffold."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "spec" / "wasapi-protocol-v1.json"
BUILD_PATH = ROOT / "packaging" / "windows-wasapi-helper-build-contract.json"
LOCK_PATH = ROOT / ".github" / "native-smoke" / "wasapi-helper-artifact-lock.template.json"
DOC_PATH = ROOT / "docs" / "windows-wasapi-native-slice-blocked.md"

DECISION_GATES = tuple(f"D{number}" for number in range(1, 20)) + (
    "G1",
    "G2",
    "G3a",
    "G4a=GO",
    "decision_sync",
    "implementation_plan",
)
ENABLEMENT_GATES = ("G3b", "G4b")
PLACEHOLDER_PREFIX = "REPLACE_WITH_"
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_THUMBPRINT = re.compile(r"^[0-9a-fA-F]{40}$")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _require(value: Mapping[str, Any], keys: set[str], label: str, errors: list[str]) -> None:
    if set(value) != keys:
        errors.append(f"{label} keys must be {sorted(keys)}")


def _validate_disabled(value: Mapping[str, Any], label: str, errors: list[str]) -> None:
    if value.get("status") != "blocked":
        errors.append(f"{label} must remain blocked")
    if value.get("implementation_allowed") is not False:
        errors.append(f"{label} must keep implementation_allowed=false")


def _validate_gate_list(value: Mapping[str, Any], label: str, errors: list[str]) -> None:
    if value.get("required_gates") != list(DECISION_GATES):
        errors.append(f"{label} required gates do not match D1-D19/G1/G2/G3a/G4a")
    if value.get("enablement_gates") != list(ENABLEMENT_GATES):
        errors.append(f"{label} enablement gates do not match G3b/G4b")


def _validate_protocol(value: Mapping[str, Any], errors: list[str]) -> None:
    if value.get("schema") != "voiceink.wasapi.protocol.v1" or value.get("version") != 1:
        errors.append("protocol contract schema/version is unsupported")
    _validate_disabled(value, "protocol contract", errors)
    _validate_gate_list(value, "protocol contract", errors)
    transport = value.get("transport")
    expected_transport = {
        "kind": "current-user-named-pipe",
        "pipe_owner": "parent",
        "helper_receives_inherited_server_handle": True,
        "remote_clients": "rejected",
        "reconnect": False,
    }
    if not isinstance(transport, Mapping):
        errors.append("protocol transport contract is missing")
    else:
        for field, expected in expected_transport.items():
            if transport.get(field) != expected:
                errors.append(f"protocol transport {field} must be {expected!r}")
    authentication = value.get("authentication")
    expected_authentication = {
        "bootstrap_challenge_bytes": 32,
        "session_key_derivation": "HKDF-SHA-256(challenge,generation)",
        "direction_specific_hmac": "HMAC-SHA-256",
        "verify_connected_server_pid": True,
        "verify_before_open": True,
    }
    if not isinstance(authentication, Mapping):
        errors.append("protocol authentication contract is missing")
    else:
        for field, expected in expected_authentication.items():
            if authentication.get(field) != expected:
                errors.append(f"protocol authentication {field} must be {expected!r}")
    frame = value.get("frame")
    if not isinstance(frame, Mapping):
        errors.append("protocol frame contract is missing")
        return
    expected_frame = {
        "magic": "VIKA",
        "max_payload_bytes": 65536,
        "canonical_chunk_bytes": 32768,
        "aggregate_ipc_queue_bytes": 4194304,
    }
    for field, expected in expected_frame.items():
        if frame.get(field) != expected:
            errors.append(f"protocol frame {field} must be {expected!r}")
    if value.get("messages") != [
        "HELLO",
        "OPEN",
        "START",
        "AUDIO",
        "STOP",
        "CANCEL",
        "STATUS",
        "RESULT",
        "CLOSE",
    ]:
        errors.append("protocol message set is not the approved RFC target")
    if value.get("reject_before_payload_allocation") != [
        "malformed",
        "replayed",
        "oversized",
        "out_of_order",
        "wrong_generation",
        "wrong_direction",
        "wrong_version",
        "wrong_hmac",
    ]:
        errors.append("protocol pre-allocation rejection set is incomplete")
    boundary = value.get("audio_boundary")
    expected_boundary = {
        "native_packets_cross_boundary": False,
        "native_handles_cross_boundary": False,
        "endpoint_ids_cross_boundary": False,
        "parent_final_value": "CanonicalAudio",
        "disk_spill": False,
    }
    if not isinstance(boundary, Mapping):
        errors.append("protocol audio boundary contract is missing")
    else:
        for field, expected in expected_boundary.items():
            if boundary.get(field) != expected:
                errors.append(f"protocol audio boundary {field} must be {expected!r}")


def _validate_build(value: Mapping[str, Any], errors: list[str]) -> None:
    if (
        value.get("schema") != "voiceink.wasapi.helper-build-contract.v1"
        or value.get("version") != 1
    ):
        errors.append("helper build contract schema/version is unsupported")
    _validate_disabled(value, "helper build contract", errors)
    _validate_gate_list(value, "helper build contract", errors)
    target = value.get("target")
    expected_target = {
        "architecture": "x64",
        "compiler": "MSVC",
        "language_standard": "C++17",
        "configuration": "Release",
    }
    if not isinstance(target, Mapping):
        errors.append("helper build target is missing")
    else:
        for field, expected in expected_target.items():
            if target.get(field) != expected:
                errors.append(f"helper build target {field} must be {expected!r}")
    output = value.get("output")
    if (
        not isinstance(output, Mapping)
        or output.get("relative_path") != "audio/voiceink-audio-helper.exe"
    ):
        errors.append("helper output path must be bundle-relative")
    forbidden = value.get("forbidden_implementations")
    if not isinstance(forbidden, list):
        errors.append("helper build contract forbidden implementations are missing")
    else:
        required = {
            "python_ctypes_capture",
            "fake_microphone_backend",
            "in_process_wasapi",
            "runtime_enablement_without_gates",
        }
        missing = required - set(forbidden)
        if missing:
            errors.append(f"helper build contract is missing prohibitions: {sorted(missing)}")


def _validate_lock(value: Mapping[str, Any], errors: list[str], *, allow_template: bool) -> None:
    if (
        value.get("schema") != "voiceink.wasapi.helper-artifact-lock.v1"
        or value.get("version") != 1
    ):
        errors.append("helper artifact lock schema/version is unsupported")
    _validate_disabled(value, "helper artifact lock", errors)
    _validate_gate_list(value, "helper artifact lock", errors)
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != {
        "voiceink-audio-helper-windows-x64"
    }:
        errors.append("helper artifact lock must contain exactly the x64 helper")
        return
    helper = artifacts["voiceink-audio-helper-windows-x64"]
    if not isinstance(helper, Mapping):
        errors.append("helper artifact lock entry must be an object")
        return
    expected_keys = {
        "kind",
        "version",
        "provenance_url",
        "sha256",
        "license",
        "allowed_relative_path",
        "authenticode_publisher_thumbprint",
        "signature_policy",
    }
    _require(helper, expected_keys, "helper artifact lock entry", errors)
    if helper.get("kind") != "signed-executable":
        errors.append("helper artifact lock kind must be signed-executable")
    if helper.get("allowed_relative_path") != "audio/voiceink-audio-helper.exe":
        errors.append("helper artifact lock path must match the build contract")
    if helper.get("signature_policy") != "Authenticode-required":
        errors.append("helper artifact lock must require Authenticode")
    for field in (
        "version",
        "provenance_url",
        "sha256",
        "license",
        "authenticode_publisher_thumbprint",
    ):
        if not isinstance(helper.get(field), str) or not helper[field].strip():
            errors.append(f"helper artifact lock {field} must be non-empty")
        elif PLACEHOLDER_PREFIX in helper[field]:
            if not allow_template:
                errors.append(f"helper artifact lock {field} still contains a template placeholder")
            continue
        elif field == "provenance_url":
            parsed = urlsplit(helper[field])
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                errors.append("helper artifact lock provenance_url must be an HTTPS URL")
        elif field == "sha256" and not _SHA256.fullmatch(helper[field]):
            errors.append("helper artifact lock sha256 must be a SHA-256 hex digest")
        elif field == "authenticode_publisher_thumbprint" and not _THUMBPRINT.fullmatch(
            helper[field]
        ):
            errors.append("helper artifact lock thumbprint must be a 40-character hex value")


def validate_scaffold(root: Path = ROOT, *, allow_template: bool = True) -> list[str]:
    root = root.resolve()
    protocol_path = root / "spec" / "wasapi-protocol-v1.json"
    build_path = root / "packaging" / "windows-wasapi-helper-build-contract.json"
    lock_path = root / ".github" / "native-smoke" / "wasapi-helper-artifact-lock.template.json"
    doc_path = root / "docs" / "windows-wasapi-native-slice-blocked.md"
    errors: list[str] = []
    try:
        protocol = _load_json(protocol_path)
        build = _load_json(build_path)
        lock = _load_json(lock_path)
    except ValueError as error:
        return [str(error)]
    _validate_protocol(protocol, errors)
    _validate_build(build, errors)
    _validate_lock(lock, errors, allow_template=allow_template)
    try:
        document = doc_path.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"blocked scaffold document cannot be read: {error}")
    else:
        for marker in (
            "implementation_allowed: false",
            "G4a",
            "G3b",
            "G4b",
            "not native evidence",
        ):
            if marker not in document:
                errors.append(f"blocked scaffold document is missing marker: {marker}")
    try:
        catalog = (root / "spec" / "catalog.yaml").read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"catalog cannot be read: {error}")
        return errors
    if "implementation_allowed: false" not in catalog or "g4a_outcome: OPEN" not in catalog:
        errors.append("catalog must keep microphone implementation and enablement disabled")
    return errors


def main() -> int:
    allow_template = "--require-pinned" not in sys.argv[1:]
    errors = validate_scaffold(allow_template=allow_template)
    if errors:
        for error in errors:
            print(f"wasapi-contract: {error}", file=sys.stderr)
        return 1
    mode = "template" if allow_template else "pinned"
    print(f"wasapi-contract: passed ({mode}; microphone remains disabled)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
