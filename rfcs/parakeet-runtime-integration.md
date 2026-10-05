# RFC: Parakeet Runtime Integration

## Status

Proposed. This RFC defines Work 3. Cross-platform adapter, protocol, and
configuration tests may begin after approval. Native smoke and release
validation remain blocked until the exact runtime/model artifacts, fixture, and
quality thresholds are pinned.

## Summary

Add a real offline Parakeet TDT 0.6B V3 transcription path to the production
composition root and native smoke entrypoint. The portable diagnostic build
remains a media-only snapshot/FFmpeg diagnostic and continues to report
`asr.not_configured` by design. The application will keep using the existing
typed ASR port and canonical audio contract. A long-lived NeMo-Speech.cpp
sidecar will own native inference, while Python owns artifact
verification, process lifecycle, loopback transport, request deadlines,
cancellation policy, and safe diagnostics.

```text
CanonicalAudio
      |
      v
AsrApplicationService
      |
      v
NeMoSidecarRuntime
      |
      +--> verified sidecar executable
      +--> verified Parakeet V3 GGUF model
      |
      v
127.0.0.1 loopback HTTP
      |
      v
TranscriptResult
```

Work 3 is an integration and validation milestone, not the desktop UI or live
microphone milestone.

The observable real-runtime events belong to `scripts/native_smoke.py` and the
future application composition root: `runtime.verified`, `model.verified`,
`sidecar.ready`, `asr.completed`, and `runtime.cleaned`. The portable
`scripts/diagnostic_cli.py` path has a separate media-only event contract and is
not evidence of ASR availability.

Work 3 has two explicit entrypoints:

- `scripts/native_smoke.py` is the Windows validation entrypoint and must load
  the canonical runtime manifest through the shared loader.
- `python -m voiceink_win` is the future application composition root. Work 3
  must add it or remove the stale `make run` target; a missing module is not an
  acceptable runtime state.

The native smoke exit codes are `0` for a passing pipeline, `2` for missing or
invalid setup, `3` for runtime/protocol failure, and `4` for a failed quality,
latency, memory, or cleanup gate.

## Context

The repository now has the platform-independent ASR port, canonical PCM16
audio value, application lifecycle service, sidecar protocol boundary,
artifact verifier, process supervisor, imported-media workflow, and a real
Windows diagnostic smoke result. The smoke successfully validated source
snapshotting and FFmpeg normalization on Windows ARM64 under x64 emulation,
but intentionally stopped at `asr.not_configured` because no runtime or model
was included.

The public product contract requires local Parakeet TDT 0.6B V3 transcription
with CPU fallback and an NVIDIA CUDA path. The first runtime must therefore be
isolated from Python and validated on Windows without weakening the macOS fake
adapter test suite.

## Goals

- Run a real local Parakeet TDT 0.6B V3 batch transcription through the existing ASR port.
- Pin and verify the exact sidecar executable and model artifacts before launch.
- Support a CPU backend as the mandatory path.
- Preserve an explicit `cuda:<index>` backend configuration for later NVIDIA validation.
- Keep runtime-specific paths, arguments, JSON, subprocess, HTTP, and CUDA details outside the domain layer.
- Start one long-lived loopback-only sidecar and reuse it for multiple requests.
- Map runtime, protocol, timeout, cancellation, missing-model, and backend failures to typed errors.
- Publish safe diagnostics without raw audio, transcript text, tokens, or full paths.
- Provide a reproducible native Windows smoke command that proves non-empty transcription.
- Produce benchmark metadata for cold startup, model load, warm inference, and end-to-end latency.
- Establish a measurable transcript-quality gate against an approved reference corpus.

## Non-Goals

- PySide6 UI, system tray, overlay, or file picker.
- WASAPI microphone recording or global hotkeys.
- Token-by-token streaming transcription.
- Automatic model downloads or background updates.
- SQLite history, text delivery, clipboard, or `SendInput`.
- Cloud transcription or AI enhancement.
- CUDA performance claims without a real NVIDIA Windows machine.
- Bundling model weights or native runtime binaries into Git.

