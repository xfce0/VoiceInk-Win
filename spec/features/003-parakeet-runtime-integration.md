# Feature: Parakeet Runtime Integration

## Status and Scope

Status: proposed.

This feature connects the existing ASR port to a verified local
NeMo-Speech.cpp sidecar running Parakeet TDT 0.6B V3. It covers batch
transcription of already-normalized audio, runtime/model lifecycle, typed
failures, authenticated loopback transport, and native Windows CPU
validation. The portable diagnostic remains media-only and intentionally emits
`asr.not_configured`; real-runtime events belong to the native smoke and
future application composition root. The feature excludes UI, microphone
recording, streaming, history, text delivery, automatic downloads, and CUDA
performance claims.

## User Scenarios

- A configured user submits imported audio and receives a non-empty local transcript.
- A user without a configured runtime receives an explicit setup error rather than a fake success.
- A user with a missing or modified model receives a safe checksum/configuration error.
- A request timeout, cancellation, or sidecar crash does not publish partial or stale text.
- A developer can run deterministic ASR contract tests without native binaries or model weights.
- A local listener that did not originate from the owned sidecar cannot receive audio or forge readiness.

## Functional Requirements

1. The runtime must implement the existing `AsrRuntime` port.
2. The runtime must accept only `AsrRequest` containing bounded canonical mono
   16 kHz signed PCM16 audio.
3. The runtime must verify the sidecar executable and Parakeet model manifest
   before launch and again before resuming a Windows suspended process.
4. The runtime must launch one loopback-only sidecar using an argument array,
   never a shell command.
5. The runtime must complete a readiness handshake before accepting requests.
6. The runtime must decode and validate the official NeMo-Speech.cpp
   `json`/`verbose_json` transcription response into the existing
   `TranscriptResult` domain value.
7. The runtime must expose capabilities and health separately from transcript text.
8. The runtime must classify missing model, unavailable backend, timeout,
   cancellation, crash, protocol, and execution failures distinctly.
9. The application service must bound concurrency and reject work after close.
10. The native smoke workflow must emit safe success metadata including
    transcript length and duration, but never transcript content or full paths.
11. The portable diagnostic workflow must remain media-only and retain
    `asr.not_configured`.
12. Native smoke must fail when requested real ASR configuration is missing or
    invalid.
13. The runtime must bind the server to loopback and generate a per-start API
     key through `NEMO_SPEECH_HTTP_API_KEY`; all `/v1` transcription requests
     must use `Authorization: Bearer`. Readiness must use the official `/ready`
     contract. The nonce header may be sent as an internal correlation value but
     is not treated as server-side authentication because the official runtime
     does not validate it.
14. The runtime must terminate and reap all owned sidecar resources on shutdown,
    timeout, crash recovery, and failed readiness.
15. `scripts/native_smoke.py` must be the native validation entrypoint and
    `python -m voiceink_win` must be the application composition root; `make
    run` must not point to a missing module.
16. The runtime manifest must be loaded through one shared loader. Native smoke
    environment variables may adapt into that loader but must not define a
    second configuration schema.
17. Artifact hashes and allowlists must come from a trusted, read-only artifact
    lock and may not be derived from the selected files at startup.
18. Native smoke must emit JSONL events in this order:
    `runtime.verified`, `model.verified`, `sidecar.ready`, `asr.completed`,
    `runtime.cleaned`.

## Non-Functional Requirements

- The canonical domain/application layers must not import subprocess, HTTP,
  Windows, CUDA, GGUF, or sidecar JSON types.
- Model and runtime binaries must remain outside Git.
- Logs must not contain raw audio, transcript text, secrets, or full local paths.
- CPU is mandatory for completion; CUDA is optional evidence and must be
  explicitly labelled by host/backend.
- The canonical configuration is a versioned JSON manifest; native smoke
  environment variables are only an adapter into that schema.
