# RFC: Windows Microphone Recording

## Status

Status: Draft — **implementation blocked**.

This RFC is a design and evidence plan only. No production microphone code,
WASAPI binding, native helper, IPC transport, packaging change, or ASR service
change may be implemented until D1–D19 and the pre-implementation portions of
G1, G2, G3a-policy, and G4a with outcome `GO` are approved and the decision
register is closed.
G3b native evidence and G4b validation are required before enablement.

## Summary

VoiceInk-Win will eventually support one explicit local microphone recording
per session, followed by batch transcription through the existing shared ASR
application service. Capture is not streaming ASR. Device-native frames remain
inside Windows infrastructure; the application and domain receive only one
bounded immutable `CanonicalAudio` value:

```text
mono, 16,000 Hz, signed PCM16, little-endian, contiguous, non-empty
```

The design is intentionally split into four independent review slices:

1. **Recording contract and state machine** — the normative user-visible and
   application lifecycle contract.
2. **Shared ASR scheduler and quiescence** — admission, fairness, cancellation,
   ownership, and the request-specific cleanup fence shared by microphone and
   imported media.
3. **WASAPI policy and format conversion** — endpoint policy, WASAPI mode,
   limits, conversion invariants, and golden-vector evidence.
4. **Native helper, IPC feasibility, and packaging** — process isolation,
   protocol, authentication, crash recovery, distribution, and target-matrix
   evidence.

Each slice has explicit entry conditions, blocking decisions, deliverables,
and Given/When/Then acceptance criteria. A slice being described is not a
decision to implement it.

## Context and Existing Constraints

- The dependency direction is `Presentation -> Application -> Domain <-
  Infrastructure adapters`.
- `CanonicalAudio` owns contiguous mono 16 kHz signed PCM16 little-endian
  samples, rejects empty or odd-sized payloads, and has a 64 MiB payload limit.
- `AsrRequest` accepts canonical audio, request metadata, an absolute monotonic
  deadline, and optional cancellation. It contains no path, device handle,
  runtime URL, or native type.
- `AsrApplicationService` currently has a bounded worker pool, initially one
  active worker, but `transcribe()` is not a request-scoped quiescence fence:
  cancellation may return while the runtime worker still owns the request.
- Imported-media transcription already defines bounded admission, FIFO
  scheduling, cleanup, redaction, and retry principles. Microphone work must
  use that shared ASR path and must not create a second ASR pool.
- The current shell semantics are
  `idle -> recording -> processing -> transcript_ready | empty | error`.
  Completion is synchronous and demo-oriented; live capture must not block the
  Qt thread.
- The repository has no production microphone adapter, selected WASAPI
  technology, native microphone smoke lane, or microphone packaging policy.
  macOS tests cannot prove Windows audio behavior.
- The architecture overview currently says that FFmpeg is the only media
  conversion boundary. This RFC proposes the narrower distinction that
  imported containers/codecs use FFmpeg, while live device-native frames are
  converted inside the platform audio-input adapter. That amendment is a
  blocking decision, not an assumed fact.

## Goals

- Specify one platform-independent audio-input boundary and one session per
  recording.
- Preserve a single canonical audio contract and a single shared ASR path.
- Keep Qt, WASAPI, COM, native handles, callbacks, device identifiers, and
  native status codes out of Domain and Application business values.
- Make cancellation, deadlines, resource limits, device loss, permissions,
  cleanup, and late-result fencing observable and testable.
- Produce enough feasibility and native evidence to make helper and packaging
  decisions before code is written.
- Define cross-platform fake tests separately from a named Windows acceptance
  lane.

## Non-Goals

- Streaming or partial transcription, waveform persistence, or live
  visualisation driven by raw frames.
- Loopback/system audio, call recording, multi-device mixing, automatic device
  switching, or silent fallback to the OS default.
- Global hotkeys, history, text injection, cloud transcription, enhancement,
  automatic downloads, installer implementation, or release rollout.
- Writing recordings to disk or using disk spill as a memory workaround.
- Choosing a Python package, native language, IPC primitive, converter,
  endpoint role, WASAPI mode, or packaging technology without the evidence and
  approval required by the four gates.

## Proposed Architecture

### Architecture and ownership

```text
Presentation
    -> Recording application service
    -> AudioInputPort / Windows adapter
    -> CanonicalAudio
    -> shared AsrApplicationService scheduler
    -> existing AsrRuntime
    -> immutable terminal snapshot
```

**Domain** owns immutable device-selection values, limits, recording/session
identifiers, capture result categories, canonical audio, ASR values, and the
state transition rules. It must not import Qt, WASAPI, COM, `ctypes`,
`pywin32`, third-party audio types, native handles, or Windows error constants.

**Application** owns admission, one active recording, state transitions,
deadlines, generation fencing, cancellation orchestration, ASR admission, and
safe user-facing error mapping. It calls abstract ports only.

**Infrastructure** owns device enumeration, Windows privacy/access behavior,
WASAPI initialization, format negotiation, bounded buffers, conversion, native
resource cleanup, helper process management, IPC, and Windows error
classification.