## Proposed Architecture

### Runtime boundary

Use the existing domain `AsrRuntime` port, implemented by
`NeMoSidecarRuntime`, and the existing `SubprocessSupervisor` rather than
importing a native inference library into Python. The sidecar binds only
to `127.0.0.1` or `::1`, receives canonical PCM16 bytes, and returns the
versioned `voiceink.asr.result.v1` payload.

The sidecar process is long-lived. Per-request process startup is rejected for
the production path because it reloads the model and makes cancellation,
latency, and resource ownership unreliable.

The sidecar transport is authenticated even though it is local. The supervisor
requests an OS-selected ephemeral loopback port and generates a per-start
cryptographic nonce. The nonce is supplied to the sidecar through its inherited
environment and sent in the `X-VoiceInk-ASR-Nonce` header. Readiness must prove
the same nonce, sidecar PID, protocol version, model hash, and backend. The
nonce header is required on every health and transcribe request, is validated
before the request body is read, and is compared with a constant-time
comparison. A listener
that did not originate from the owned process must fail the handshake. If the
selected sidecar cannot support this handshake, a local authenticated proxy or
another transport must be used; an unauthenticated fixed-port HTTP endpoint is
not an acceptable production boundary.

### Model and backend

The target model is Parakeet TDT 0.6B V3 in the exact GGUF artifact supported
by the pinned NeMo-Speech.cpp release. The exact runtime commit, model filename,
quantization, download URL, license notices, and SHA-256 are blocking release
inputs and must be recorded in a checked-in manifest template without adding
the binary artifacts themselves.

CPU is the required first backend. CUDA is configuration-compatible but remains
blocked from completion until a real NVIDIA Windows host provides benchmark
evidence. There is no silent CPU fallback from an explicitly requested CUDA
backend; any fallback must be a future visible policy decision.

### Artifact policy

The runtime executable and model are externally provisioned. Work 3 verifies
both artifacts immediately before launch and revalidates their identity/hash
after any Windows handle lock is acquired and before the suspended process is
resumed. A missing or mismatched artifact is a typed setup failure, never a
successful empty transcript.

Model and runtime paths are supplied through an explicit runtime configuration
file or CLI options. They are never interpolated into a shell command. The
configuration may contain paths and hashes but must not contain secrets.

### Protocol

Requests use:

- `Content-Type: application/octet-stream`;
- canonical PCM16 bytes as the body;
- compact JSON metadata in `X-VoiceInk-ASR-Metadata`;
- protocol version `1` and schema `voiceink.asr.request.v1`.

Responses use JSON schema `voiceink.asr.result.v1` with text, duration,
optional segment timestamps, and optional detected language. The adapter must
validate the complete response before constructing `TranscriptResult`.

### Cancellation

Work 3 keeps the foundation best-effort cancellation policy. The application
marks the request cancelled, interrupts the transport, discards late output,
and cleans up. The supervisor terminates the sidecar only for process failure,
shutdown, or a lifecycle deadline; hard cancellation inside native inference
is not promised.

## Configuration and Manifests

The implementation must add a versioned JSON runtime manifest and a trusted
artifact lock. The lock is the source of expected SHA-256 values, canonical
allowed paths, provenance, version, and license. A user-selectable path or
mutable environment variable may select an artifact only within the lock's
approved install root; it may not change the expected hash. The lock is
read-only during a run and is provisioned from a checked-in release record or a
trusted CI secret, not generated from the selected files at startup.

The manifest is the canonical configuration source for the application and
native smoke. This manifest is
native smoke environment variables remain a setup interface and are converted
to this same manifest loader; they are not a second schema. Precedence is
explicit CLI override, then manifest file, then no implicit defaults for
artifact paths or hashes.

The manifest model is equivalent to:

```json
{
  "schema": "voiceink.runtime.manifest.v1",
  "executable": "C:\\VoiceInk\\runtime\\nemo-speech.exe",
  "executable_allowed_path": "C:\\VoiceInk\\runtime\\nemo-speech.exe",
  "install_root": "C:\\VoiceInk",
  "executable_sha256": "<64 hex characters>",
  "executable_version": "<pinned release or commit>",
  "executable_provenance_url": "https://...",
  "model": "C:\\VoiceInk\\models\\parakeet-tdt-0.6b-v3.gguf",
  "model_allowed_path": "C:\\VoiceInk\\models\\parakeet-tdt-0.6b-v3.gguf",
  "model_sha256": "<64 hex characters>",
  "model_version": "<pinned model revision>",
  "model_provenance_url": "https://...",
  "model_license": "CC-BY-4.0",
  "backend": "cpu",
  "endpoint": "ephemeral-loopback"
}
```

The checked-in example must use placeholders and must not point to a mutable
branch or an unverified download. The native smoke workflow receives real
values through protected configuration or downloaded test artifacts and emits
only sanitized manifest metadata.

The `*_allowed_path` fields are security allowlists and must exactly match the
canonical artifact paths before launch. Reports may include artifact IDs, versions, provenance URLs, licenses, and
SHA-256 values, but never `allowed_path`, source paths, model paths, or
unredacted exception strings. Absolute-path scanning is part of report tests.

The lock and path validator must reject UNC paths, device paths, reparse
points, symbolic links, aliases outside `install_root`, and case-insensitive
path mismatches after canonical Windows normalization. Hash and identity are
checked through the opened file handle immediately before process resume.

## Event and Report Contract

Native smoke writes JSONL events with this schema:

```json
{
  "schema": "voiceink.native-smoke.event.v1",
  "event": "asr.completed",
  "run_id": "<opaque id>",
  "timestamp_utc": "<RFC 3339 timestamp>",
  "duration_seconds": 7.78,
  "backend": "cpu",
  "model_id": "parakeet-tdt-v3",
  "transcript_length": 42
}
```

Required success order is `runtime.verified`, `model.verified`,
`sidecar.ready`, `asr.completed`, `runtime.cleaned`. Every event is sanitized
before serialization. Reports must never include transcript text, PCM/audio,
authorization headers, absolute paths, model paths, or raw exception strings.
Failure reports use a stable `error_code` and sanitized `error_type` only.

The implementation must document all third-party notices for the sidecar,
Parakeet model, FFmpeg, and transitive native dependencies before a distributable
bundle is created.

## Lifecycle

1. Load and validate runtime configuration.
2. Verify the executable and model paths, regular-file status, allowed paths,
   manifests, identities, and SHA-256 values.
3. Start the sidecar suspended on the configured loopback endpoint.
4. Assign the Windows Job Object before resuming the process.
5. Probe readiness within the startup deadline.
6. Complete the authenticated capability/health handshake and verify that
   protocol version, model ID, model hash, and backend match configuration.
7. Submit bounded canonical audio through the ASR application service.
8. Validate and return `TranscriptResult`.
9. Reuse the ready process for subsequent requests.
10. On timeout/crash, close transport, terminate the process tree, reap all
    resources, and return one typed failure to the application retry coordinator
    within the remaining request deadline.
11. On application shutdown, drain requests, stop the sidecar, and report
    pending cleanup as a typed recovery failure.

## Functional Requirements

1. With a valid CPU configuration, a valid Parakeet model, and a supported WAV
   fixture, the runtime reaches `ready` and returns a non-empty transcript.
2. The first transcription request must use the existing `AsrRequest` and
   `CanonicalAudio` types; no file path may cross the ASR port.
3. The runtime must reject a missing, changed, symlinked, reparse-point, or
   checksum-mismatched executable/model before process launch.
4. A sidecar that fails readiness must be terminated and must not remain alive.
5. A malformed response, unsupported schema, oversized response, or unknown
   error code must become `ProtocolError`.
