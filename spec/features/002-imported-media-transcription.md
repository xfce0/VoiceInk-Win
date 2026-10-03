# Feature: Imported Media Transcription

## Status and Scope

Status: draft.

This feature accepts local audio/video files, normalizes them to the canonical audio contract, transcribes them through the foundation ASR port, and returns typed results. It excludes the PySide6 UI, microphone recording, streaming, history, and AI enhancement. Implementation is blocked by `rfcs/foundation-runtime-spike.md` and `rfcs/imported-media-transcription.md`.

## User Scenarios

- A caller submits a supported local audio file and receives one completed transcript result.
- A caller submits a supported local video file and receives a transcript from its first audio stream (`0:a:0`) in v1.
- A caller submits invalid or audio-less media and receives a safe typed failure without an ASR call.
- A caller cancels a queued or running job and receives no partial transcript.
- A caller submits work while the bounded queue is full and receives `QueueFull` without unbounded resource growth.

## Functional Requirements

1. The application must accept only canonicalized local regular files inside configured import roots; symlinks, Windows reparse points, UNC paths, and network protocols must be rejected unless an explicit trusted-input policy is approved. Invalid input is a rejected request, not a terminal job result.
2. The application must reserve queue capacity before creating a temporary workspace using a per-job `ReservationToken` with atomic states `held -> committed -> released`; workspace/enqueue failure may release `held`, while only the job-level terminal CAS or supervisor recovery may release `committed`. All releases are idempotent and owner-checked. Capacity counts all admitted non-terminal jobs, including queued, running, and `retry_waiting` jobs.
3. The queue must be FIFO, bounded, and initially limited to one active worker.
4. FFmpeg must produce one non-empty RIFF/WAV artifact with mono, 16 kHz, signed PCM S16LE samples.
5. The normalizer must enforce these v1 limits: source snapshot `2 GiB`, derived workspace artifacts `768 MiB`, decoded duration `4 hours` (`230,400,000` samples), normalized PCM payload `512 MiB`, stderr `64 KiB`, queue capacity `8`, stage timeout `30 minutes`, cleanup timeout `5 minutes`, and processing deadline `45 minutes`. A quota-enforcing pipe must reject the first chunk that crosses the normalized PCM byte or sample-count quota; derived quota accounts for every workspace byte except the source snapshot.
6. The application must copy the source through a no-follow read-only handle into an immutable workspace snapshot. The manifest must store `snapshot_identity` (Windows volume serial plus file ID), `snapshot_size`, and `snapshot_sha256` for the snapshot itself; the snapshot must be write-protected and reopened no-follow with all three fields verified before FFmpeg. Any mismatch returns `SourceChanged`.
7. `CanonicalAudio` must own one bounded PCM16 allocation with `sample_count`, `byte_length`, and `duration`; the ASR request must not contain paths or runtime-specific types.
8. A job must have one terminal outcome: succeeded, failed, or cancelled. Publication must use `transition(job_id, expected_attempt, expected_state, new_state) -> bool` under a lock/transaction so late results cannot overwrite a newer attempt.
9. Only classified transient runtime failures may retry, with a default maximum of two total attempts and capped backoff. The retry returns to the FIFO tail only after the previous attempt workspace is cleaned; at most one attempt workspace is live, and retry cannot begin after the absolute deadline. Queued and waiting jobs expire as `DeadlineExceeded`.
10. Temporary workspace cleanup must run for success, failure, cancellation, process timeout, and process crash paths. Pre-admission rejection performs only idempotent rollback of a held reservation and has no workspace or cleanup state; a startup no-follow sweep removes private workspaces older than 24 hours and fails closed on containment uncertainty.
11. The terminal result must be the typed union `Success | Failed | Cancelled`, discriminated by `status` and sharing typed `job_id` and `attempt` values.
12. `Success` must include the transcript, normalized duration, attempt count, stage timings, safe diagnostics, and warnings; `Failed` must include terminal stages `normalization`, `transcription`, or `cleanup`, stable code values, safe message, and retryable flag; `Cancelled` must include stage and reason. Pre-admission errors use the separate `RejectedRequest` union with `InvalidSourceError`, `QueueFullError`, or `ResourceLimitExceededError`; they do not create a job or reservation.

## Non-Functional Requirements

- Processing must remain local and must not log raw audio, transcript text, full paths, or complete FFmpeg stderr.
- FFmpeg must be pinned, checksum-verified, launched without a shell, and restricted to local file input and a private workspace.
- On Windows, the FFmpeg process must run in a Job Object with kill-on-close and resource limits.
- FFmpeg stderr must be captured with a bounded limit and redacted before diagnostics are retained.
- The canonical source display name must be a normalized basename with a length limit and no control characters.
- Queue operations must be `O(1)` amortized; cancellation may use tombstones and lazy compaction rather than linear removal.
- Normalization/transcription work is `O(n)` in decoded samples, with peak memory bounded by configured limits.
- Native Windows smoke tests must use a manifest containing pinned version, provenance URL, SHA-256, license, allowed executable path, and a licensed fixture without committing binaries or model weights.

## Error and Cancellation Behavior

The state machine is:

```text
accepted -> queued -> normalizing -> transcribing -> cleaning_up -> succeeded
    |         |           |              |                |
    v         v           v              v                v
  failed   cleaning_up cancelled      retry_waiting     failed
               |                          |
               v                          +----> queued
           cancelled
```