**Presentation** owns commands, accessibility labels, rendering, and immutable
event/snapshot delivery. It never enumerates devices, reads samples, waits for
capture/ASR, calls WASAPI, or calls `AsrRuntime`.

### Gated work plan

| Gate | Slice | Must decide and prove | Exit artifact | Status |
|---|---|---|---|---|
| G1 | Recording contract and state machine | authoritative commands, states, terminal outcomes, ownership, limits, error hierarchy, and generation fence | approved contract section plus decision record and race matrix | BLOCKED |
| G2 | Shared ASR scheduler/quiescence | shared admission semantics, ownership states, fairness, request handle, recovery, cancellation, and quiescence fence | approved shared-service contract plus decision record and scheduler race matrix | BLOCKED |
| G3 | WASAPI policy/format conversion | G3a: default role, mode, exact input-format matrix, converter, limits; G3b: Windows evidence | approved G3a policy/decision record, then G3b native evidence | BLOCKED |
| G4 | Native helper/IPC/packaging | pre-implementation helper/IPC/package feasibility and decisions; later crash, security, and clean-machine evidence | approved feasibility record, protocol/package decision, then validation evidence | BLOCKED |

Gate dependencies are strict: **G1 precedes G2; G1 precedes G4a; G2 and G4a
precede G3a; G3a precedes G3b and G4b**. G3a is the pre-implementation policy
decision; G3b is post-implementation native validation. G4a is the
pre-implementation feasibility and target/package decision; G4b is
post-implementation helper/package validation. All pre-implementation
decisions D1–D19 must be approved before any production code. G3b/G4b
evidence is required before microphone enablement, not before code may be
written. No fake adapter, fake test implementation, helper prototype,
packaging change, or ASR service code is authorized by this RFC while it is
Draft; research results may be supplied as external prose/evidence and then
recorded in the decision register.

## Slice G1: Normative Recording Contract and State Machine

### Entry conditions

G1 review may start only with the existing `CanonicalAudio` and imported-media
contracts attached to the review. G1 cannot close while the 32-minute limit,
the cancellation representation, the error hierarchy, or the architecture
conversion wording remains unspecified.

### Audio-input port contract

The following is a conceptual contract, not an instruction to create these
exact Python classes:

```text
AudioInputPort.list_input_devices(deadline)
    -> tuple[InputDevice, ...] | CaptureError
AudioInputPort.open(selection, limits, deadline)
    -> CaptureSession | CaptureError

CaptureSession.start(deadline) -> None | CaptureError
CaptureSession.request_stop(deadline) -> CanonicalAudio | CaptureError
CaptureSession.cancel(deadline) -> CancelAcknowledgement | CaptureError
CaptureSession.close(deadline) -> None | CleanupError
```

The contract is normative:

1. `InputDevice` is immutable and contains only an opaque selection ID,
   display name, input/active/default flags, and approved safe capability
   metadata. Application code cannot parse or construct the ID.
2. A session is single-use, bound to one endpoint at `open()`, and cannot be
   retargeted or restarted after `start()`.
3. `request_stop()` stops accepting frames, drains only adapter-owned bounded
   data, validates continuity, converts to canonical audio, and returns one
   owned immutable value. It is not a polling read API.
 4. `cancel()` is idempotent signalling, never returns audio, and is not proof
   of cleanup. `close()` is idempotent. No terminal result is published until
   cleanup completes and the owned audio/native references are released. A
   typed recovery failure is non-terminal while `recovery_owned`; it becomes a
  terminal `Failed(CleanupWarning)` outcome, projected as shell `error`, only
  after approved recovery proves release.
5. Every operation receives an absolute monotonic deadline. No COM call,
   callback, device wait, or native cleanup may wait forever.
6. `cancel()` may race with `start()` or `request_stop()`. No other concurrent
   method call is allowed. A cancellation acknowledgement means only that the
   signal was accepted.
7. There is no disk recording, native frame crossing, path, handle, callback,
   or Windows error value in Domain/Application contracts.

### Canonical output and resource bounds

The successful result is exactly the existing canonical value: mono, 16 kHz,
S16LE, contiguous samples, non-empty, immutable application-owned bytes. A
valid all-zero buffer is silence; zero accepted frames are `EmptyCapture` and
must not invoke ASR. Discontinuity, overrun, malformed/non-finite samples, or
silent frame dropping is a typed failure, never padding or truncation.

The proposed 32-minute / 30,720,000-sample limit and 64 MiB canonical ceiling
are not approved defaults. G1 must approve the duration/sample relationship
and the complete byte budget. Until then, implementation must not choose a
different limit or rely on the byte ceiling alone. The final canonical buffer
is `O(n)` space for `n` samples; the bounded capture queue is `O(b)` for
configured capacity `b`; capture/conversion are `O(n)` excluding a fixed
converter constant.

### Authoritative application state machine

The application state and legal commands are:

The normative operation states are `idle`, `starting`, `recording`,
`finalizing`, `asr_queued`, `asr_running`, `cancelling`, and `cleanup`.
`transcript_ready`, `empty`, `error`, and `cancelled` are terminal operation
outcomes projected by the shell. A successful cancellation publishes the
`cancelled` outcome and then projects `idle`; `idle` is not a substitute for
the terminal cancellation record.

```text
idle --start--> starting --opened--> recording --stop--> finalizing
 |        |         |                   |                 |
 |        |         +--cancel----------> cancelling       +--cancel--> cancelling
 |        +--cancel-------------------> cancelling       +--QueueFull-> cleanup
 |        +--failure------------------> cleanup          +--failure--> cleanup
 |                                                        |
finalizing --admitted--> asr_queued --> asr_running       +--cleanup--> RejectedRequest(code=QueueFull)
    |                                  |       |
    +--cancel--> cancelling            |       +--result--> cleanup
                                       +--cancel--> cancelling
recording --device/overflow/deadline--> cleanup
asr_queued/asr_running --failure/deadline--> cleanup

starting/recording/finalizing/asr_queued/asr_running
    --cancellation or failure--> cancelling/cleanup
cleanup --success--> pending terminal outcome (transcript_ready/empty/error/cancelled)
cleanup --failure--> recovery_owned (non-terminal)
recovery_owned --all ownership release proven--> Failed(CleanupWarning) -> shell error
terminal outcome --reset--> idle
```

The transition table is normative:

| Current | Event/guard | Next | Resource owner and obligation |
|---|---|---|---|
| `idle` | `start` admitted | `starting` | orchestration owns session creation; no native call on GUI thread |
| `starting` | open/start succeeds | `recording` | capture session owns bounded native/queued frames |
| `starting` | cancel | `cancelling` | orchestration closes session; no ASR handle exists |
| `starting` | failure or deadline | `cleanup` | pending `Failed`; close session; no ASR handle exists |
| `recording` | `stop` | `finalizing` | capture session owns frames and conversion scratch |
| `recording` | `cancel` | `cancelling` | capture session owns/discards frames; ASR is not invoked |
| `recording` | device/overflow/deadline failure | `cleanup` | pending `Failed`; capture session is closed; ASR is not invoked |
| `finalizing` | canonical value admitted | `asr_queued` | orchestration transfers the only canonical reference at the admission linearization point |
| `finalizing` | `QueueFullError` before ASR admission | `cleanup` | pending internal `StageAdmissionFull(QueueFullError)`; cleanup releases canonical value exactly once and only then maps/publicizes `RejectedRequest(code=QueueFull)`; no ASR handle |
| `finalizing` | cancel/failure/limit | `cancelling` or `cleanup` | orchestration owns and discards canonical value; no ASR handle on this path |
| `asr_queued` | worker begins | `asr_running` | ASR handle/service owns canonical value |
| `asr_queued` | cancellation, deadline, or scheduler failure | `cancelling` or `cleanup` | pending `Cancelled`/`Failed`; handle is fenced and then released |
| `asr_queued`/`asr_running` | cancel | `cancelling` | ASR handle owns canonical value until quiescence or recovery ownership |
| `asr_running` | result | `cleanup` | pending transcript/empty/error; ASR handle reaches quiescence before terminal publication |
| `asr_running` | runtime/timeout/deadline failure | `cleanup` | pending `Failed`; ASR handle reaches quiescence before publication |
| `cancelling` | capture/ASR fences complete | `cleanup` | orchestration owns cleanup; no result may be published yet |
| `cleanup` | cleanup succeeds | pending terminal outcome | all references/resources released exactly once before publication; QueueFull remains a pre-ASR-admission rejection of the recording operation |
| `cleanup` | cleanup deadline expires | `recovery_owned` (non-terminal) | no publication, audio release, or new generation; named supervisor must prove release or complete process-level recovery |
| `recovery_owned` | approved recovery proves all audio/ASR/helper/native ownership is released | `Failed(CleanupWarning)` -> shell `error` | exactly one terminal recovery outcome is published after `resource_release_proven`; no pending outcome is published afterward |

Only the orchestration owner may commit the terminal CAS and publish the
outcome. D2 chooses the shell projection representation, but this operation
state machine and the `cancelled` terminal outcome are fixed. If approval
rejects the proposed representation, an RFC/spec amendment is required before
implementation.

Rules:

- `start` is admitted only from `idle`; a second start is rejected and is not
  queued. No native `open()` or `start()` runs on the GUI command path.
- `stop` changes the visible state to the approved processing presentation
  before `finalizing` and ASR. `cancel` from `starting` or `recording`
  discards capture and never invokes ASR.
- `cancel` from `finalizing` fences capture without an ASR handle; cancellation
  from `asr_queued` or `asr_running` fences the specific ASR request. All paths
  discard late output and publish only after cleanup.
- One orchestration owner performs the generation compare-and-swap and emits
  at most one terminal result. Old generations cannot overwrite reset,
  shutdown, cancellation, or a newer recording.