6. Runtime failures must preserve the distinction between unavailable runtime,
   missing model, unavailable backend, timeout, cancellation, process crash,
   and execution failure.
7. A late response after cancellation or deadline must never publish text.
8. The portable diagnostic path must retain `asr.not_configured` because it is
   media-only. The configured native smoke and future application composition
   root must report `asr.completed` with safe metadata.
9. The native smoke path must return a non-zero setup status when the runtime,
   model, handshake, or fixture is unavailable.
10. Normalized audio, runtime responses, and model paths must not be written to
    ordinary logs.

## Non-Functional Requirements

- Local processing only; no network access from the application except the
  loopback sidecar and explicitly provisioned artifact setup.
- No shell invocation for the sidecar or model.
- The sidecar endpoint must be loopback-only and validated at configuration time.
- Response memory must remain bounded by the configured response limit.
- The runtime must support Python 3.12-3.14 and remain testable without native
  binaries or model weights.
- PCM validation, framing, and transport are `O(n)` in canonical samples with
  `O(n)` audio storage. Native neural inference complexity is runtime-specific
  and unspecified; latency and RSS are measured rather than guessed.
- Each benchmark report must include OS/build, CPU/RAM, GPU/VRAM/driver when
  applicable, Python, runtime commit/version, model revision, quantization,
  checksums, backend, thread count, fixture manifest hash, cold/warm mode, and
  timestamp.

## Error and Recovery Behavior

| Condition | Result | Retry |
|---|---|---|
| Missing or invalid configuration | typed configuration failure | no |
| Missing model | `MissingModelError` | no |
| Model/executable checksum mismatch | typed configuration failure | no |
| Sidecar readiness timeout | `RuntimeUnavailableError` | one recovery attempt if the deadline allows |
| Explicit backend configuration is incompatible | typed configuration failure | no |
| Backend temporarily unavailable during startup/probe | `BackendUnavailableError` | one bounded restart/retry |
| Sidecar process crash | `ProcessCrashedError` | one bounded restart attempt |
| Transport timeout | `AsrTimeoutError` | one bounded restart attempt |
| Malformed response | `ProtocolError` | no |
| Caller cancellation | `CancellationError` | no |
| Cleanup deadline exceeded | `RuntimeRecoveryPendingError` | explicit later recovery |

The application/import workflow is the sole retry coordinator. It owns a
maximum of two total job attempts and the absolute processing deadline. The
sidecar runtime may restart a dead process only to restore lifecycle ownership
and readiness; it must not issue a hidden second transcription attempt. The
restart is reported as part of the same job attempt and the typed failure is
then classified by the application. This preserves the
existing imported-media policy: `BackendUnavailableError` is retryable only for
transient runtime availability, while a manifest/backend incompatibility is a
non-retryable configuration failure. No retry may silently switch from CUDA to
CPU. There are at most two total attempts per admitted job. After the second
attempt, the last typed failure is terminal and no further restart is allowed.

## Acceptance Criteria

- `make check` passes without a native runtime or model installed.
- Unit tests cover configuration validation, manifest parsing, checksum
  failures, safe argv construction, readiness failure, protocol decoding,
  cancellation, timeout, crash restart, and cleanup recovery.
- A fake sidecar integration test returns a deterministic transcript through
  the real transport and protocol boundaries.
- A native Windows CPU smoke run with pinned artifacts records:
  `runtime.verified`, `model.verified`, `sidecar.ready`, `asr.completed`, and
  `runtime.cleaned`, exits `0`, and reports a non-zero transcript length.
- The native smoke run exits non-zero for missing/mismatched artifacts and does
  not report a synthetic success.
- The same native smoke fixture can be processed twice through one long-lived
  sidecar without restarting the process between successful requests.
- A simulated crash or timeout demonstrates process-tree cleanup before a
  retry and leaves no owned runtime process behind.
