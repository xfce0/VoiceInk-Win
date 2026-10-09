# Feature: Microphone Recording

## Status and Scope

Status: draft — **implementation blocked**.

This specification defines the contract for one explicit local Windows
microphone recording followed by batch transcription through the shared ASR
application service. It covers the user-visible state machine, capture port,
shared ASR admission/quiescence, WASAPI policy and conversion obligations, and
the native helper/IPC/packaging evidence gate. It excludes streaming ASR,
loopback capture, multi-device mixing, automatic device fallback, global
hotkeys, history, text delivery, cloud transcription, disk recordings, and
production implementation decisions not approved in
`rfcs/microphone-recording.md`.

**Implementation is blocked until decisions D1–D19 and the pre-implementation
portions of G1, G2, G3a-policy, and G4a with outcome `GO` in the RFC are approved and recorded
here.** G3b native evidence and G4b validation are required before enablement. A
fake adapter or passing macOS test does not authorize live microphone support
or close a Windows gate.

## User Scenarios

- A user lists local input-capable devices, selects one, records, stops, and
  receives one local transcript after asynchronous processing.
- A user records with the approved operating-system default input policy; the
  session binds to the endpoint selected at open and never silently retargets.
- A user encounters a missing, busy, disconnected, unsupported, or denied
  endpoint and receives a stable actionable error without an ASR call.
- A user cancels during capture or processing and sees cancellation/cleanup
  progress, no partial audio, and no late transcript.
- A user submits microphone work while shared ASR capacity is full and receives
  a visible bounded rejection rather than an implicit retry or unbounded PCM
  retention.

## Functional Requirements

1. The application must admit at most one active microphone recording and must
   reject a second start rather than queueing it.
2. Device descriptors must be immutable and expose only an opaque selection ID,
   display name, capability flags, default-role information, and approved safe
   metadata. Application code must not parse or construct IDs.
3. A capture session must be single-use, bound to one endpoint at open, and
   unable to restart or retarget after capture starts.
4. `start`, `stop`, and `cancel` commands must return without blocking the Qt
   event thread. Native open/start/finalization/ASR calls must run off that
   thread.
5. A successful session must return exactly one immutable, non-empty
   `CanonicalAudio` value: mono, 16 kHz, signed PCM16, little-endian,
   contiguous samples.
6. Silence with accepted frames is valid canonical audio. Zero accepted frames
   is `EmptyCapture` and must not invoke ASR.
7. The adapter must reject malformed/non-finite input, discontinuity, overrun,
   unsupported format, and any buffer/duration/byte limit crossing. It must
   never return truncated audio or silently drop/pad frames.
8. The chosen default-role, WASAPI mode, native input format matrix, converter,
   coefficients, rounding, clamping, and golden-vector tolerance must be
   approved by G3a before implementation; G3b validates the implemented
   policy before enablement.
9. The production capture boundary must be a dedicated native helper process;
   an in-process native binding is not an allowed production fallback. The
   helper technology and package target decision must be approved in G4a before
   any production implementation.
10. Helper IPC must be local-only, bounded, generation-correlated,
    authenticated/ACL-restricted to the current user, and versioned. Its
    selected primitive and exact framing remain blocked decisions until G4a;
    G4b validates the implemented choice before enablement.
11. Capture must use the shared ASR application scheduler. A microphone-specific
    worker pool, hidden priority, global idle fence, or second runtime is
    forbidden.
12. Shared ASR capacity must count pending, active, and reserved requests from
    microphone and imported-media sources under one FIFO policy.
13. An admitted request must expose a request-scoped cancellation and
    quiescence handle. Audio cannot be released, cancellation published, or a
    new microphone generation accepted until that request reaches
    `quiescent -> released`; `recovery_owned` never satisfies this rule and
    requires the approved process-level recovery.
14. Every admitted recording operation must publish exactly one terminal
    outcome after capture cleanup: transcript-ready, empty, error, cancellation,
    or the pre-ASR-admission stage rejection `RejectedRequest(code=QueueFull)`. An admitted
    ASR request has exactly one of success, empty, failure, or cancellation.
    Late callbacks/results cannot overwrite a newer generation.
