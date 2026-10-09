# Feature: First Windows Microphone Capture Slice

## Status and Scope

Status: draft - implementation blocked.

This feature defines one explicit local Windows microphone recording followed
by batch transcription through the shared ASR application service. It covers
device enumeration/selection, one bounded start/stop/cancel session, canonical
mono 16 kHz S16LE conversion, typed terminal outcomes, cancellation fences,
native helper isolation, and validation evidence.

This document mirrors `rfcs/microphone-recording.md`. Its concrete values are
recommendations only. Implementation is blocked until D1-D19 and the
pre-implementation portions of G1, G2, G3a, and G4a with outcome `GO` are
approved and recorded in both documents. G3b and G4b are still required before
enablement. No fake adapter, passing macOS test, or generic Windows CI run
closes a native gate.

## User Scenarios

- A user lists active local input endpoints, selects one, records, stops, and
  receives one local transcript after asynchronous processing.
- A user records with the approved Windows default input role; the endpoint is
  resolved once at open and remains bound for the session.
- A user sees a stable actionable permission, unavailable, busy, unsupported,
  disconnected, overflow, timeout, or cleanup error without fallback.
- A user cancels during capture or ASR and sees cancellation/recovery progress,
  no partial audio result, and no late transcript.
- A user submits microphone work while shared ASR capacity is full and sees a
  bounded `QueueFull` rejection without retry or retained PCM.

## Functional Requirements

1. The application admits at most one active microphone generation and rejects
   a second start rather than queueing it.
2. Enumeration returns only active `eCapture` endpoints with a random,
   process-local selection token, bounded display name, default-role flag, and
   approved safe format metadata. The Windows adapter maps the token to the
   native `IMMDevice::GetId` only at `open()`; Domain/Application never hold or
   log the native ID. Tokens expire when the enumeration snapshot is replaced.
3. An explicit selection opens exactly that endpoint. A default selection uses
   `IMMDeviceEnumerator::GetDefaultAudioEndpoint(eCapture, eConsole)` once at
   open. There is no silent fallback or retargeting.
4. A session is single-use, bound to one endpoint at open, and cannot restart
   or switch device after capture starts.
5. Start, stop, and cancel return without blocking the Qt event thread. Native
   open/start/finalization/ASR calls run outside Presentation.
6. Production capture uses a separate C++17 MSVC/Windows SDK helper. Python
   does not directly import COM, WASAPI, `ctypes`, or `pywin32` audio APIs.
7. The proposed transport is a parent-created per-generation current-user
   named pipe using `FILE_FLAG_FIRST_PIPE_INSTANCE`, `PIPE_REJECT_REMOTE_CLIENTS`,
   a bootstrap challenge, HKDF-derived direction-specific HMAC session key,
   generation/sequence/deadline fields, a fixed version-1 frame, 64 KiB frame
   bound, 32 KiB canonical chunk bound, and 4 MiB queue bound. There is no
   reconnect. The parent owns the client end and the helper owns the inherited
   server handle; the parent verifies the connected server PID and helper HMAC
   before `OPEN`. G4a must approve this choice before implementation.
8. The helper uses shared event-driven WASAPI with `IAudioCaptureClient` and
   never captures loopback or uses exclusive mode in the proposed v1 policy.
9. The proposed input matrix is interleaved mono/stereo PCM16 or float32 at
   16000, 32000, 44100, 48000, or 96000 Hz with approved channel masks. Other
   tuples return `UnsupportedFormat` before ASR.
10. The proposed converter is a pinned static SpeexDSP wrapper. It maps PCM16
    to float64, downmixes stereo with 0.5/0.5, starts at source phase zero,
    emits cumulative `floor(total_input_frames * 16000 / input_rate)` samples,
    uses the approved round-half-away/clamp rule, and emits S16LE. EOS output
    outside the target count is a conversion failure. G3a must approve the
    exact version, vectors, and tolerance.
11. A successful stop returns exactly one immutable non-empty
    `CanonicalAudio`: mono, 16 kHz, signed PCM16, little-endian, contiguous.
12. Accepted all-zero frames are valid silence. Zero accepted frames is
    `Empty` and invokes ASR zero times.
13. Malformed packets, non-finite samples, later discontinuity, overrun,
    unsupported format, converter failure, and every approved limit crossing
    are typed failures. A discontinuity flag on the first accepted packet is
    tolerated because no preceding packet exists. The adapter never silently
    drops, pads, repeats, truncates, or spills to disk.
