# RFC: Offline Transcription of Imported Media

## Status

Proposed. This RFC depends on `rfcs/foundation-runtime-spike.md`. Implementation is blocked until the foundation RFC is approved, implemented, and its ASR/audio contracts are stable.

## Summary

Add offline transcription of imported audio and video files using the canonical audio contract and the Parakeet runtime adapter established by the foundation milestone. Product behavior is specified in `spec/features/002-imported-media-transcription.md`.

```text
Imported media
      |
      v
Bounded import queue
      |
      v
FFmpeg normalization
      |
      v
Canonical mono 16 kHz PCM
      |
      v
ASR port
      |
      v
Imported transcription result
```

This RFC describes an application workflow and infrastructure boundary. It does not define the desktop UI.

## Context

The foundation RFC defines the typed ASR port, canonical audio contract, fake runtime, Parakeet `NeMo-Speech.cpp` boundary, initial concurrency, cancellation semantics, and typed runtime failures.

The next product slice must accept local audio/video files, normalize them safely, submit them to the existing ASR port, and return a deterministic result. FFmpeg is the only boundary for containers, codecs, and media decoding. The original source file must never be modified.

Development continues on macOS, so application behavior must be testable with fake FFmpeg and fake ASR adapters. Real FFmpeg and Parakeet validation belongs to a separate Windows native smoke workflow.

## Goals

- Support imported local audio and video files.
- Normalize supported input to mono, 16 kHz, signed PCM S16LE.
- Implement a bounded FIFO queue with explicit capacity behavior.
- Support cancellation for queued, normalizing, and transcribing jobs.
- Retry only transient failures within a fixed attempt limit.
- Use the foundation ASR port without runtime-specific details in application code.
- Return typed success and failure results.
- Enforce source, decoded-duration, normalized-output, workspace, and stage-time limits.
- Remove temporary artifacts on every tested terminal path.
- Cover application behavior on macOS with fake infrastructure adapters.
- Add Windows CI contract checks and a separate native CPU smoke path.

## Non-Goals

- PySide6 UI, file picker, drag-and-drop, or progress widgets.
- WASAPI microphone recording.
- Global hotkeys, clipboard, or `SendInput`.
- Real-time or token-by-token streaming.
- AI enhancement, prompts, modes, dictionary, or cloud transcription.
- SQLite history.
- Persistent queue recovery across application restarts.
- CUDA-specific implementation changes.
- Installer or automatic distribution of FFmpeg, models, or runtime binaries.
- Partial transcript delivery.

## Dependencies

### Blocking dependency

`foundation-runtime-spike.md` must be approved and implemented with:

- a stable typed ASR port;
- a stable canonical audio contract;
- a deterministic fake runtime;
- a validated Windows CPU runtime path;
- documented error, timeout, and cancellation behavior.

### Future dependencies

- PySide6 presentation adapter;
- SQLite history;
- runtime/model installer;
- user-facing file selection;
- optional CUDA file-import benchmarks.

## Use Case

### Transcribe imported media

1. The caller submits a local source path and transcription options.
2. The application validates existence, regular-file status, path policy, and admission limits. Invalid input is a rejected request and has no job ID.
3. The application reserves queue capacity and creates a `job_id`.
4. The application creates an isolated temporary workspace after queue admission.
5. The job enters a bounded FIFO queue.
6. A worker copies an immutable source snapshot through a no-follow handle.
7. The worker runs FFmpeg normalization through a quota-enforcing pipe.
8. The normalized samples are validated.
9. The application calls the foundation ASR port.
10. The runtime result is mapped to a typed terminal result.
11. Temporary artifacts are removed.
12. The caller receives `succeeded`, `failed`, or `cancelled`.

### State machine

```text
accepted -> queued -> normalizing -> transcribing -> cleaning_up -> succeeded
    |         |           |              |                |
    v         v           v              v                v
  failed   cleaning_up cancelled      retry_waiting     failed
               |                         |
               v                         +----> queued
           cancelled
                                      ^
                                      |
                         DeadlineExceeded from every non-terminal state
```

For an admitted job, normalization, timeout, runtime, protocol, and deadline failures transition to `failed` after cleanup. A transient runtime failure may return from `transcribing` to `retry_waiting` and then `queued` for a new attempt. Cleanup runs before terminal publication, so a cleanup failure produces `failed` with `CleanupWarning` rather than changing a published success.