15. Cancellation must discard captured audio, never publish partial text, and
    remain visible until capture and any specific ASR request have fenced their
    resources.
16. Cleanup must be bounded, idempotent, and attempted on success, failure,
    cancellation, device loss, helper crash, timeout, and application shutdown.
17. Stable errors must preserve the distinction between permission, device,
    format, overflow, resource, timeout, cancellation, ASR, and cleanup
    failures. Exceptions must never become an empty success.
18. No raw PCM, transcript text, full paths, endpoint names/IDs, secrets, or
    raw native exception strings may be written to ordinary logs, telemetry,
    crash reports, minidumps, or acceptance artifacts.
19. A dump/report policy must specify disabled or filtered dumps, exact
    application/helper locations, retention and consent, scanner/version,
    canaries and negative controls before G4a can be approved.

## Non-Functional Requirements

- The layer direction remains `Presentation -> Application -> Domain <-
  Infrastructure`; native and Qt types do not cross into Domain.
- Capture queues, IPC messages, conversion scratch, transport copies, and
  diagnostics must have explicit byte ceilings. D17/G3a/G4a must approve the
  accounting before implementation; G3b/G4b resource evidence is required
  before enablement.
- Capture and conversion are `O(n)` in accepted samples with bounded queue
  space `O(b)` for configured buffer capacity. Queue admission and release are
  `O(1)` amortized; cancellation may use tombstones and compaction.
- The canonical PCM allocation is bounded by the approved duration/sample and
  64 MiB payload limits. No disk spill is allowed in v1.
- Windows policy behavior, format vectors, helper crash recovery, packaging,
  and privacy behavior must be tested on named supported Windows/build,
  architecture, and distribution lanes. macOS evidence is cross-platform
  contract evidence only.
- The helper artifact must be versioned, provenance-recorded, hash-locked,
  and independently validated for every claimed x64/ARM64 and packaged/
  portable mode. It must not rely on developer PATH state.
- Presentation receives immutable snapshots/events and uses accessible labels
  for recording, processing, cancelling, error, and recovery states.

## Error and Cancellation Behavior

The authoritative operation state machine is:

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

starting/recording/finalizing/asr_queued/asr_running
    --cancellation or failure--> cancelling/cleanup
cleanup --success--> pending terminal outcome (transcript_ready/empty/error/cancelled)
cleanup --failure--> recovery_owned (non-terminal)
recovery_owned --all ownership release proven--> Failed(CleanupWarning) -> shell error
terminal outcome --reset--> idle
```

`cancelled` is a terminal operation outcome that is projected to shell `idle`
only after cleanup. D2 decides whether the shell exposes `cancelling` as a
state or operation status; it cannot remove the operation state or make it
`processing`.

The state transition/resource-owner contract is:

| State | Owner | Cancellation/terminal rule |
|---|---|---|
| `starting` | orchestration; no canonical value or ASR handle | cancel/failure closes session; ASR invocation count remains zero |
| `recording` | capture session owns bounded frames | cancel or device/overflow/deadline failure discards frames; ASR is not invoked |
| `finalizing` | orchestration owns capture and canonical value until ASR admission | cancel/failure/limit enters cleanup; `QueueFullError` is retained as internal `StageAdmissionFull` until cleanup releases value exactly once, then maps to `RejectedRequest(code=QueueFull)` |
| `asr_queued` | scheduler handle owns canonical value after atomic admission | cancellation leaves handle owning value until quiescence/recovery |
| `asr_running` | ASR worker/service owns canonical value | result or runtime/deadline failure enters cleanup; late result is discarded if cancellation generation won |
| `cancelling` | orchestration coordinates capture and optional ASR handle | no publication before both fences complete |
| `cleanup` | named cleanup/recovery owner | release each resource exactly once; success publishes the pending outcome, while a deadline enters non-terminal `recovery_owned` |
| `recovery_owned` | named supervisor/process-level recovery owner | no publication, audio release, or new generation; after `resource_release_proven` publish exactly one `Failed(CleanupWarning)` |
| terminal outcome/rejection | no live audio owner after cleanup/release | exactly one admitted outcome or one `RejectedRequest(code=QueueFull)` stage rejection; reset may return shell to `idle` |

## Shared ASR Scheduler and Quiescence Contract

The microphone request uses the imported-media scheduler and this exact
ownership sequence:

```text
reserved -> queued -> active -> quiescent -> released
                         \-> recovery_owned