14. The proposed limits are 32 minutes, 30,720,000 canonical samples,
    61,440,000 canonical bytes, 4 MiB native queue, 4 MiB IPC queue, 16 MiB
    conversion scratch, a 64 MiB helper Job Object memory limit, and a 192 MiB
    process-tree RSS acceptance ceiling. D1/D11/D17 must approve the
    accounting; a single runtime RSS cap across Python, ASR, and helper is not
    claimed.
15. Every operation receives an absolute monotonic deadline. Proposed values
    are enumeration 2 seconds, open/handshake 5 seconds, finalization 10
    seconds, ASR 45 minutes, and cleanup/reap 5 minutes.
16. Capture uses the shared ASR scheduler. A microphone-specific worker pool,
    hidden priority, global idle fence, or second runtime is forbidden.
17. Shared ASR capacity counts pending, active, and reserved microphone and
    imported-media requests under one FIFO policy. The proposed microphone
    reservation occurs only at post-finalization `try_admit()`, not during
    capture. A request-scoped handle exposes cancel and quiescence.
18. Audio cannot be released, cancellation published, or a new microphone
    generation accepted until the specific request reaches
    `quiescent -> released`. `recovery_owned` is not quiescence. If release is
    not proven after the approved 60-second escalation, a watchdog has 30
    seconds to terminate the helper/ASR jobs and application; it publishes no
    terminal result and the next launch keeps microphone disabled.
19. Each admitted recording operation publishes exactly one terminal outcome:
    `Succeeded`, `Empty`, `Failed`, `Cancelled`, or post-cleanup
    `RejectedRequest(code=QueueFull)` when ASR admission never occurred.
20. Late callbacks/results cannot overwrite a newer generation. Terminal
    publication is owned by one generation CAS/orchestration owner.
21. Cleanup is bounded, idempotent, and attempted on success, failure,
    cancellation, device loss, helper crash, timeout, and shutdown.
22. Windows privacy/access failures do not mutate settings or registry state.
    Device loss discards partial audio and requires a fresh open.
23. The helper is launched in a Job Object with kill-on-close, one active
    process limit, bounded reap, and generation invalidation. There is no
    in-process capture fallback.
24. The first package claim is Windows 10 22H2 and Windows 11 23H2/24H2, x64,
    Python 3.12-3.14, portable onedir. ARM64 and installer/MSIX claims are
    deferred until separate evidence exists.
25. Logs, telemetry, crash reports, dumps, and evidence contain no raw PCM,
    transcript text, endpoint identity, full paths, secrets, or raw native
    exception strings.

## Non-Functional Requirements

- The layer direction remains `Presentation -> Application -> Domain <-
  Infrastructure`; native and Qt types do not cross into Domain.
- The helper owns COM, WASAPI packets, native conversion state, and native
  cleanup. Python owns typed orchestration and final canonical allocation.
- The final canonical allocation is `O(n)` in samples. Capture/conversion are
  `O(n)` with bounded queue space `O(b)`. Queue admission/release are `O(1)`
  amortized; cancellation may use tombstones.
- No audio recording is written to disk. Imported-media FFmpeg conversion and
  live microphone conversion remain separate boundaries.
- Portable and packaged lanes are separate claims. The helper path is relative
  to a verified bundle root and never depends on PATH.
- A release helper is Authenticode-signed and hash-locked. Unsigned developer
  output is not enablement evidence.
- Cross-platform tests run without Windows APIs, PySide6, CUDA, model weights,
  or a physical microphone. Native claims require named Windows evidence.

## Error and Cancellation Behavior

The authoritative operation state machine is:

```text
idle -> starting -> recording -> finalizing -> asr_queued -> asr_running
  |        |          |             |              |             |
  |        |          |             |              +-> cancelling|
  |        |          |             +-> cancelling               |
  |        +-> cancelling                                          |
  +--------------------------------------------------------------> cancelling

starting/recording/finalizing/asr_queued/asr_running
    -> cleanup -> terminal outcome
    cleanup --deadline--> recovery_owned (non-terminal)
    recovery_owned --release proven--> Failed(CleanupWarning)
```

Cancellation is idempotent signalling, not cleanup proof. During capture, the
helper stops accepting frames, drains/discards bounded frames, closes the
endpoint, and is reaped. During ASR, only the identified request handle is
cancelled. No result, audio release, terminal cancellation, or new generation
is published before the specific quiescence fence and cleanup succeed.