- `cancelling` is distinct from `processing`; the shell may project it either
  as a state or an operation status only after D2 approval. Successful
  cancellation has no transcript and no partial audio. Cleanup failure is
  visible and blocks a new microphone generation until the approved recovery
  action completes.

### G1 acceptance criteria

- **G1-AC-001** — Given an admitted explicit device, when start and stop both
  complete before their deadlines, then exactly one canonical immutable value
  is produced, the GUI thread never blocks, and the value is submitted only
  through the shared ASR service.
- **G1-AC-002** — Given a missing selected device or denied permission, when
  start is attempted, then the session publishes the stable typed error, does
  not fall back, invokes ASR zero times, and leaves no active session.
- **G1-AC-003** — Given valid silence, when capture stops, then the canonical
  value remains valid; given zero accepted frames, then `EmptyCapture` is
  published and ASR is not invoked.
- **G1-AC-004** — Given cancellation races with start, stop, finalization, or
  ASR completion, when the race is exercised with barriers/events, then one
  generation publishes exactly one fenced outcome and no partial transcript.
- **G1-AC-005** — Given a cleanup timeout, when the cleanup deadline expires,
  then non-terminal `RuntimeRecoveryPending` is visible, no terminal outcome,
  audio release, or new microphone generation is permitted, and
  `Failed(CleanupWarning)` is published only after `resource_release_proven`;
  the process never claims native closure before that proof.

## Slice G2: Shared ASR Scheduler and Quiescence Contract

### Entry conditions

G2 is blocked until the existing imported-media scheduler owner agrees that
microphone admission is a source of the same queue, capacity reservation, and
terminal cleanup accounting. A microphone-specific worker pool or global idle
fence is not an acceptable substitute.

### Normative shared-service contract

The shared service must expose the following semantics (the concrete Future,
callback, signal, lock, or actor implementation is a later decision):

```text
AsrApplicationService.try_admit(request)
    -> AsrRequestHandle | QueueFullError

AsrRequestHandle.cancel(deadline) -> None | AsrError
AsrRequestHandle.await_result(deadline)
    -> TranscriptResult | AsrError
AsrRequestHandle.await_quiescence(deadline)
    -> None | RuntimeRecoveryPending
```

- FIFO fairness is shared by imported-media and microphone requests. Capacity
  counts pending, active, and reserved requests; neither source has hidden
  priority.
- Admission has one linearization point and these ownership states:
  `reserved -> queued -> active -> quiescent -> released`, with
  `active -> recovery_owned` when the bounded quiescence deadline expires.
  Before the linearization point the orchestration owner owns the canonical
  value. On successful `reserved -> queued`, the request handle/service takes
  the only canonical reference; on `QueueFull` or rollback, ownership remains
  with the caller, which releases it exactly once.
- `QueueFull` is a distinct pre-ASR-admission `RejectedRequest`, not an
  admitted ASR terminal outcome. The already-admitted recording operation
  publishes this one stage-rejection after cleanup. It performs no implicit
  retry and retains no canonical value after the caller's release.
- The scheduler returns the existing typed `QueueFullError`; orchestration maps
  that error to `RejectedRequest(code=QueueFull)` only after capture cleanup.
- A handle identifies exactly one admitted request and owns at most one
  canonical payload. It may be cancelled idempotently. The handle's state,
  reservation, worker ownership, and reference release are changed under one
  scheduler transaction/linearization protocol; a failed enqueue rolls back
  the reservation before returning to the caller.
- Quiescence means that the identified request is absent from the pending
  queue and active-worker set, no runtime or transport operation can consume
  or publish its result, and the shared service retains no reference to its
  canonical audio.
- `await_quiescence()` is request-scoped. Global `interrupt_active()` or an
  unscoped `wait_idle()` cannot satisfy it when an unrelated imported-media
  request is active.
- `recovery_owned` means a named supervisor owns the handle and canonical
  reference after the fence deadline; it is not quiescence and it is not
  permission to release audio. Until `resource_release_proven` confirms that
  the request is `quiescent -> released` and all audio/ASR/helper/native
  ownership is released, no new microphone generation is admitted and no
  terminal result is published. `RuntimeRecoveryPending` is a visible
  non-terminal recovery status, not a successful cancellation. Process-level
  recovery or restart is only a method to establish `resource_release_proven`.
- The recording service may release its last audio reference, publish
  cancellation, or accept a new microphone generation only after the
  identified request reaches `quiescent -> released`; `recovery_owned` can
  satisfy none of those conditions.
- A blocked runtime remains governed by the existing best-effort interruption
  semantics. The new fence adds ownership/recovery observability; it does not
  promise unsafe hard interruption of native inference.

### Required race and ownership matrix

G2 approval must include tests for each pair below. The winning terminal CAS,
not callback arrival order, defines the result:

| Race | Required winner rule | Required evidence |
|---|---|---|
| microphone admission vs full queue | queue state at atomic admission | no leaked reservation or PCM |
| cancel vs queued request | cancellation removes/tombstones handle | runtime invocation count is zero |
| cancel vs active request | request cancellation wins if not published | late result discarded; quiescence required |
| microphone vs imported-media completion | request identity, never global idle | unrelated completion cannot release PCM |
| shutdown vs pending/active request | shutdown rejects new work and fences each handle | one terminal outcome per handle |

The exact scheduler transaction mechanism (lock/condition, actor, or another
bounded design) is D5, but it must implement the states and ownership rules
above. The recovery supervisor, recovery budget, and process-level fallback are
D6; until D6 is approved, `RuntimeRecoveryPending` blocks new microphone
generations and cannot be treated as cleanup success.

### G2 acceptance criteria

- **G2-AC-001** — Given microphone and imported-media requests share capacity,
  when capacity is exhausted, then both observe the same FIFO bounded
  admission policy and neither source bypasses the other.
- **G2-AC-002** — Given an active fake runtime that ignores best-effort cancel,
  when the microphone handle is cancelled, then no transcript, terminal
  cancellation, audio release, or new generation occurs before that handle
  reaches `quiescent -> released`; `RuntimeRecoveryPending` keeps the operation
  blocked and is not a terminal outcome.
- **G2-AC-003** — Given an unrelated imported-media request becomes idle, when
  the microphone request remains active, then `await_quiescence(microphone)`
  does not complete.
- **G2-AC-004** — Given cancellation races with publication, when barriers are
  used instead of sleeps, then exactly one request outcome is published and
  reservation/reference release occurs exactly once.
- **G2-AC-005** — Given `try_admit()` returns the typed `QueueFullError`, when
  the already-admitted recording operation enters cleanup, then cleanup and
  canonical release complete exactly once before the error is mapped to
  `RejectedRequest(code=QueueFull)` and published; no ASR handle is created.

## Slice G3: WASAPI Policy and Format Conversion

### Entry conditions

G3a is a documentation/policy gate and must close with a written choice for
every item in the G3a decision register before implementation. G3b cannot
close on macOS or with a synthetic adapter; it requires a Windows probe on
each claimed architecture/distribution lane after the approved adapter exists.

### Normative policy that is independent of the unresolved choices

1. Enumerate active input-capable endpoints for the current session.
2. With an explicit selection, open exactly that opaque ID.
3. Without one, open the approved Windows default role exactly once at session
   open and bind to that endpoint for the full session.
4. Never capture loopback/output endpoints, mix devices, retarget on device
   change, or silently fall back.
5. A removed, disabled, busy, inaccessible, or unsupported endpoint produces a
   stable typed error and no ASR invocation.
6. Native frames never cross into Application or Domain and ordinary live
   capture does not invoke FFmpeg.

### Conversion contract

The adapter must deterministically convert the selected device format into
mono 16 kHz S16LE. It must reject malformed or non-finite input, clamp output
explicitly to `[-32768, 32767]`, define channel reduction, define sample-rate
conversion, define rounding, and classify discontinuity/overrun as failure.
No padding, sample duplication, truncation, timestamps, channel markers, or
resampler metadata may appear in `CanonicalAudio`.

G3a must select one exact input-format matrix, converter implementation, channel
coefficients, rounding policy, overflow behavior, golden-vector corpus,
tolerance, and ownership location. “Use a Windows API” or “use a library” is
not a decision. The evidence must show behavior for the highest supported
sample rate/channel count/sample type and the configured memory budget.

The G3a approval record must contain this completed policy matrix; `OPEN` is
the current value and a blocking status, not an implementation default:

| Policy field | Allowed alternatives to evaluate | Current value | Required approval evidence |
|---|---|---|---|
| Default input role | console, communications, explicit product role | OPEN | Windows endpoint-role probe and product approval |
| WASAPI mode | shared, exclusive | OPEN | contention/format probe and user-visible busy policy |
| Native format matrix | exact `WAVEFORMATEX`/`WAVEFORMATEXTENSIBLE` rate, channel, subtype, valid bits, alignment tuples | OPEN | capability inventory on every claimed lane |
| Channel mapping | exact coefficient matrix and channel-order rule | OPEN | golden vectors for mono/stereo/multichannel inputs |
| Resampler | named Windows facility or pinned project-owned converter/version | OPEN | deterministic vectors, latency, and memory report |
| Rounding/clamping | exact integer rounding, tie rule, and `[-32768,32767]` clamp | OPEN | boundary vectors including full-scale and non-finite rejection |
| Output-length rule | exact frame-count/timestamp-to-sample rule | OPEN | duration and alignment vectors; no unapproved repetition/padding |
| Buffer policy | exact period, queue capacity, overflow action | OPEN | backpressure stress and peak-memory report |
| Architecture boundary | live conversion in platform adapter; imported conversion in FFmpeg | OPEN | approved architecture overview amendment |

The conversion contract forbids *unapproved* padding or sample repetition; it
does not forbid mathematically defined resampling output. Until the output
length rule and converter are approved, no code may claim canonical conversion
compatibility.