- A competing or wrong-process loopback listener fails the authenticated
  readiness handshake and receives no PCM audio.
- A forged request after a successful handshake is rejected unless it includes
  the correct `X-VoiceInk-ASR-Nonce` header.
- The report contains no raw audio, transcript text, secrets, authorization
  headers, full local paths, model paths, or unredacted exception strings.
- CPU benchmark evidence includes one cold run, one warm-up, and at least ten
  warm runs, with startup, model-load, p50/p95/p99, RTFx, peak RSS, and error
  rate recorded.
- Work 3 cannot move to `Complete` until an approved reference corpus,
  transcript-quality metric and threshold, CPU latency/RTFx threshold, and RSS
  ceiling are recorded and the native report contains a passing quality gate.
- The initial quality baseline is normalized WER over at least ten approved
  clips: Unicode NFKC, lowercase, punctuation removed, and whitespace
  collapsed; WER is word-level Levenshtein distance divided by reference word
  count and must be `<= 15%`. Warm CPU inference must achieve `RTFx >= 1.0`
  and peak RSS must be `<= 4 GiB` on the named Windows baseline. These values
  may be changed only by an RFC amendment before implementation is marked
  complete.
- CUDA remains explicitly marked `blocked` unless the benchmark was run on a
  real NVIDIA Windows machine.

## Test Plan

### macOS and cross-platform tests

- Validate configuration and manifests with temporary files.
- Exercise the real JSON protocol decoder with valid, malformed, oversized,
  and semantically invalid payloads.
- Use an in-process fake sidecar server for transport and lifecycle tests.
- Use fake process/supervisor boundaries for crash, timeout, and cleanup paths.
- Verify diagnostics redaction and absence of transcript/audio content.
- Verify event order, stable failure codes, absolute-path redaction, and
  failure-report sanitization.
- Keep all tests independent of PySide6, Windows APIs, CUDA, and model weights.

### Windows validation

- Provision the exact sidecar executable and Parakeet GGUF artifact from pinned
  manifests.
- Run the existing imported-media fixture through the real runtime.
- Repeat the request through one long-lived sidecar.
- Validate CPU transcript output, safe diagnostics, process cleanup, and
  artifact verification.
- Run the same smoke on Windows ARM64 under x64 emulation and on native x64
  Windows if available; report them as separate environments.
- Run CUDA only on a real NVIDIA Windows host and keep it out of mandatory PR
  checks until a reproducible runner exists.

## Open Questions

Blocking for native smoke/release:

1. Which exact NeMo-Speech.cpp release or commit provides the production HTTP
   endpoint and Parakeet TDT 0.6B V3 GGUF compatibility?
2. Which model quantization is the first CPU target: F16, Q8_0, or another
   verified artifact?
3. What public, redistributable fixture and expected transcript are approved
   for native smoke and accuracy checks?
4. Where are the trusted artifact lock and externally provisioned runtime/model
   artifacts stored for CI and manual Windows validation?
5. Is the proposed baseline of WER `<= 15%`, warm CPU `RTFx >= 1.0`, and peak
   RSS `<= 4 GiB` accepted for the named Windows baseline?

Not blocking cross-platform adapter work:

- The adapter, protocol, canonical JSON manifest loader, fake sidecar, and
  report-redaction tests may be implemented with synthetic artifacts and a fake
  server before native pins are available.

Deferred decisions:

- Automatic model download and update verification.
- Installer and licensing UX.
- GPU auto-selection and visible fallback policy.
- Streaming transcription and a live-mode model.
- Persistent runtime cache management.

## Exit Criteria

Work 3 is complete when the exact runtime/model artifacts are pinned, the CPU
sidecar produces a non-empty and quality-gated transcript on Windows, the
authenticated native smoke and cross-platform gates pass, benchmark evidence
meets the approved latency/RSS thresholds, the required third-party notices
are documented, and this RFC plus the affected feature/spec documents are
updated from `Proposed` to `Complete`.