The shared scheduler ownership sequence is:

```text
reserved -> queued -> active -> quiescent -> released
                           \-> recovery_owned
```

The minimum stable capture codes are `PermissionDenied`, `DeviceUnavailable`,
`DeviceBusy`, `UnsupportedFormat`, `CaptureOverflow`, `DeviceDisconnected`,
`ResourceLimitExceeded`, `CaptureTimeout`, `CaptureCancelled`, `CaptureFailed`,
and `CleanupWarning`. ASR and recovery codes remain typed separately. An
exception never becomes empty success.

Windows privacy denial maps to `PermissionDenied` and an actionable Windows
privacy-settings message. `AUDCLNT_E_DEVICE_INVALIDATED`, unplug, disable,
and removal map to `DeviceDisconnected`. No endpoint fallback or auto-switch
is attempted. `recovery_owned` is visible and blocks new microphone work until
resource release is proven.

## Shared ASR Scheduler and Quiescence Contract

The microphone uses the imported-media scheduler and no second worker pool.
The target contract is:

```text
try_admit(request) -> AsrRequestHandle | QueueFullError
handle.cancel(deadline) -> None | AsrError
handle.await_result(deadline) -> TranscriptResult | AsrError
handle.await_quiescence(deadline) -> None | RuntimeRecoveryPending
```

Admission has one linearization point. Before it, orchestration owns the only
canonical reference. After successful admission, the handle owns it. A queue
full result leaves ownership with orchestration for exactly-once release and
creates no handle. FIFO fairness and capacity reservation are shared with
imported media. An unrelated request becoming idle cannot satisfy a microphone
fence.

G2 must approve the scheduler transaction, recovery supervisor, recovery
budget, and process-level fallback before Slice 1 implementation.

## Windows Policy and Conversion Recommendation

The proposed policy is intentionally exact but unapproved:

| Field | Recommendation | Approval |
|---|---|---|
| Default role | `eCapture` + `eConsole`, resolve once at open | D7/G3a |
| WASAPI | shared event-driven `IAudioClient`/`IAudioCaptureClient` | D8/G3a |
| Input matrix | PCM16/float32, mono/stereo, 16/32/44.1/48/96 kHz | D9/G3a |
| Channel mapping | mono identity; stereo `0.5 * L + 0.5 * R` | D10/G3a |
| Resampler | pinned static SpeexDSP wrapper | D10/G3a |
| Output length | floor of rate ratio; no padding/repetition | D10/G3a |
| Buffering | 4 MiB bounded queue; overflow fails | D11/G3a |
| Boundary | FFmpeg for imported media; helper for live frames | D12/G3a |

All approved formats must convert deterministically to canonical S16LE. The
golden corpus must cover each tuple, both channel layouts, full-scale and
clamp boundaries, non-finite input, output length, and repeated conversion.

PCM16 and float32 use the same decode-to-float64, downmix, resample, and final
quantization pipeline; only the input decoder differs. The golden vectors are
the normative byte output, including mono input with no resampling.

`AUDCLNT_BUFFERFLAGS_SILENT` is valid zero-valued audio for the reported frame
count. `AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY` is accepted only on the first
accepted packet, where no preceding packet exists, and fails the session on
every later packet. Timestamp errors, malformed alignment, failed
`ReleaseBuffer`, and converter output-count mismatch are typed failures. The
helper calls `GetBuffer`/`ReleaseBuffer` exactly once per packet and drains
until `AUDCLNT_S_BUFFER_EMPTY` on normal stop.

The version-1 pipe header is `magic[4]`, `version:u16`, `type:u8`,
`flags:u8`, `generation:u64`, `sequence:u64`, `deadline_ns:u64`, and
`payload_length:u32`, followed by the payload and `hmac_sha256[32]`. Allowed
message types are `HELLO`, `OPEN`, `START`, `AUDIO`, `STOP`, `CANCEL`,
`STATUS`, `RESULT`, and `CLOSE`. Replay, wrong-generation, wrong-direction,
and oversized frames fail closed before allocation.

## Native Helper and Packaging Recommendation

G4a must compare the recommended C++17 helper/named pipe with loopback TCP,
shared memory, and no-go alternatives for boundedness, current-user
authentication/ACL, cancellation interruption, crash containment, packaging,
and observability. Only an approved G4a `GO` permits production code.

The proposed bundle is:

```text
voiceink-shell.exe
audio/voiceink-audio-helper.exe
audio/voiceink-audio-helper.manifest.json
README.txt
```