Only admitted jobs have terminal results. Invalid source, queue-full, and pre-admission source-size requests return `RejectedRequest`, a separate union of `InvalidSourceError`, `QueueFullError`, and `ResourceLimitExceededError`, without creating a job, reservation, or workspace. `TerminalResult` is only `Success | Failed | Cancelled` after admission commits; `admission` and `queue` are not terminal stages.

## Proposed Architecture

```text
Application
  ImportMediaUseCase
  ImportQueue
  RetryPolicy
  CancellationCoordinator
        |
        v
Domain ports and models
  ImportJob, NormalizedAudio, AsrRequest
  TranscriptResult, typed errors
        ^
        |
Infrastructure adapters
  FFmpegNormalizer, AsrRuntimeAdapter
  FFmpegProcessRunner, TemporaryWorkspace
```

This is a data-flow view. The dependency direction is `presentation -> application -> domain <- infrastructure`; a composition root wires concrete infrastructure adapters into application use cases.

### Boundary rules

- Domain must not import `subprocess`, HTTP clients, Qt, Windows APIs, FFmpeg types, or runtime JSON schemas.
- Application must not know executable paths or command-line arguments.
- FFmpeg is used only through a `MediaNormalizer` port.
- The ASR adapter receives canonical audio, never the original media file.
- Source files are read-only.
- Runtime diagnostics are separate from transcript text.
- Infrastructure depends on domain ports; domain never imports infrastructure implementations.

## Audio Normalization

The normalized artifact must contain exactly one audio stream with:

| Property | Required value |
|---|---|
| Channels | `1` |
| Sample rate | `16000 Hz` |
| Sample format | signed PCM `S16LE` |
| Container | RIFF/WAV |
| Samples | non-empty |
| Duration | derived from decoded sample count |

Duration is authoritative only when calculated from normalized samples:

```text
duration_seconds = sample_count / 16000
```

The proposed FFmpeg argument list is:

```text
ffmpeg
  -nostdin
  -hide_banner
  -loglevel error
  -xerror
  -i <source>
  -map 0:a:0
  -vn
  -map_metadata -1
  -ac 1
  -ar 16000
  -c:a pcm_s16le
  -f wav
  pipe:1
```

Arguments must be passed as an array without shell interpolation. Version 1 selects the first audio stream (`0:a:0`) for every supported media file. FFmpeg output goes to stdout and is consumed by a quota-enforcing sink; no unbounded normalized file is written. The exact FFmpeg build, supported codecs, and checksum policy are blocking decisions.

Permanent normalization failures include missing source, non-regular file, missing audio stream, unsupported/corrupt media, missing FFmpeg, invalid normalized WAV, empty audio, and admission-limit violations. These failures are not retried automatically.

The first implementation must enforce these proposed v1 limits: source snapshot `2 GiB`, derived workspace artifacts `768 MiB`, decoded duration `4 hours` (`230,400,000` samples), normalized PCM payload `512 MiB`, captured stderr `64 KiB`, queue capacity `8`, stage timeout `30 minutes`, cleanup timeout `5 minutes`, and processing deadline `45 minutes`. Source snapshot and derived-artifact quotas are disjoint; the maximum per-job disk budget is `2.75 GiB`. Derived quota accounts for every workspace byte except the source snapshot; PCM quota accounts only for the WAV `data` payload and sample count. A quota-enforcing sink rejects a chunk that would cross either PCM bytes or sample-count limit, terminates FFmpeg, accepts zero bytes beyond the limit, and returns `ResourceLimitExceeded`; before admission the same code is returned as `ResourceLimitExceededError` inside `RejectedRequest`. The supported container/codec matrix and final limit approval remain blocking decisions.

## FFmpeg Port

The infrastructure boundary should be equivalent to:

```python
class MediaNormalizer(Protocol):
    def normalize(
        self,
        source: SourceMedia,
        workspace: JobWorkspace,
        cancellation: CancellationToken,
        deadline: Deadline,
    ) -> NormalizedAudio: ...
```

The adapter owns process startup, safe arguments, timeout, process-tree termination, exit-code handling, WAV validation, error classification, and safe diagnostics. Application code must not parse FFmpeg stderr or construct commands. The adapter must read an immutable input snapshot in the private workspace, reject UNC paths/reparse points and network protocols, enforce the stdout quota sink, bound captured stderr, and use a Windows Job Object with kill-on-close and resource limits. FFmpeg must be pinned and checksum-verified before execution. The artifact manifest must record version, provenance URL, SHA-256, license, and allowed executable path.

## ASR Integration