- Runtime and model checksums, provenance, version/revision, license, and
  explicit `executable_allowed_path` and `model_allowed_path` allowlists must
  be captured in the runtime manifest.
- A selected path may only resolve inside the trusted lock's approved install
  root; UNC paths, device paths, reparse points, symlinks, aliases, and
  case-insensitive canonical path mismatches are rejected.
- Cross-platform tests must run without Windows, CUDA, or model installation.

## Error and Cancellation Behavior

The runtime must return typed errors for configuration, missing model, backend
unavailability, runtime unavailability, timeout, cancellation, process crash,
protocol failure, and execution failure. A runtime exception must never become
an empty successful transcript.

Cancellation is best effort: the request is cancelled, transport output is
discarded, and cleanup runs before the caller observes the terminal state.
Late output cannot overwrite a newer request or result.

Only transient startup, transport timeout, process-crash, and transient backend
availability failures may use the existing bounded restart policy. There are at
most two total attempts per admitted job. Explicit
backend incompatibility, invalid configuration, missing model, malformed
protocol, and cancellation are not retried. No retry may silently switch from
CUDA to CPU.

## Acceptance Criteria

- A fake sidecar produces a validated `TranscriptResult` through the real
  adapter boundary.
- Missing and mismatched artifacts fail before process launch.
- Failed readiness cleans up the process and reports a typed error.
- Timeout/crash recovery does not leave a live sidecar or publish stale output.
- Two successful requests reuse one ready sidecar process.
- An unauthenticated or wrong-process loopback listener fails readiness and
  receives no PCM audio.
- A forged request after readiness is rejected without reading its PCM body.
- A native Windows CPU smoke run produces a non-empty transcript and exit code
  `0` with sanitized diagnostics.
- A missing runtime/model configuration produces a non-zero setup result.
- Completion requires an approved reference corpus, transcript-quality metric
  and threshold, CPU latency/RTFx threshold, and RSS ceiling, all passing in
  the native report.
- The initial quality gate is normalized word-level WER over at least ten
  approved clips, using Unicode NFKC, lowercase, punctuation removal, and
  collapsed whitespace; the target is WER `<= 15%`. Warm CPU inference must
  achieve `RTFx >= 1.0` and peak RSS must be `<= 4 GiB` on the named Windows
  baseline.
- Required third-party notices for the sidecar, model, FFmpeg, and native
  dependencies are present before the feature can be marked complete.
- `make check` passes on the development machine.

## Test Plan

- Unit tests for manifests, configuration, protocol decoding, error mapping,
  health/capabilities, deadlines, cancellation, restart, and cleanup.
- In-process loopback integration test for request/response framing and bounded
  response handling.
- Security test with a competing loopback listener and wrong handshake nonce.
- Native Windows CPU smoke test with externally provisioned pinned artifacts.
- Benchmark run with cold startup, model load, warm-up, ten warm inferences,
  p50/p95/p99 latency, RTFx, RSS, and error rate.
- Quality-gate test records the corpus hash, normalized WER/CER (or approved
  metric), threshold, and pass/fail result.
- Retry tests prove one application-owned budget of two total job attempts and
  no hidden second transcription attempt inside runtime recovery.
- Event tests prove the required order, stable failure codes, and exit codes:
  `0` success, `2` setup failure, `3` runtime/protocol failure, and `4`
  quality/latency/memory/cleanup failure.
- Redaction tests assert that reports contain no transcript text, raw audio,
  secrets, full local paths, model paths, or unredacted exception strings.

## Open Questions and Deferred Work

- Pin the exact NeMo-Speech.cpp release/commit and supported HTTP API.
- Pin the first CPU model quantization and SHA-256.
- Select a redistributable fixture and approve the proposed WER/RTFx/RSS
  thresholds.
- Define trusted artifact-lock storage and retention for CI and manual Windows
  validation.
- Defer automatic downloads, installer packaging, live streaming, CUDA
  benchmarking, and GPU auto-selection.