The manifest contains schema, version, architecture, provenance, SHA-256,
license, and allowed relative path, but is metadata only. The expected helper
digest and pinned publisher are in the read-only artifact lock embedded in the
frozen Python bundle. Launch opens and hashes the package helper no-follow,
copies it to a per-generation private no-modify directory, verifies the copy
and `WinVerifyTrust`, starts it suspended, and compares the child image path
and volume/file identity before resuming. Any mismatch terminates the Job
Object before audio starts.
Job Object kill-on-close and helper reaping are part of G4b evidence. Missing,
changed, unsigned, crashing, or unreachable helper artifacts produce typed
unavailable/failure states, never a Python/native fallback.

## Acceptance Criteria

- **AC-001** - While any D1-D19, G1, G2, G3a, or G4a approval is absent, the
  feature and catalog remain draft/blocked with implementation disabled.
- **AC-002** - A valid selected endpoint and approved limits produce one
  bounded canonical value through the shared ASR path without blocking Qt.
- **AC-003** - Default selection applies the approved role exactly once, binds
  the endpoint for the session, and does not retarget after a default change.
- **AC-004** - Missing, denied, busy, unsupported, disconnected, overrun,
  timeout, and helper failures have stable typed codes, no silent fallback,
  and no ASR call when capture did not complete.
- **AC-005** - Accepted silence is valid; zero accepted frames is `Empty` and
  ASR invocation count is zero.
- **AC-006** - A limit-crossing packet returns `ResourceLimitExceeded` with no
  truncated success, padding, frame drop, disk spill, or leaked ownership.
- **AC-007** - Start/stop/cancel races use barriers and publish exactly one
  fenced result. No partial transcript or late result is visible.
- **AC-008** - Queue full releases canonical ownership exactly once after
  cleanup, creates no ASR handle, and publishes one `QueueFull` rejection.
- **AC-009** - An unrelated imported-media completion cannot satisfy a blocked
  microphone request's quiescence fence.
- **AC-010** - Approved golden vectors produce identical canonical bytes on
  repeated conversion and meet the approved tolerance on every claimed lane.
- **AC-011** - Helper crash, malformed IPC, timeout, and shutdown invalidate
  the generation, discard partial audio, reap the Job Object, and block new
  work until release is proven.
- **AC-012** - Clean portable/package runs verify the locked helper without
  developer PATH, unverified artifact, or installer assumptions.
- **AC-013** - Repeated native capture shows approved RSS, handle, thread, and
  queue bounds and leaves no raw data in diagnostics.
- **AC-014** - G3b and G4b evidence is required before enablement even after
  pre-implementation approval.
- **AC-015** - A stuck helper/runtime reaches `recovery_owned`, prevents a new
  generation, executes the approved 60-second escalation, and either proves
  release before `Failed(CleanupWarning)` or performs controlled application
  shutdown without claiming a terminal result.

## Test Plan

### Cross-platform behavior tests

- Domain/application state transitions, generation CAS, typed mapping, and
  exactly-once terminal publication.
- Device selection, single-use session lifecycle, deadlines, limits, empty
  versus silence, and no fallback.
- Cancellation races with start, stop, finalization, ASR completion, reset,
  shutdown, and late callbacks.
- Shared FIFO admission, queue-full rollback, request-scoped quiescence,
  blocked runtime, recovery ownership, and imported-media interaction.
- Conversion contract vectors using synthetic inputs, malformed/non-finite
  values, clamp/round boundaries, and no-padding/output-length rules.
- Redaction, no disk spill, helper protocol bounds, and no retained PCM after
  terminal cleanup.

### Windows native and packaging tests

Run exactly 18 named lanes: runtime `R-10-*`, `R-11-23H2-*`, and
`R-11-24H2-*` for Python 3.12/3.13/3.14, plus frozen onedir
`P-10-*`, `P-11-23H2-*`, and `P-11-24H2-*` for the same three Python minors.
Each report names helper/compiler/SDK/converter versions, fixture device,
distribution mode, repetitions, and thresholds.
Exercise enumeration, explicit/default selection, privacy denial, stale token,
busy endpoint, unsupported format, unplug/disconnect, overrun,
0/1/limit/limit+1 boundaries, stop, capture cancellation, ASR cancellation,
helper crash, malformed/replayed/wrong-generation IPC, normal close, and
repeated start/stop/close.