The file workflow must use the foundation ASR port and must not introduce a second contract.

- ASR receives only canonical audio.
- Source path, FFmpeg path, model path, and CUDA device do not enter `AsrRequest`.
- FFmpeg details are invisible to the runtime adapter.
- Runtime diagnostics are separate from transcript text.
- Runtime exceptions cannot become successful empty transcripts.
- A result completed after cancellation is discarded and never published.

The foundation contract requires contiguous samples. The application reads the bounded normalized WAV stream from the quota sink into one immutable `CanonicalAudio` value before calling ASR. `CanonicalAudio` owns exactly one bounded contiguous PCM16 allocation and exposes `sample_count`, `byte_length`, and `duration`; the ASR request borrows it for the duration of the call and never contains a filesystem path. This is batch processing, not streaming transcription.

## Queue, Cancellation, and Retry

### Queue

- FIFO ordering.
- Bounded capacity.
- Initial worker concurrency of one.
- In-memory queue only.
- `QueueFullError` when admission capacity is exhausted.
- Queue capacity is reserved before a workspace is created.
- Capacity counts every admitted non-terminal job: queued, running, and `retry_waiting`. The reservation is released exactly once by the successful terminal CAS transition.
- No silent loss during shutdown; pending/running jobs receive terminal cancellation state.

Enqueue and dequeue are `O(1)` amortized. Queued cancellation uses a job index plus tombstones and lazy compaction, so it does not require linear removal from the FIFO. Queue metadata is `O(q)` for `q` pending jobs. Normalization and transcription are `O(n)` for `n` decoded samples. Disk usage is bounded by the configured workspace quota; ASR memory is bounded by the configured normalized-size limit.

Queue reservation is represented by a per-job `ReservationToken` with atomic states `held -> committed -> released`. Before admission commit, workspace/enqueue failure may transition `held -> released` idempotently and removes any partial workspace. A successful enqueue transitions `held -> committed`. After commit, only the job-level terminal CAS or supervisor recovery may transition `committed -> released`, and every release checks the owning job ID. Capacity counts every admitted non-terminal job, including queued, running, and `retry_waiting`; retry jobs re-enter the FIFO tail after `retry_waiting`; backoff never occupies the worker. At most one attempt workspace is live for a job: the previous attempt must finish cleanup and release its derived quota before a retry workspace is created. A worker crash is recovered by the supervisor, which completes cleanup and performs the same terminal release; startup recovery uses the workspace manifest for jobs with no live supervisor.

### Cancellation

- Queued cancellation marks the job cancelled and transitions it to `cleaning_up` before execution; cleanup then publishes `Cancelled` and releases the committed reservation.
- Normalization cancellation signals the token and terminates the FFmpeg process tree.
- Transcription cancellation follows the foundation best-effort policy: request cancellation, discard late output, clean up, and publish `cancelled` without partial text.
- If cancellation is requested during `cleaning_up`, the cleanup still runs. If cleanup succeeds, cancellation wins over success; if cleanup fails, `Failed(code=CleanupWarning)` wins over cancellation.
- Hard native-runtime cancellation is not promised by this RFC.

Cancellation intent and deadline expiry are linearized by the same job lock/transaction as terminal CAS. The first successful transition out of the current state wins: cancellation wins over completion only when its transition is linearized first; an already-running completion may win if it commits first. Cancellation in `retry_waiting` cancels the timer and transitions directly to `cleaning_up`; cancellation during cleanup uses the precedence above. Deadline expiry wins over a retry that has not committed its next attempt, while an already-committed cancellation wins over a later deadline callback.

### Retry

Retries are allowed only for transient runtime unavailability, startup/readiness failure, transport timeout, or runtime crash without invalid-input evidence.

Retries are forbidden for invalid media, missing audio, malformed output, cancellation, missing model/configuration, and protocol/schema failures.

Default: two total attempts with capped exponential backoff and a new temporary workspace per attempt. The application owns retry classification and uses an injectable monotonic clock, an absolute processing deadline of 45 minutes, a per-stage timeout of 30 minutes, and a cleanup deadline equal to `cleanup_entered_at + 5 minutes`. The processing deadline applies only before entry into `cleaning_up`; once in `cleaning_up`, only `cleanup_deadline` applies. For every processing operation, `effective_timeout = min(stage_timeout, processing_deadline - monotonic_now)`; cleanup uses `cleanup_deadline - monotonic_now`. A scheduler CAS-transitions every queued, running, or `retry_waiting` job whose processing deadline expires to `cleaning_up` with `DeadlineExceeded`; cleanup-deadline expiry produces `Failed(CleanupWarning)` and releases the committed reservation. No retry starts after the processing deadline. A retry is fenced by `(job_id, attempt)` so a late result from an older attempt cannot publish over a newer attempt.