### G3a policy acceptance criteria

- **G3a-AC-001** — Given every policy field is `OPEN`, when G3a is reviewed,
  then a single selected alternative, all rejected alternatives, owner,
  approver, evidence hash, and approval-record path are recorded before any
  implementation begins.
- **G3a-AC-002** — Given the selected policy, when the RFC/spec decision
  records are compared, then default role, mode, format tuples, converter,
  output-length, buffer, architecture wording, and D17 accounting match.

### G3b native acceptance criteria after implementation

- **G3b-AC-001** — Given each format in the approved matrix, when the same
  golden input is converted twice, then canonical bytes are identical and
  match the approved output/tolerance.
- **G3b-AC-002** — Given a device with a non-approved format, when open or
  conversion is attempted, then `UnsupportedFormat` is returned before ASR and
  no silent format fallback occurs.
- **G3b-AC-003** — Given an endpoint disconnect or overrun during capture, when
  the adapter observes it, then the session fails with its stable code, partial
  audio is discarded, and the endpoint is not replaced.
- **G3b-AC-004** — Given input at every duration/sample/byte boundary, when the
  next frame would exceed an approved limit, then capture stops without a
  truncated successful value and reports `ResourceLimitExceeded`.
- **G3b-AC-005** — Given a repeated Windows open/capture/stop/close run, when
  the named lane completes its required repetitions, then handle/thread/RSS
  counters do not increase beyond approved thresholds.

## Slice G4: Native Helper, IPC Feasibility, and Packaging

### G4a entry conditions and pre-implementation decision

Production capture is process-isolated. An in-process COM/WASAPI binding is
not an allowed production fallback because it cannot contain an access
violation, corrupted native state, or an uninterruptible call. G4a must still
prove that the helper boundary is feasible; “use a helper” alone is not proof.

G4a is a documentation-only feasibility decision. It compares candidate
technologies and produces a no-go option, target matrix, protocol outline, and
packaging decision; it does not compile, ship, or import a helper. G4a must be
approved before G3 claims a packaging lane. G4b is the later implementation
acceptance gate and cannot be used to authorize implementation early.

G4a has an explicit outcome: `GO` or `NO_GO`. Only `GO`, with selected helper,
IPC, package, and recovery alternatives, can satisfy the pre-implementation
gate. `NO_GO` is a terminal feasibility result that keeps
`implementation_allowed: false`, keeps this RFC/spec blocked, and requires a
new feasibility decision; it cannot be treated as approval by merely closing
D1–D19.

### Required helper boundary

The Python infrastructure adapter owns the abstract port and session
generation. The helper owns COM initialization, WASAPI client/endpoint
handles, callback/event handling, native conversion state, and native cleanup.
The helper is launched in a Windows Job Object with kill-on-close and is
reaped on cancellation timeout, protocol failure, crash, or shutdown. A
helper crash or kill invalidates its generation and requires a fresh helper
before another session.

### Feasibility decision and protocol requirements

G4a must compare at least named-pipe, loopback-local-socket, and shared-memory
transport designs against boundedness, current-user authentication/ACL,
crash containment, cancellation interruption, packaging, and observability.
The review must select one or record a rejected alternative with evidence.

The selected protocol must define, before implementation:

- versioned message types for hello/authentication, open, start, bounded audio
  chunk, stop, cancel, status, terminal result, and close;
- maximum frame/chunk/message/queue sizes and a rejection rule before allocation;
- generation/session correlation and monotonic deadline propagation;
- authenticated local endpoint/ACL behavior and replay/forgery handling;
- backpressure behavior: never block the audio callback indefinitely and never
  drop frames while claiming a complete recording;
- helper crash, malformed frame, timeout, version mismatch, and kill result
  mapping to stable application errors;
- a proof that neither raw PCM nor transcript text is written to disk or
  ordinary diagnostics.

### Packaging feasibility

Packaging must identify the helper artifact format, supported Windows
architectures (at least the claimed x64/ARM64 lanes), signing/provenance,
version/hash lock, install/portable paths, privacy capability/manifest
behavior, upgrade/rollback behavior, and missing/corrupt artifact errors.
The helper must not depend on a developer-only PATH or unpinned runtime.
Portable and packaged modes are separate claims until each is tested.

### G4b acceptance criteria after implementation

- **G4b-AC-001** — Given a helper crash, when the adapter detects the process
  loss, then the session returns a typed capture/cleanup failure, invalidates
  the generation, reaps the Job Object, and does not publish partial audio.
- **G4b-AC-002** — Given a competing or forged local client, when it connects or
  sends a frame, then authentication/ACL or the handshake rejects it before
  PCM is accepted.
- **G4b-AC-003** — Given a message or queue at each approved boundary, when the
  next allocation would exceed its limit, then the helper rejects/stops with a
  bounded error and remains recoverable without unbounded memory growth.
- **G4b-AC-004** — Given every claimed packaged/portable architecture, when the
  clean-machine fixture is installed and started, then the locked helper is
  located, verified, launched, and cleaned without PATH or unsigned-artifact
  assumptions.