For admitted jobs, normalization, timeout, runtime, protocol, and deadline failures transition to `failed` after cleanup. A transient runtime failure may return from `transcribing` to `retry_waiting` and then the FIFO tail. The processing deadline applies only before `cleaning_up`; on entry, `cleanup_deadline = cleanup_entered_at + 5 minutes`, after which the result is `Failed(code=CleanupWarning)`. Cleanup occurs before publication and never changes a published result. Shutdown cancels queued jobs and requests cancellation for active jobs. Pre-admission invalid input, queue full, and source-size rejection are `RejectedRequest` values and create no job, reservation, workspace, or cleanup state.

Queued cancellation marks the job cancelled, runs cleanup, publishes `Cancelled`, and releases the committed reservation before any worker execution. Running cancellation sets the token, terminates the FFmpeg process tree when applicable, asks the ASR runtime to cancel according to the foundation best-effort policy, and discards late output. A cancelled job never publishes partial text.

Permanent failures are not retried. Retry uses a new attempt workspace, an injectable monotonic clock for backoff, an absolute processing deadline, a separate cleanup deadline five minutes later, and an atomic terminal transition. Every processing operation uses `min(stage_timeout, processing_deadline - monotonic_now)`; cleanup uses the cleanup deadline. No retry starts when processing time is exhausted. Cancellation and deadline expiry share the job lock/transaction: the first successful transition wins, cancellation in `retry_waiting` cancels its timer, and cleanup failure overrides cancellation.

Minimum terminal failure codes are `SourceChanged`, `ResourceLimitExceeded`, `NoAudioStream`, `UnsupportedMedia`, `NormalizationFailed`, `RuntimeUnavailable`, `RuntimeTimeout`, `DeadlineExceeded`, `RuntimeProtocolFailure`, `TranscriptionFailed`, `Cancelled`, and `CleanupWarning`. Rejected request codes are `InvalidSource`, `QueueFull`, and pre-admission `ResourceLimitExceeded`.

## Acceptance Criteria

- Given a supported fixture within all configured limits, when processing completes, then the normalizer reports mono/16 kHz/S16LE/non-empty audio and exactly one success result is published.
- Given a missing, unsupported, corrupt, or audio-less fixture, when processing starts, then the result has the expected permanent error code and the fake ASR invocation count is zero.
- Given a source snapshot or normalized stream at `limit-1`, `limit`, and `limit+1`, when processing starts, then only `limit+1` returns `ResourceLimitExceeded` or a pre-admission `RejectedRequest` as appropriate, no retry occurs, and no accepted output exceeds its quota; duration boundaries are tested at the equivalent sample counts.
- Given a full queue, when another job is submitted, then the API raises `QueueFullError` with stable code `QueueFull`, releases no-longer-needed reservation state, and creates no workspace or terminal job result.
- Given an admitted queued job, when it is cancelled before worker execution, then FFmpeg and ASR invocation counts remain zero, cleanup runs, the result is `Cancelled`, and reservation release count is exactly one.
- Given a running, queued, or `retry_waiting` job, when cancellation races with completion, deadline, or retry, then the first fenced transition under the job transaction publishes and no later attempt can overwrite it.
- Given a transient runtime error, when the attempt limit is not exhausted, then exactly one retry occurs using a new workspace and capped backoff only after the prior attempt workspace is fully cleaned; at most one attempt workspace is live.
- Given a permanent input error, when processing ends, then the job is not retried.
- Given any terminal state or a simulated process crash, when cleanup/sweep completes, then no-follow cleanup removes the workspace and partial artifacts within the 24-hour retention policy, fails closed on an external junction/symlink, and leaves the source file hash unchanged.
- Given macOS without Windows APIs, CUDA, model weights, or a real runtime, when the fake-adapter suite runs, then all application tests pass.
- Given a Windows runner with pinned FFmpeg and Parakeet CPU artifacts, when the native smoke command runs, then normalization, non-empty transcription, diagnostics, immutable snapshot checks, and cleanup all pass.

## Test Plan

- Unit tests for admission, path policy, display-name normalization, resource limits, queue capacity, retry classification, state transitions, fencing, error mapping, WAV validation, and diagnostics redaction.
- Integration tests with fake FFmpeg and fake ASR for success, invalid input, timeout, cancellation, deadline, retry, late result, process crash, queue rejection, reservation rollback/recovery, and cleanup failure.
- Property-based or table-driven malformed RIFF/WAV tests covering truncated chunks, invalid PCM tags, block alignment, overflow, and inconsistent data sizes.
- Windows native smoke tests with pinned FFmpeg/runtime checksums, supported container/codec fixtures, process-tree termination, immutable snapshot mutation detection, and cleanup verification.
- Security tests for no-follow junction/symlink cleanup, root-containment failure, permission races, path replacement, reparse replacement, and same-size source mutation.
- Benchmark matrix for short/long files and supported codecs recording p50/p95 latency, real-time factor, peak RSS, peak workspace bytes, cancellation latency, and queue wait time.

## Open Questions and Deferred Work

- Approve exact source byte, duration, output byte, workspace, and stage-time limits.
- Approve the supported container/codec matrix and confirm the v1 first-audio-stream policy (`0:a:0`).
- Approve the pinned FFmpeg build, checksum storage, and distribution/license policy.
- Approve exact foundation contiguous-sample request limits and lifetime ownership.
- Keep the 24-hour startup orphan cleanup policy aligned with the future operations specification.
- Defer persistent queue recovery, SQLite history, UI progress, partial results, streaming, automatic downloads, CUDA benchmarks, and installer packaging.