## Result Contract

The workflow returns a typed terminal union that wraps the foundation `TranscriptResult` on success:

```python
@dataclass(frozen=True)
class ImportedTranscriptionResult:
    job_id: JobId
    source_name: str
    transcription: TranscriptResult
    processing: ProcessingMetadata
    diagnostics: RuntimeDiagnostics
```

Processing metadata includes normalized duration, attempt count, stage timings, total duration, and terminal status. Diagnostics may include runtime version, model revision, backend, hardware, request ID, and safe failure code.

The terminal result is:

```python
TerminalResult = Success | Failed | Cancelled

class Success:
    status: Literal["succeeded"]
    job_id: JobId
    attempt: Attempt
    transcription: ImportedTranscriptionResult
    warnings: tuple[WarningCode, ...]

class Failed:
    status: Literal["failed"]
    job_id: JobId
    attempt: Attempt
    stage: Literal["normalization", "transcription", "cleanup"]
    code: ErrorCode
    safe_message: str
    retryable: bool

class Cancelled:
    status: Literal["cancelled"]
    job_id: JobId
    attempt: Attempt
    stage: Stage
    reason: CancellationReason
```

`ProcessingMetadata`, `RuntimeDiagnostics`, `JobId`, `Attempt`, `Stage`, `ErrorCode`, `WarningCode`, and `CancellationReason` are typed domain values, not unvalidated strings. Terminal publication uses `transition(job_id, expected_attempt, expected_state, new_state) -> bool` under one lock/transaction. Only a successful compare-and-swap may emit the terminal result. Cleanup runs before publication; cleanup failure produces `Failed(code=CleanupWarning)` rather than publishing success with leftover sensitive data.

Failure values must expose:

```text
stage: normalization | transcription | cleanup
code: stable machine-readable code
message: safe user-facing message
retryable: boolean
```

Minimum terminal failure codes include `SourceChanged`, `ResourceLimitExceeded`, `NormalizationFailed`, `NoAudioStream`, `UnsupportedMedia`, `RuntimeUnavailable`, `RuntimeTimeout`, `DeadlineExceeded`, `RuntimeProtocolFailure`, `TranscriptionFailed`, `Cancelled`, and `CleanupWarning`. Rejected request codes are `InvalidSource`, `QueueFull`, and pre-admission `ResourceLimitExceeded`.

## Privacy and Temporary Files

- Processing is local.
- Source media is never modified.
- Each admitted job has a unique temporary workspace.
- Normalized audio is removed after success, failure, or cancellation.
- Pre-admission `RejectedRequest` values do not enter `cleaning_up`; only idempotent rollback of a held reservation is allowed, and no workspace or terminal result exists.
- Cleanup failure produces a diagnostic warning without deleting the source.
- Temporary paths, raw audio, and transcripts are excluded from ordinary logs and telemetry.
- Paths are canonicalized and validated before process execution. Symlinks, reparse points, UNC paths, and network-backed inputs are rejected by default. The source is opened through a no-follow, read-only handle; its `snapshot_identity` is fixed as Windows volume serial plus file ID; and the bytes read through that handle are copied into the private workspace snapshot. The copied bytes are hashed and size-checked, the manifest stores `snapshot_identity`, `snapshot_size`, and `snapshot_sha256`, the snapshot is write-protected, and it is reopened no-follow with identity, size, and SHA-256 verification immediately before FFmpeg starts. Any mismatch returns `SourceChanged` without retry.
- FFmpeg and the ASR runtime are launched without a shell.
- Output collisions and accidental source overwrite are prevented.

The workspace has a manifest and recursive cleanup policy for partial output, stderr, and marker files. Cleanup and the startup orphan sweep use no-follow handle-based traversal, delete reparse points as links, verify identity and containment under the private root at every node, and fail closed when containment cannot be proven. The startup sweep runs before accepting jobs, removes workspaces older than 24 hours without trusting child mtimes, and records only a count of removed workspaces. This retention policy also applies after a simulated process crash.

## Testing Strategy

### macOS tests

Application tests must run without Windows APIs, PySide6, CUDA, model weights, or a real Parakeet runtime.