```

Before the ASR admission linearization point, orchestration owns canonical audio.
Successful admission transfers the only canonical reference to the request
handle/service; `QueueFull` is a pre-ASR-admission `RejectedRequest` of the
already-admitted recording operation and leaves ownership with orchestration
for exactly-once release. `recovery_owned` means
that a named supervisor still owns the handle and audio after a bounded fence
deadline. It is not quiescence: until `resource_release_proven` confirms
`quiescent -> released` and all audio/ASR/helper/native ownership is released,
no new microphone generation may be admitted and no terminal result may be
published. Process-level recovery or restart is only a method to establish
`resource_release_proven`; recovery ownership never permits publication or
release.

Quiescence requires that the identified request is absent from pending and
active sets, cannot be consumed or publish a result, and the shared service
retains no canonical reference. An unrelated imported-media request becoming
idle cannot satisfy this condition. The scheduler is FIFO across sources and
capacity counts pending, active, and reserved requests. D5 chooses the
concrete transaction mechanism; D6 chooses the recovery supervisor/budget.

The request contract is:

```text
try_admit(request) -> AsrRequestHandle | QueueFullError
handle.cancel(deadline) -> None | AsrError
handle.await_result(deadline) -> TranscriptResult | AsrError
handle.await_quiescence(deadline) -> None | RuntimeRecoveryPending
```

The existing shared scheduler returns typed `QueueFullError`. The already-admitted
recording orchestration then closes capture, releases its canonical value, and
publishes one recording-operation `RejectedRequest(code=QueueFull)` outcome after cleanup;
it is not the return type of a successful ASR admission.

## WASAPI Policy and Conversion Decision Surface

G3a must approve each `OPEN` field before implementation. The accepted policy
must record exact values, not a generic “use WASAPI” statement:

| Field | Alternatives to evaluate | Current value |
|---|---|---|
| Default role | console, communications, product-defined | OPEN |
| WASAPI mode | shared, exclusive | OPEN |
| Input formats | exact `WAVEFORMATEX`/`WAVEFORMATEXTENSIBLE` tuples | OPEN |
| Channel mapping | coefficient matrix and channel order | OPEN |
| Resampling | named facility or pinned converter/version | OPEN |
| Rounding/clamping | tie rule, integer conversion, signed range | OPEN |
| Output length | exact sample-count rule; no unapproved padding/repetition | OPEN |
| Buffer policy | period, queue capacity, overflow action | OPEN |
| Architecture wording | FFmpeg for imported media; adapter for live frames | OPEN |

The approval must include a corpus hash, golden-vector tolerances, highest
format memory evidence, and the Windows lane on which the policy was probed.

## Native Helper and Packaging Decision Surface

G4a is a documentation-only feasibility and target/package decision. It must
compare named pipe, local loopback socket, shared memory, and an explicit
no-go alternative for boundedness, current-user ACL/authentication, crash
containment, cancellation, packaging, and observability. It must define the
versioned message set, maximum frame/chunk/message/queue sizes, generation and
deadline fields, replay/forgery rejection, and error mapping before production
code is authorized. G4b validates the selected design after implementation.

G4a has an explicit outcome: `GO` or `NO_GO`. Only `GO`, with selected helper,
IPC, package, and recovery alternatives, can satisfy the pre-implementation
gate. `NO_GO` keeps `implementation_allowed: false`, keeps this feature
blocked, and requires a new feasibility decision; closing D1–D19 alone cannot
authorize implementation.

The G4a record must specify helper artifact format, signing/provenance,
hash-lock, x64/ARM64 support, portable/packaged paths, privacy capability,
upgrade/rollback, missing/modified artifact behavior, and Job Object/restart
policy. No helper prototype, packaging change, or ASR service change is
authorized while this feature is blocked.

Capture failures include `PermissionDenied`, `DeviceUnavailable`,
`DeviceBusy`, `UnsupportedFormat`, `CaptureOverflow`,
`DeviceDisconnected`, `EmptyCapture`, `ResourceLimitExceeded`,
`CaptureTimeout`, `CaptureCancelled`, and `CaptureFailed`. The final type
hierarchy is D3. `CleanupWarning` is a recovery failure and must prevent the
application from claiming that native resources are closed.

Cancellation rules:

- From `recording`, signal capture cancellation, discard bounded frames, close
  the session, and never invoke ASR.
- From `finalizing`, cancel capture without an ASR handle; from `asr_queued` or
  `asr_running`, cancel the specific ASR handle. Discard late output and await
  request-scoped quiescence before releasing any audio/native ownership or
  publishing terminal cancellation.
- On shutdown, reject new starts, cancel active work, fence cleanup, and close
  the shared ASR service according to its approved recovery policy.
- Cancellation competes with completion under a generation/terminal CAS. The
  first committed publication wins; a result already published is not
  rewritten.
- Queue-full ASR admission is a visible stage rejection with no implicit retry
  and no retained canonical payload. The admitted recording operation publishes
  exactly one `RejectedRequest(code=QueueFull)` after capture cleanup; an admitted ASR
  request has exactly one of `Succeeded`, `Empty`, `Failed`, or `Cancelled`.
- `RuntimeRecoveryPending` is non-terminal. It forbids publication, audio
  release, and a new generation until `resource_release_proven`. Recovery
  completion publishes exactly one `Failed(CleanupWarning)` and never
  publishes a pending outcome afterward.

## Acceptance Criteria

- **AC-001** — Given a valid selected endpoint and approved limits, when the
  user records and stops, then one bounded canonical value reaches the shared
  ASR path and the GUI thread remains responsive.
- **AC-002** — Given no explicit endpoint, when recording starts, then the
  approved default-role policy is applied once, the chosen endpoint remains
  bound for the session, and a default-device change cannot retarget it.
- **AC-003** — Given a missing, denied, busy, unsupported, or disconnected
  endpoint, when the failure occurs, then the matching stable error is visible,
  no fallback occurs, and ASR invocation count is zero.
- **AC-004** — Given silence with accepted frames, when recording stops, then
  valid canonical silence is submitted; given zero frames, then `EmptyCapture`
  is published and ASR invocation count is zero.
- **AC-005** — Given the next frame would cross an approved limit, when it is
  offered, then capture fails with `ResourceLimitExceeded`, returns no
  truncated audio, and releases all native resources.
- **AC-006** — Given cancellation races with start, finalization, ASR
  completion, reset, or shutdown, when synchronization barriers exercise the
  race, then exactly one fenced outcome is published and no partial transcript
  is visible.
- **AC-007** — Given ASR is blocked and an unrelated imported-media request
  completes, when microphone cancellation awaits quiescence, then the
  unrelated completion cannot satisfy the microphone fence or release its
  audio.
- **AC-008** — Given shared ASR capacity is full, when a microphone request is
  finalized, then the scheduler returns `QueueFullError`, cleanup and
  canonical release complete exactly once, only then is
  `RejectedRequest(code=QueueFull)` published, no ASR handle is created, and
  no implicit retry occurs.
- **AC-009** — Given a helper crash, malformed IPC, or kill-on-close timeout,
  when cleanup runs, then the generation is invalidated, the helper is reaped,
  partial audio is discarded, and a typed recovery error is visible only after
  all audio/native resource ownership is released and proven, either by normal
  quiescence or approved process-level recovery; releasing one audio reference
  alone is insufficient. Otherwise `RuntimeRecoveryPending` remains
  non-terminal and blocks new generations.
- **AC-010** — Given an input in the approved format matrix, when conversion
  runs twice on the same golden vector, then the canonical bytes are
  deterministic and meet the approved output/tolerance.
- **AC-011** — Given an input outside the approved format matrix, when open or
  conversion is attempted, then `UnsupportedFormat` is returned without
  silent fallback or ASR invocation.
- **AC-012** — Given every claimed Windows architecture and distribution mode,
  when a clean-machine acceptance run uses the locked helper artifact, then
  launch, authentication, capture, cleanup, and artifact verification pass
  without PATH assumptions.
- **AC-013** — Given ordinary logs, crash reports, minidumps, and evidence
  outputs, when they are byte-scanned with PCM/transcript/device canaries, then
  no sensitive capture data is present.
- **AC-014** — Given any decision D1–D19 is not approved, when an engineer
  attempts to begin implementation, then the feature remains marked blocked
  and no production code or live microphone capability may be enabled.
- **AC-015** — Given G1, G2, G3a, and G4a has outcome `GO`, and both RFC/spec
  documents contain the same decisions, when status is changed to approved,
  then a separate implementation plan is required before production code
  starts; G3b/G4b native evidence remains required before enablement.

## Test Plan

- **Domain/application unit tests:** state transitions, command admission,
  generation fencing, typed error mapping, limits, deadline arithmetic, and
  exactly-once terminal publication.
- **Fake adapter contract tests:** device selection, single-use lifecycle,
  cancellation races, deadline expiry, buffer overflow, no truncation, no disk
  spill, cleanup idempotence, and redaction.
- **Shared ASR integration tests:** FIFO capacity across both sources,
  request-scoped cancellation/quiescence, blocked runtime, unrelated request,
  late result, reservation ownership, and shutdown.
- **Conversion tests:** table-driven and golden-vector tests for every approved
  format, non-finite/malformed input, downmix, resampling, rounding, clamping,
  discontinuity, boundaries, and deterministic output.
- **Windows native tests:** actual default-role and explicit-device policy,
  privacy denial, device removal, busy/unsupported format, overflow,
  cancellation latency, helper crash, Job Object cleanup, repeated
  start/stop/close, peak RSS, handle/thread leak counters, and no retargeting.
- **IPC/security tests:** competing local client, forged handshake, replay,
  malformed/oversized frames, generation mismatch, version mismatch,
  backpressure, kill/restart, and ACL behavior.
- **Packaging tests:** clean portable and packaged x64/ARM64 lanes, locked
  artifact verification, missing/modified artifact failures, upgrade/rollback,
  privacy capability behavior, and no developer PATH dependency.
- **Evidence tests:** scan diagnostics and crash/report locations for PCM,
  transcript text, endpoint identity, full paths, secrets, and raw native
  exception strings.

## Open Questions and Deferred Work

The following exact decisions mirror the RFC. Every row must have selected and
rejected alternatives, owner, approver, evidence path plus SHA-256, approval
record path, and status. Current `NONE`/`OPEN` values are blocking.

The synchronized evidence register is
`docs/microphone-gate-evidence.md`. `APPROVAL_REQUIRED`, `OPEN`, and
`WINDOWS_REQUIRED` are evidence statuses, not approvals; all decision records
remain pending until the required approver fields and hashes are supplied.

| ID | Decision and allowed alternatives | Selected alternative | Rejected alternatives | Owner | Approver | Evidence path | Evidence SHA-256 | Approval record | Decision date | RFC/spec/catalog commit | Status | Gate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| D1 | Duration/sample limit: 32 min/30,720,000 samples and 64 MiB candidate from the existing imported-media contract; product approval remains required | Not evaluated | Product owner | Product owner + maintainer | `docs/microphone-gate-evidence.md#d1`; `rfcs/imported-media-transcription.md`; `src/voiceink_win/domain/models.py` | `9389b32b4644cf670512c57d1b56cba4a66b9bdb730591648f4db2459320dea9`; `8770188aff26443c59981e010343ea9227acab8d937bd19f4a73cbaa6e32dc70` | PENDING | PENDING | PENDING | APPROVAL_REQUIRED | G1 |
| D2 | `CANCELLING` operation state is normative; current shell projection remains open | Not evaluated | Application owner | Maintainer + UI owner | `docs/microphone-gate-evidence.md#d2`; `src/voiceink_win/domain/shell.py`; `src/voiceink_win/application/shell_controller.py` | `258ddd1b75f2a74a973b52a1636eba28d6dc52f47414a26bfef8bf91dec583f2`; `f12c5771b6e5ec3e4672f86b2e8265383ea8c7ff4fef2a14cbfebf187ca9b7d8` | PENDING | PENDING | PENDING | APPROVAL_REQUIRED | G1 |
| D3 | Separate capture-error family candidate; existing `AsrError` remains for shared ASR/runtime failures | Not evaluated | Application owner | Maintainer | `docs/microphone-gate-evidence.md#d3`; `src/voiceink_win/domain/errors.py`; `src/voiceink_win/domain/imported_errors.py` | `50968080bf40c5e42bf666a4dc50ce8ed494979e5be1b500bb561f2b8049c75d`; `a0ff5bf6a2a0ba84d5d5ae801590005196d5cc3c836d63105a4b94d9d8c778c8` | PENDING | PENDING | PENDING | APPROVAL_REQUIRED | G1 |
| D4 | Absolute monotonic deadlines are required; exact recording/finalization/ASR/cleanup values are unresolved | None | Application owner | Product owner + maintainer | `docs/microphone-gate-evidence.md#d4`; `rfcs/imported-media-transcription.md` | `9389b32b4644cf670512c57d1b56cba4a66b9bdb730591648f4db2459320dea9` | PENDING | PENDING | PENDING | OPEN | G1/G2 |
| D5 | Existing lock/condition FIFO reservation pattern is the candidate; shared ASR request-scoped quiescence is still missing | Actor/unbounded queue not evaluated | ASR owner | Maintainer | `docs/microphone-gate-evidence.md#d5`; `src/voiceink_win/application/import_queue.py`; `src/voiceink_win/domain/imported_job.py` | `17ead2b8b57b6ebb5a236d62ee6be2e3033964aa1cd41449b0aacc51a71b09bf`; `55ad2284a86280d25a0942e17a95e70a1a4afb08ae7adf290ab46d3168b7fc8b` | PENDING | PENDING | PENDING | APPROVAL_REQUIRED | G2 |
| D6 | Block new generations while `recovery_owned`; helper recovery budget remains open | No silent release/fallback | ASR owner | Maintainer + security owner | `docs/microphone-gate-evidence.md#d6`; `src/voiceink_win/application/import_service.py` | `2af166385acf6c52f4b20fb266b218f447929dcee5c94dc697b00c566e7dfe39` | PENDING | PENDING | PENDING | OPEN | G2 |
| D7 | NONE | Console/communications/product role not rejected | Windows owner | Product owner + maintainer | `docs/evidence/microphone/<run-id>/g3a/endpoint-role.json` | NONE | PENDING | PENDING | PENDING | WINDOWS_REQUIRED | G3a |
| D8 | NONE; shared mode is only the candidate contract | Exclusive not rejected | Windows owner | Product owner + maintainer | `docs/evidence/microphone/<run-id>/g3a/wasapi-mode.json` | NONE | PENDING | PENDING | PENDING | WINDOWS_REQUIRED | G3a |
| D9 | NONE | No format tuple rejected | Windows owner | Maintainer | `docs/evidence/microphone/<run-id>/g3a/format-inventory.json` | NONE | PENDING | PENDING | PENDING | WINDOWS_REQUIRED | G3a |
| D10 | NONE | No converter alternative rejected | Audio owner | Maintainer + Windows owner | `docs/evidence/microphone/<run-id>/g3a/golden-vectors.json` | NONE | PENDING | PENDING | PENDING | WINDOWS_REQUIRED | G3a |
| D11 | NONE | No buffer alternative rejected | Windows owner | Maintainer | `docs/evidence/microphone/<run-id>/g3a/buffer-stress.json` | NONE | PENDING | PENDING | PENDING | WINDOWS_REQUIRED | G3a |
| D12 | Live adapter conversion for device frames; FFmpeg remains imported-media-only candidate amendment | Existing wording not rejected until approval | Architecture owner | Maintainer | `docs/microphone-gate-evidence.md#d12`; `spec/architecture/overview.md` | `3bb15b0064a01af77b4f7e0f3520941e65d7852c90c27cda15d597a2af1e9` | PENDING | PENDING | PENDING | APPROVAL_REQUIRED | G3a |
| D13 | NONE; C++20/MSVC helper is a candidate only | Alternatives not rejected | Windows owner | Maintainer | `docs/evidence/microphone/<run-id>/g4a/helper-feasibility.json` | NONE | PENDING | PENDING | PENDING | WINDOWS_REQUIRED | G4a |
| D14 | NONE; named pipe/current-user ACL is a candidate only | Socket/shared-memory/no-go not rejected | Security owner | Maintainer + Windows owner | `docs/microphone-gate-evidence.md#d14`; `docs/evidence/microphone/<run-id>/g4a/named-pipe-acl.json` | NONE | PENDING | PENDING | PENDING | WINDOWS_REQUIRED | G4a |
| D15 | NONE | No package lane rejected | Release owner | Maintainer + security owner | `.github/workflows/native-smoke.yml`; `.github/native-smoke/artifact-lock.template.json` | `e56fc7e2f005eb5ac6a372b11c761da5d5bf9857ec532f0005462b974231a0cd`; `38835b09e7b35173d2b6f05e0713dcfb7b343493d5a8812d497669ed8507fc0e` | PENDING | PENDING | PENDING | WINDOWS_REQUIRED | G4a |
| D16 | NONE; existing Job Object code is pattern evidence only | No restart policy rejected | Windows owner | Maintainer + security owner | `src/voiceink_win/infrastructure/process.py` | `4e10f55953a845f3caed03cc67876cb3e03e980b4a970c7e62eeb8e29ce4b871` | PENDING | PENDING | PENDING | WINDOWS_REQUIRED | G4a |
| D17 | NONE; canonical and diagnostic ceilings are partial evidence only | No memory budget rejected | Performance owner | Maintainer | `src/voiceink_win/domain/models.py`; `rfcs/native-windows-runtime-startup.md` | `8770188aff26443c59981e010343ea9227acab8d937bd19f4a73cbaa6e32dc70`; `2fc51b6683e716adff85e29c7f0016c5599fa8ac6087e6d35fadba54cb1770c2` | PENDING | PENDING | PENDING | APPROVAL_REQUIRED | G3a/G4a |
| D18 | NONE; current workflow is imported-media x64 evidence only | No microphone lane rejected | Release owner | Maintainer | `.github/workflows/native-smoke.yml`; `.github/native-smoke/artifact-lock.template.json` | `e56fc7e2f005eb5ac6a372b11c761da5d5bf9857ec532f0005462b974231a0cd`; `38835b09e7b35173d2b6f05e0713dcfb7b343493d5a8812d497669ed8507fc0e` | PENDING | PENDING | PENDING | WINDOWS_REQUIRED | G4a |
| D19 | NONE; existing report sanitizer is reusable evidence only | Dump policy alternatives not rejected | Security/privacy owner | Maintainer + security owner | `src/voiceink_win/infrastructure/reporting.py`; `rfcs/native-windows-runtime-startup.md` | `b5fa0246529064e4191b87e945de26515a51e35bbd2425fb14a523574d90d2f4`; `2fc51b6683e716adff85e29c7f0016c5599fa8ac6087e6d35fadba54cb1770c2` | PENDING | PENDING | PENDING | APPROVAL_REQUIRED | G4a |

Implementation, feature enablement, and status `approved` are blocked until
every decision is approved with the evidence named in the RFC and the result
is synchronized into this specification and the catalog. Streaming, disk
spill, automatic downloads, cloud providers, history, hotkeys, installer
rollout, and text delivery remain deferred beyond this feature.
