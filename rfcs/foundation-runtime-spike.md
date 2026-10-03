# RFC: Foundation and Parakeet Runtime Spike

## Status

Approved / In Progress. The maintainer approved implementation on the feature branch. Windows-native runtime, CUDA, model pinning, and benchmark evidence remain explicit validation gates before this RFC can become Complete.

## Summary

Create the first technical foundation for VoiceInk-Win and prove that Parakeet TDT 0.6B V3 can be used as a local transcription runtime on Windows with a CPU fallback and an NVIDIA CUDA path.

This RFC deliberately does not implement the complete desktop application. It establishes the boundaries that later recording, file import, UI, history, and text-delivery features will depend on.

## Context

Development currently happens on macOS, while the target product is Windows. Mac tests can validate platform-independent Python code, but cannot prove WASAPI, Windows global hooks, Windows clipboard behavior, MSVC/DLL loading, CUDA compatibility, or Windows packaging.

The existing Windows port demonstrates the basic product idea but has no file-import workflow, hard-coded CPU transcription, a prototype Tkinter UI, weak model lifecycle management, plaintext secrets, and fragile platform adapters. The macOS VoiceInk repository contains the desired product behavior and design ideas, but its SwiftUI, AppKit, CoreAudio, Accessibility, and FluidAudio integrations cannot be copied to Windows.

## Goals

- Define a typed, platform-independent ASR port.
- Define and validate the canonical audio contract.
- Provide a deterministic fake ASR runtime for tests on macOS.
- Implement a narrow adapter boundary for a long-lived `NeMo-Speech.cpp` sidecar.
- Validate a Windows CPU runtime smoke path.
- Define a separate CUDA validation path for a real NVIDIA machine.
- Define reproducible benchmark metadata and measurements.
- Add Windows CI for Python-level checks and runtime contracts.
- Make runtime failures, timeouts, cancellation, and process cleanup explicit.

## Non-Goals

- PySide6 UI, system tray, or recording overlay.
- WASAPI microphone recording.
- Windows global hotkeys.
- Clipboard and `SendInput` text delivery.
- FFmpeg file-import queue.
- SQLite history and migrations.
- Modes, prompts, dictionary, cloud providers, or AI enhancement.
- Production installer, updater, signing, or licensing UI.
- Real-time streaming transcription.
- AMD or Intel GPU support.
- Claiming CUDA support without a real NVIDIA validation environment.

## Proposed Architecture

```text
Application use case
        |
        v
Domain ASR port
        |
        v
NeMo-Speech.cpp sidecar adapter
        |
        v
127.0.0.1 loopback HTTP
        |
        v
Parakeet TDT V3 GGUF, CPU or CUDA backend
```

The application owns request lifecycle and user-facing state. The infrastructure adapter owns process startup, readiness, transport, model paths, checksums, backend arguments, and process-tree cleanup.

The preferred first transport is a long-lived loopback HTTP sidecar because it avoids a Python/C++ ABI binding, isolates native crashes, keeps the model loaded, and provides one boundary for macOS development and Windows runtime validation. Per-request CLI invocation may be retained as a diagnostic smoke tool but is not the production application path because it reloads the model for every request.

Native C ABI integration is deferred until HTTP overhead is measured and shown to be a real bottleneck.

## Layer Boundaries

### Domain

The domain defines:

- normalized audio value objects;
- `AsrRequest` and `TranscriptResult` models;
- word/segment timestamp values;
- runtime capability values;
- the ASR port;
- typed domain errors.

The domain must not import Qt, HTTP clients, subprocess APIs, Windows APIs, CUDA types, GGUF paths, or runtime-specific JSON schemas.

### Application

The application layer coordinates validation, request lifecycle, timeout, cancellation, bounded concurrency, runtime selection, and error mapping. It must depend on the ASR port rather than a concrete sidecar.

### Infrastructure

Infrastructure implements the fake runtime, sidecar supervisor, loopback transport, model/checksum verification, and later Windows adapters. Runtime metadata must be returned separately from transcript content.

## ASR Contract

The first contract should be equivalent to:

```python
class AsrRuntime(Protocol):
    def capabilities(self) -> AsrCapabilities: ...
    def health(self) -> RuntimeHealth: ...
    def transcribe(self, request: AsrRequest) -> TranscriptResult: ...
    def close(self) -> None: ...
```

The canonical audio input is:

- mono;
- 16 kHz;
- signed PCM S16LE;
- contiguous samples;
- non-empty;
- with deterministic duration metadata.