Capture claims require a real named hardware fixture. Conversion reports contain
only vector IDs, hashes, lengths, timings, and bounded metrics. Native reports
contain codes/counts/RSS/handle/thread deltas and no PCM, transcript, endpoint
identity, full path, or raw exception text. `windows-latest` without fixture
definition and every macOS run are insufficient native evidence.

## Open Questions and Deferred Work

The exact choices remain blocking register entries D1-D19. In particular,
product approval must confirm `eConsole`, security must approve named-pipe
challenge/HMAC, G3a must approve the format/resampler matrix and license, and
G4a must approve helper/package/recovery feasibility with `GO`.

Deferred beyond this slice are streaming, disk recordings, waveform
persistence, multi-device mixing, automatic fallback/switching, ARM64 claim,
installer/MSIX, history, hotkeys, text delivery, cloud providers, automatic
downloads, and runtime/model changes.

## Decision Register

| ID | Recommendation (not approved) | Owner | Approval/evidence still required | Status |
|---|---|---|---|---|
| D1 | 32 min / 30,720,000 samples / 61,440,000 canonical bytes | Product owner | product sign-off and memory accounting | RECOMMENDED / BLOCKED |
| D2 | normative cancelling; shell projection remains UI decision | Application owner | UI race review | RECOMMENDED / BLOCKED |
| D3 | separate CaptureError family | Application owner | type hierarchy review | RECOMMENDED / BLOCKED |
| D4 | 2s/5s/10s/45m/5m operation deadlines | Application owner | deadline race matrix | RECOMMENDED / BLOCKED |
| D5 | lock-protected shared reservations and request handles | ASR owner | scheduler race evidence | RECOMMENDED / BLOCKED |
| D6 | recovery_owned blocks new generations; 60s escalation plus 30s watchdog shutdown | ASR owner | recovery supervisor evidence | RECOMMENDED / BLOCKED |
| D7 | eCapture/eConsole once at open | Windows owner | endpoint-role probe | RECOMMENDED / BLOCKED |
| D8 | shared event-driven WASAPI | Windows owner | contention/format probe | RECOMMENDED / BLOCKED |
| D9 | PCM16/float32 mono/stereo at approved rates | Windows owner | capability inventory | RECOMMENDED / BLOCKED |
| D10 | pinned SpeexDSP and exact conversion rules | Audio owner | golden vectors and license | RECOMMENDED / BLOCKED |
| D11 | 4 MiB bounded event queue; overflow failure | Windows owner | stress/RSS evidence | RECOMMENDED / BLOCKED |
| D12 | FFmpeg imported; helper live conversion | Architecture owner | architecture amendment | RECOMMENDED / BLOCKED |
| D13 | C++17 helper, one process per generation | Windows owner | build/crash feasibility | RECOMMENDED / BLOCKED |
| D14 | named pipe with challenge/HMAC and bounds | Security owner | ACL/protocol threat review | RECOMMENDED / BLOCKED |
| D15 | signed x64 portable onedir, sibling helper | Release owner | clean package/provenance | RECOMMENDED / BLOCKED |
| D16 | Job Object kill-on-close and bounded reap | Windows owner | crash/timeout evidence | RECOMMENDED / BLOCKED |
| D17 | bounded queue/scratch/canonical/ASR accounting; helper 64 MiB cap; 192 MiB RSS acceptance ceiling | Performance owner | peak RSS evidence | RECOMMENDED / BLOCKED |
| D18 | 9 runtime plus 9 frozen lanes across Win10/11 x64 and Python 3.12/3.13/3.14; ARM64 deferred | Release owner | runner/fixture record | RECOMMENDED / BLOCKED |
| D19 | filtered/disabled dumps and approved scanner/canaries | Security/privacy owner | dump policy | RECOMMENDED / BLOCKED |

Every decision also has these currently empty approval fields:

| ID | Selected alternative | Evidence path | Evidence SHA-256 | Approval record | Decision date | RFC/spec/catalog commit | Status |
|---|---|---|---|---|---|---|---|
| D1 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D2 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D3 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D4 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D5 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D6 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D7 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D8 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D9 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D10 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D11 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D12 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D13 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D14 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D15 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D16 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D17 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D18 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |
| D19 | NONE | NONE | NONE | NONE | NONE | NONE | RECOMMENDED / BLOCKED |

An approval must replace every `NONE` value before changing any status.
Implementation, enablement, and status `approved` remain forbidden until every
row has the full approval record and is synchronized with the RFC and catalog.