- **G4b-AC-005** — Given a helper crash, cancellation, and normal shutdown,
  when the diagnostic artifacts are scanned, then they contain no PCM,
  transcript text, device ID/name, full path, or raw native exception.

## Cross-Slice Error and Privacy Contract

Pre-ASR-admission results are a separate discriminated union:
`RejectedRequest(code=QueueFull | InvalidSelection | InvalidLimits)`. A
recording operation is already admitted at `idle -> starting`, so
`RejectedRequest(code=QueueFull)` is its one terminal stage-rejection outcome after
capture cleanup; it creates no ASR operation, ASR handle, or retained canonical
value. Admitted ASR operation outcomes are
`Succeeded(TranscriptResult)`, `Empty`, `Failed(Capture/Asr/CleanupError)`,
and `Cancelled`; every admitted ASR operation has exactly one. Stable conceptual
capture codes are `PermissionDenied`, `DeviceUnavailable`, `DeviceBusy`,
`UnsupportedFormat`, `CaptureOverflow`, `DeviceDisconnected`, `EmptyCapture`,
`ResourceLimitExceeded`, `CaptureTimeout`, `CaptureCancelled`,
`CaptureFailed`, and `CleanupWarning`. Their final placement relative to the
existing `AsrError` hierarchy is D3. Exceptions must never become an empty
success.

Audio remains in memory and is sent only to the local ASR port. Ordinary logs,
telemetry, crash reports, minidumps, and smoke artifacts must not contain PCM,
transcript text, full paths, endpoint names/IDs, secrets, or complete native
exception strings. Diagnostics may contain stable codes, bounded timings and
counts, adapter version, and an ephemeral operation token. Python memory cannot
be claimed to be securely zeroed; the design only minimizes retention.

D19 must approve a dump/report policy before G4a approval: whether dumps are
disabled or memory-filtered, exact application/helper dump locations, retention
and consent, scanner implementation and version, canary/negative-control
fixtures, and the security/privacy approver. A byte-scan without this policy
is not privacy evidence.

The proposed initial budgets (64 MiB canonical payload, 16 MiB helper/IPC,
16 MiB conversion scratch, one 65 MiB ASR framing copy, 1 MiB diagnostics,
and 192 MiB audio-path RSS delta) are **proposals requiring G3a/G4a
approval and G3b/G4b evidence**.
The accounting must include simultaneous queued/active/recovery-owned
canonical payloads, runtime/transport copies, helper/Python overhead, and
process-tree RSS as a function of scheduler capacity. They are not permission
to allocate each category independently.

## Test and Evidence Plan

### Cross-platform evidence

Fake adapters and fake runtimes must cover device selection, single-use
sessions, limits, deadlines, cancellation races, state transitions, typed
errors, redaction, no disk spill, shared scheduler capacity, request-scoped
quiescence, late-result fencing, and exact-once cleanup. Tests run without
Windows APIs, PySide6, CUDA, model weights, or a real microphone.

### Windows evidence

The native lane must name the owner, runner label, Windows build, architecture,
distribution mode, helper/binding versions, fixture or controlled signal,
golden-vector tolerances, cancellation/cleanup/RSS thresholds, and repetition
count. Evidence records metadata, codes, timings, peak RSS, and leak counters,
never signal, PCM, or transcript content. A macOS run, emulation, file
ingestion, `windows-latest` without fixture definition, or fake adapter is not
microphone evidence.

## Acceptance Criteria

- **RFC-AC-001** — Given this RFC is reviewed, when any of D1–D19, G1, G2,
  G3a-policy, or G4a is not approved, then the microphone feature remains
  unavailable and no production implementation is authorized. G3b evidence and
  G4b validation are enablement gates.
- **RFC-AC-002** — Given G1 approval, when the fake state/race suite runs,
  then the normative recording contract, terminal ownership, and no-partial
  result rules pass.
- **RFC-AC-003** — Given G2 approval, when shared microphone/imported-media
  scheduler tests run, then FIFO bounded admission and request-scoped
  quiescence pass under cancellation and unrelated-work races.
- **RFC-AC-004** — Given G3a policy approval and the implementation exists,
  when the named G3b Windows conversion lane runs, then endpoint policy,
  format vectors, limits, disconnect/overflow, resource, and repeatability
  evidence pass.
- **RFC-AC-005** — Given G4a has outcome `GO` with protocol/package decisions
  approved and the implementation exists, when clean packaged and portable G4b lanes run, then
  helper protocol, ACL/authentication, crash recovery, artifact verification,
  and redaction evidence pass.
- **RFC-AC-006** — Given all pre-implementation decision records are approved,
  when the feature specification and this RFC are updated to `Approved`, then
  only the separately approved implementation slices may begin; enablement
  still requires G3b native evidence and G4b validation.

## Open Questions