Unit and workflow coverage must include admission validation, FFmpeg argument construction, strict RIFF/WAV validation (chunk bounds, PCM tag, block alignment, and truncation), duration calculation, retry classification, queue capacity, state transitions, cancellation, result mapping, diagnostics redaction, and cleanup.

Fake FFmpeg must simulate valid output, missing audio, unsupported media, malformed output, timeout, cancellation, non-zero exit, and cleanup failure. The foundation fake ASR must simulate deterministic success, invalid input, timeout, cancellation, unavailable runtime, malformed result, crash, and unavailable backend.

Required workflow tests include successful audio/video processing, normalization failure without ASR invocation, resource-limit rejection, queued cancellation through cleanup without FFmpeg invocation, running cancellation without published partial output, cancel-vs-complete/deadline/retry fencing, cancellation in `retry_waiting`, bounded retries, no retry for permanent failures, queue rejection before workspace creation, reservation rollback/recovery, at-most-one-live-attempt quota enforcement, path replacement, reparse replacement, same-size mutation, immutable snapshot identity/hash verification, no-follow junction/symlink cleanup, and cleanup on every terminal path.

Fixtures must be generated in temporary directories. Binary media and model weights are not committed.

### Windows validation

The mandatory PR matrix continues to run specification, formatting, lint, fake adapter tests, and compilation on Python 3.12-3.14. The implementation must add a separate manual/nightly native smoke job or script with pinned FFmpeg/runtime/model checksums, a licensed public fixture, explicit setup, and a report artifact. It must validate normalization, non-empty transcription, metadata, process-tree cancellation, and workspace cleanup.

CUDA validation remains owned by the foundation RFC.

## Acceptance Criteria

This RFC is architectural and non-normative for product behavior. The normative Given/When/Then acceptance criteria live in `spec/features/002-imported-media-transcription.md`. The implementation must additionally preserve the dependency direction, immutable input snapshot, bounded resource policy, contiguous `CanonicalAudio` contract, atomic terminal transition, and reproducible Windows smoke workflow defined here.

## Exit Criteria

This RFC is complete when the foundation RFC is stable, the imported-media use case and bounded queue are implemented, FFmpeg is isolated behind a port, cancellation/retry behavior is tested, macOS fake-adapter tests pass, Windows PR CI passes, Windows native CPU smoke succeeds, temporary workspace cleanup is verified, and no raw audio, private transcripts, model weights, or generated binaries are committed.

Supported media limits and FFmpeg distribution policy must be documented before this RFC changes to `Complete`.

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Foundation ASR contract changes | Block implementation until foundation is approved |
| Codec/container variability | Pin FFmpeg, publish a support matrix, and run native smoke |
| Long files exhaust memory | Use disk-backed normalized artifacts and a bounded contiguous-sample buffer |
| FFmpeg hangs | Enforce deadlines and process-tree termination |
| Runtime cancellation is delayed | Discard late results and supervise cleanup |
| Retry duplicates work | Use a new workspace per attempt, attempt fencing, and publish once |
| Temporary files survive crashes | Use recursive workspace cleanup, cleanup diagnostics, and a startup orphan sweep |
| Sensitive data leaks in logs | Redact paths and never log raw content |
| Malicious media exhausts resources | Enforce byte/duration/workspace/time quotas and bound stderr |
| Native parser or process escape | Pin/checksum FFmpeg, reject network inputs, use a private workspace, and use a Windows Job Object |
| Ambiguous audio tracks | Make first-track behavior explicit or decide track selection first |
| FFmpeg licensing ambiguity | Document the exact build before distribution |

## Open Questions

### Blocking

1. Are the proposed v1 limits approved: source `2 GiB`, decoded duration `4 hours`, normalized WAV `512 MiB`, workspace `768 MiB`, stderr `64 KiB`, queue capacity `8`, stage timeout `30 minutes`, and total deadline `45 minutes`?
2. Which FFmpeg build, version, checksum policy, and distribution/license policy are used?
3. Which containers/codecs are supported in the first release?
4. Version 1 selects the first audio stream (`0:a:0`); is explicit track selection required instead?
5. What cancellation guarantee does the runtime supervisor provide for the contiguous-sample request?

### Deferred

- Persistent queue recovery.
- SQLite history.
- UI progress and file picker.
- Partial transcript delivery.
- Streaming transcription.
- Automatic model/runtime downloads.
- CUDA-specific file-import benchmarks.
- AI enhancement and post-processing.
- Installer packaging of FFmpeg and runtime.