The runtime adapter may convert PCM16 to another representation internally, but that conversion must not leak into the domain contract.

The request may contain language, timestamp preference, request ID, deadline, and cancellation. It must not contain runtime URLs, executable paths, CUDA device IDs, GGUF paths, or subprocess arguments.

The result contains text, optional timestamps, duration, and optional detected language. Runtime version, model revision, backend, and hardware are diagnostics metadata, not part of the core transcript value.

Typed failures must distinguish invalid input, configuration, unavailable runtime, missing model, timeout, cancellation, protocol failure, and execution failure. An exception must never become a successful empty transcript.

## Runtime Lifecycle

1. Validate configuration.
2. Verify executable and model paths.
3. Verify the pinned model checksum.
4. Start one sidecar process bound to loopback only.
5. Wait for readiness with a deadline.
6. Serve requests through a bounded queue with initial concurrency of one.
7. Drain and stop during graceful shutdown.
8. Kill the process tree if shutdown exceeds its deadline.

Backend selection is explicit: `cpu` or `cuda:0`. Automatic fallback, if later desired, must be a visible policy and must emit a warning. The first cancellation policy is best-effort cancellation with client timeout, result discard, and supervisor cleanup. Hard cancellation is deferred.

## Fake Runtime

The fake runtime is required for macOS tests and must cover:

- deterministic success;
- invalid/empty input;
- unavailable runtime;
- missing model;
- timeout;
- cancellation;
- malformed result;
- backend unavailable;
- process crash simulation.

It must implement the same port as the real adapter and must not be a production fallback.

## Windows Validation

The mandatory PR pipeline should run on `windows-latest` with Python 3.12, 3.13, and 3.14 for specification, formatting, lint, tests, compilation, and fake-adapter contract tests.

The CPU runtime lane must use a pinned `NeMo-Speech.cpp` release/commit, a pinned Parakeet GGUF artifact, a licensed public fixture, and checksum verification. The CUDA lane requires a self-hosted NVIDIA runner or a real Windows machine and should initially run manually or nightly, not on every PR.

GitHub-hosted Windows runners must not be presented as NVIDIA validation.

## Benchmark Protocol

The benchmark must separate:

- cold process startup;
- model load;
- warm-up;
- warm inference;
- end-to-end application latency;
- IPC overhead.

Use one cold run, one warm-up, and at least ten measured warm runs for each backend. Report startup time, model load time, p50/p95/p99 latency, RTFx, peak RSS, CUDA VRAM, error rate, cancellation latency, and transcript accuracy where a reference transcript exists.

`RTFx` is defined as:

```text
audio_duration_seconds / wall_time_seconds
```

Every JSON result must include OS/build, CPU, RAM, GPU/VRAM/driver, Python, application commit, runtime commit, model filename/revision/quantization/checksum, backend, thread count, input manifest hash, mode, and timestamp. Raw audio and private transcripts must not be committed or logged.

## Risks and Mitigations

- Runtime API drift: pin runtime and add adapter contract tests.
- Model or quantization accuracy loss: compare F16/Q8 baselines with a fixed corpus.
- Windows DLL failures: native Windows smoke and packaging checks.
- CUDA/driver mismatch: explicit diagnostics and compatibility matrix.
- Missing hard cancellation: timeout, bounded queue, and process supervision.
- Model supply-chain risk: pinned revision and SHA-256 verification.
- Incorrect platform conclusions: label every benchmark with OS and backend.

## Exit Criteria

The milestone is complete when:

- the ASR port and normalized audio contract are documented and tested;
- fake runtime success and failure paths pass on macOS;
- the Windows CPU runtime smoke path passes;
- sidecar readiness, timeout, crash, and cleanup are tested;
- model/runtime checksums are verified;
- benchmark reports are reproducible and complete;
- Windows Python CI passes;
- CUDA is either validated on real NVIDIA hardware or explicitly marked blocked/deferred;
- no model weights, recordings, secrets, or generated binaries are in Git;
- the RFC and affected specs are updated with final decisions and evidence.

## Open Questions

- Which exact `NeMo-Speech.cpp` release or commit is pinned?
- Is the first model artifact Q8_0, F16, or both?
- What WER/CER and latency thresholds are acceptable?
- Is Windows 10 required, or is Windows 11 the first supported target?
- Is best-effort cancellation sufficient for the first application adapter?
- What maximum audio duration and request size must be supported?
- When will a streaming model be selected for live mode?
- How will the runtime and model be distributed in the Windows installer?