Every item below is an explicit approval decision. The columns are mandatory
approval-record fields, not optional project-management metadata. `NONE` and
`OPEN` are deliberate blocking values. An approver must replace them with a
selected alternative, rejected alternatives, an evidence path plus SHA-256,
and an approval-record path before the decision can become `APPROVED`.

| ID | Decision and allowed alternatives | Selected alternative | Rejected alternatives | Owner | Approver | Evidence path | Evidence SHA-256 | Approval record | Decision date | RFC/spec/catalog commit | Status | Gate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| D1 | Duration/sample limit: 32 min/30,720,000 samples or product-approved value; relation to 64 MiB | NONE | NONE | Product owner | Product owner + maintainer | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G1 |
| D2 | Shell `CANCELLING` state or `ShellSnapshot` operation status | `CANCELLING` operation state is normative target; shell projection OPEN | NONE | Application owner | Maintainer + UI owner | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G1 |
| D3 | Separate capture errors or `AsrError` extension | NONE | NONE | Application owner | Maintainer | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G1 |
| D4 | Exact recording/finalization/ASR/cleanup deadlines | NONE | NONE | Application owner | Product owner + maintainer | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G1/G2 |
| D5 | Scheduler transaction: lock/condition, actor, or other bounded mechanism; capacity/reservation semantics | NONE | NONE | ASR owner | Maintainer | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G2 |
| D6 | Recovery supervisor, budget, process-level fallback, and new-generation admission | New generation is blocked while `recovery_owned`; final recovery policy OPEN | NONE | ASR owner | Maintainer + security owner | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G2 |
| D7 | Default role: console, communications, or product-defined role | NONE | NONE | Windows owner | Product owner + maintainer | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G3a |
| D8 | WASAPI shared or exclusive mode | NONE | NONE | Windows owner | Product owner + maintainer | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G3a |
| D9 | Exact `WAVEFORMATEX`/`WAVEFORMATEXTENSIBLE` rate/channel/subtype/valid-bits/alignment tuples | NONE | NONE | Windows owner | Maintainer | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G3a |
| D10 | Converter, coefficients, resampling, rounding, clamping, output length, tolerance | NONE | NONE | Audio owner | Maintainer + Windows owner | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G3a |
| D11 | Period, queue capacity, callback/event model, overflow behavior | NONE | NONE | Windows owner | Maintainer | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G3a |
| D12 | Architecture amendment: FFmpeg imported conversion versus live adapter conversion | NONE | NONE | Architecture owner | Maintainer | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G3a |
| D13 | Helper technology and supported Python/Windows matrix, including no-go option | NONE | NONE | Windows owner | Maintainer | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G4a |
| D14 | IPC primitive: named pipe, local socket, shared memory, or no-go; framing, auth, ACL, bounds | NONE | NONE | Security owner | Maintainer + Windows owner | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G4a |
| D15 | Helper artifact/signing, x64/ARM64 support, portable/package layout, privacy capability | NONE | NONE | Release owner | Maintainer + security owner | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G4a |
| D16 | Job Object limits, restart policy, and cleanup/recovery semantics | NONE | NONE | Windows owner | Maintainer + security owner | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G4a |
| D17 | Audio-path byte/RSS ceilings, simultaneous payload count, and transport copies | NONE | NONE | Performance owner | Maintainer | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G3a/G4a |
| D18 | Native owner, runner labels, Windows builds, architecture, distribution mode, fixtures, helper/binding versions, thresholds, retention | NONE | NONE | Release owner | Maintainer | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G4a |
| D19 | Dump policy: disabled/filtered, locations, retention/consent, scanner/version, canaries | NONE | NONE | Security/privacy owner | Maintainer + security owner | NONE | NONE | NONE | NONE | NONE | OPEN/BLOCKED | G4a |

Approval records must be immutable or versioned and must name the approver,
decision date, selected alternative, rejected alternatives, evidence path and
SHA-256, and the RFC/spec/catalog commit that records the decision. The catalog
entry must retain `implementation_allowed: false` until all pre-implementation
decisions are `APPROVED`.

**Implementation remains blocked until D1–D19 are approved, G1/G2/G3a/G4a have
their pre-implementation artifacts, and both this RFC and
`spec/features/005-microphone-recording.md` record the same decisions.** G3b
and G4b native evidence remain required before feature enablement.

## Exit Criteria

This RFC may change from Draft only when:

1. D1–D19 have written approvals and no conflicting open question remains.
2. G1, G2, G3a, and G4a with outcome `GO` have pre-implementation decision artifacts; G3b and
   G4b have their required post-implementation acceptance evidence before
   enablement.
3. The feature specification is catalogued and updated with the approved
   contracts; its status is still not `implemented` merely because the RFC is
   approved.
4. The architecture overview amendment, if approved, is committed with the
   same decision record.
5. A separate implementation plan names the production slices, tests, native
   lanes, rollback owner, and kill-switch owner.

Until then, the only authorized work is editing this RFC/spec/catalog and
reviewing or attaching externally produced decision evidence. No production
code, fake adapter, fake test implementation, helper prototype, packaging
change, or ASR service change is authorized.
