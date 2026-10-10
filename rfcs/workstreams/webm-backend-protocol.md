# RFC: WebM Imported-Media Backend Protocol Failure

## Status

Implemented locally. This workstream is limited to the backend path from imported media
through FFmpeg normalization and the NeMo-Speech.cpp v0.2.0 HTTP adapter. UI,
microphone capture, and native Windows smoke are out of scope.

## Problem

Imported WebM transcription can surface the generic `RuntimeProtocolFailure`
message even though the source reaches the transcription runtime. The owned
loopback relay currently closes a quiet request after 30 seconds and has a
120-second total relay limit, while imported-media ASR requests may have a
30-minute stage deadline. During a long inference NeMo sends no bytes, so the
relay closes the connection and the bounded response reader classifies the
truncated HTTP response as `ProtocolError`.

The repository has no copy of the reported WebM recording, so the fix must be
anchored to the smallest deterministic backend seam and must not claim native
end-to-end evidence that has not been run.

## Goal

- Preserve the official NeMo-Speech.cpp v0.2.0 multipart request contract.
- Decode both `json` (`{"text": "..."}`) and `verbose_json` responses.
- Keep the loopback relay alive for the configured ASR request lifetime.
- Prevent valid long-form results from being rejected as protocol failures.
- Keep malformed or semantically invalid responses classified as
  `ProtocolError`.
- Preserve safe application-level error mapping.

## Scope

- `src/voiceink_win/infrastructure/loopback_proxy.py`
- `src/voiceink_win/infrastructure/sidecar_protocol.py` only if the regression
  exposes a decoder issue
- `src/voiceink_win/infrastructure/sidecar.py` only if the transport seam
  requires a call-site correction
- Focused loopback and sidecar regression tests
- This RFC and its acceptance evidence

The official response contract is:

- `json`: an object containing string `text`;
- `verbose_json`: `text`, `task`, `language`, `duration`, and `words[]`;
- each word uses `word`, `start`, and `end`, with offsets in seconds;
- non-2xx responses use `{"error": {"message": "...", "type": ...}}`.

## Dependencies

- Existing canonical audio and transcript domain models.
- Existing imported-media application error mapping.
- NeMo-Speech.cpp v0.2.0 API contract.
- Python 3.12-3.14 quality environment for the repository checks.
- A redacted native response or the reported WebM fixture is still required
  for native reproduction and final Windows smoke evidence.

## Acceptance Criteria

- A focused regression test fails before the fix against the confirmed backend
  relay timeout pattern.
- The test passes after the fix and proves a complete HTTP response reaches the
  client after a quiet inference interval.
- A quiet relay does not close before the request deadline, while relay
  cleanup and malformed HTTP handling remain bounded.
- Invalid JSON, missing/non-string `text`, negative/non-finite duration, and
  invalid word timestamps remain protocol failures.
- Existing legacy sidecar protocol tests remain unchanged and passing.
- `pytest`, `ruff`, and the repository's applicable `make` quality targets
  pass under a supported Python version.
- No UI or microphone files are changed.
- The RFC records that native WebM end-to-end validation remains unavailable
  without the user-provided fixture and Windows runtime.

## Out of Scope

- Changing FFmpeg codec support or media admission limits.
- Changing NeMo-Speech.cpp, model files, or runtime startup arguments.
- Relaxing transcript invariants without a failing regression test.
- UI copy, presentation behavior, or localization changes.
- Adding a synthetic WebM fixture that is not representative of the report.

## Open Evidence Gap

The exact user recording is not present in the workspace, and the native
Windows FFmpeg plus NeMo runtime cannot be executed on this macOS workstation.
The implementation can therefore prove the protocol behavior at the adapter
seam, but native reproduction must be rerun with the original recording.
