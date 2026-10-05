"""Versioned sidecar protocol decoding kept outside the runtime lifecycle."""

from __future__ import annotations

import json

from voiceink_win.domain import (
    AsrTimeoutError,
    BackendUnavailableError,
    CancellationError,
    ConfigurationError,
    ExecutionError,
    InvalidInputError,
    MissingModelError,
    ProcessCrashedError,
    ProtocolError,
    Timestamp,
    TranscriptResult,
    TranscriptSegment,
    WordTimestamp,
)

from .transport import TransportResponse

PROTOCOL_VERSION = 1
REQUEST_SCHEMA = "voiceink.asr.request.v1"
RESULT_SCHEMA = "voiceink.asr.result.v1"
AUDIO_CONTENT_TYPE = "application/octet-stream"
AUDIO_METADATA_HEADER = "X-VoiceInk-ASR-Metadata"


def map_error_response(response: TransportResponse):
    code = ""
    try:
        payload = json.loads(response.body.decode("utf-8"))
        if isinstance(payload, dict):
            if payload.get("protocol_version") != PROTOCOL_VERSION:
                return ProtocolError("sidecar returned an unsupported error protocol")
            value = payload.get("code", payload.get("error", ""))
            if isinstance(value, dict):
                value = value.get("code", "")
            if isinstance(value, str):
                code = value.lower()
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    if code in {"missing_model", "model_not_found"}:
        return MissingModelError("sidecar model is missing")
    if code in {"backend_unavailable", "backend_not_available"}:
        return BackendUnavailableError("sidecar backend is unavailable")
    if code in {"cancelled", "canceled"}:
        return CancellationError("sidecar cancelled the request")
    if code in {"timeout", "deadline_exceeded"}:
        return AsrTimeoutError("sidecar request timed out")
    if code in {"invalid_input", "bad_request"}:
        return InvalidInputError("sidecar rejected the ASR input")
    if code == "process_crashed":
        return ProcessCrashedError("sidecar process crashed")
    if code in {"execution", "runtime_error"}:
        return ExecutionError("sidecar execution failed")
    return ProtocolError("sidecar returned an unknown protocol error")


def map_nemo_error_response(response: TransportResponse):
    try:
        payload = json.loads(response.body.decode("utf-8"))
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        message = error.get("message", "") if isinstance(error, dict) else ""
        error_type = error.get("type", "") if isinstance(error, dict) else ""
    except (UnicodeDecodeError, json.JSONDecodeError):
        message = ""
        error_type = ""
    if response.status_code == 401:
        return ConfigurationError("NeMo-Speech.cpp API key was rejected")
    if response.status_code == 400 or error_type == "invalid_request_error":
        return InvalidInputError(message or "NeMo-Speech.cpp rejected the ASR input")
    return ExecutionError(message or "NeMo-Speech.cpp ASR request failed")


def decode_result(body: bytes) -> TranscriptResult:
    try:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("result must be an object")
        if payload.get("protocol_version") != PROTOCOL_VERSION:
            raise ValueError("unsupported sidecar protocol version")
        if payload.get("schema") != RESULT_SCHEMA:
            raise ValueError("unsupported sidecar result schema")
        segments = tuple(_decode_segment(item) for item in payload.get("segments", []))
        return TranscriptResult(
            text=payload["text"],
            duration=payload["duration"],
            segments=segments,
            detected_language=payload.get("detected_language"),
        )
    except (
        InvalidInputError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise ProtocolError(
            "sidecar returned a malformed transcript result", cause=error
        ) from error


def decode_nemo_result(body: bytes, default_duration: float) -> TranscriptResult:
    try:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise ValueError("result must contain text")
        duration = payload.get("duration", default_duration)
        words = tuple(
            WordTimestamp(
                word=item["word"],
                timestamp=Timestamp(item["start"], item["end"]),
            )
            for item in payload.get("words", [])
        )
        segments = (
            (
                TranscriptSegment(
                    text=payload["text"],
                    timestamp=Timestamp(0.0, duration),
                    words=words,
                ),
            )
            if payload["text"].strip()
            else ()
        )
        return TranscriptResult(
            text=payload["text"],
            duration=duration,
            segments=segments,
            detected_language=payload.get("language"),
        )
    except (
        InvalidInputError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as error:
        raise ProtocolError(
            "NeMo-Speech.cpp returned a malformed transcript result", cause=error
        ) from error


def _decode_segment(value: object) -> TranscriptSegment:
    if not isinstance(value, dict):
        raise ValueError("segment must be an object")
    words = tuple(_decode_word(word) for word in value.get("words", []))
    return TranscriptSegment(
        text=value["text"],
        timestamp=Timestamp(value["start"], value["end"]),
        words=words,
    )


def _decode_word(value: object) -> WordTimestamp:
    if not isinstance(value, dict):
        raise ValueError("word must be an object")
    return WordTimestamp(
        word=value["word"],
        timestamp=Timestamp(value["start"], value["end"]),
    )
