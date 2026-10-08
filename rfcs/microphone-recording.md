# RFC: Windows Microphone Recording

## Status

Status: Draft

## Summary

Define the Windows microphone boundary for VoiceInk-Win. A process-isolated
Windows-specific WASAPI adapter will capture one user-selected input endpoint,
convert the capture to the existing canonical audio value, and hand one bounded
immutable `CanonicalAudio` value to the existing ASR application service. The feature is
batch transcription after the user stops recording; it is not streaming ASR.

This RFC defines contracts and evidence requirements only. It does not add a
WASAPI binding, a native helper, a PySide6 integration, a new dependency, or
production recording code.

## Context and Existing Constraints

The project constitution requires specifications before implementation, local
processing by default, explicit Windows boundaries, visible failure, bounded
resources, and `make check` before handoff. The baseline dependency direction
is:

```text
Presentation -> Application -> Domain <- Infrastructure adapters
```

The relevant existing contracts are:

- `CanonicalAudio` owns contiguous mono 16 kHz signed PCM16 little-endian
  samples, rejects empty or odd-sized data, and limits the owned payload to
  64 MiB.
- `AsrRequest` accepts `CanonicalAudio`, request metadata, an absolute
  monotonic deadline, and an optional cancellation token. It contains no path,
  device handle, runtime URL, or native type.
- `AsrRuntime` is the existing ASR port. `AsrApplicationService` runs it on a
  bounded worker pool, initially with one active worker, and propagates typed
  timeout and best-effort cancellation failures.
- The current `AsrApplicationService.transcribe()` call is not yet a
  request-scoped quiescence fence: on cancellation its caller may return while
  the runtime worker is still active. A live microphone implementation must
  extend the shared service or its application wrapper before it can release
  microphone PCM after cancellation; the extension is a prerequisite, not a
  second ASR path.
- The imported-media feature uses a 32-minute / 30,720,000-sample limit. Its
  bounded-resource policy, terminal fencing, cleanup, redaction, and retry
  principles are the reference for microphone work.
- The implemented frontend shell currently exposes
  `idle -> recording -> processing -> transcript_ready | empty | error`.
  `ShellController.complete_processing()` is synchronous and its fake backend
  is a demo seam; it is not safe to call live capture or ASR from the Qt GUI
  thread.
- The foundation and Parakeet RFCs explicitly deferred WASAPI recording,
  streaming transcription, global hotkeys, history, and text delivery.
- The repository has no production microphone adapter, no selected WASAPI
  binding, no microphone native smoke workflow, and no audio dependency in the
  base Python package. macOS validation cannot prove Windows audio behavior.

The existing runtime evidence proves a Windows CPU ASR path only. It is not
evidence that microphone capture, Windows privacy settings, device selection,
or native audio format conversion works.

There is one architecture wording conflict to resolve before implementation:
the architecture overview calls FFmpeg the only media conversion boundary,
while this RFC places live-device conversion inside the WASAPI adapter. This
RFC interprets “media conversion” as imported containers/codecs and proposes
that live capture conversion remain inside the audio adapter because no file
or FFmpeg process is involved. The architecture overview must be amended to
make that distinction explicit before this RFC can be approved. The required
wording is: “Imported containers and codecs are normalized only through the
FFmpeg media-conversion boundary. Live device-native frames are converted to
canonical mono 16 kHz S16LE inside the platform audio-input infrastructure
adapter; no native capture format crosses into Application or Domain.”

## Goals

- Define a narrow, platform-independent audio-input port and a Windows WASAPI
  adapter boundary.
- Capture exactly one local input endpoint per recording session.
- Produce the existing `CanonicalAudio` contract without introducing a second
  audio format for ASR.
- Keep WASAPI, COM/native handles, device identifiers, callbacks, and format
  negotiation outside Domain and Application business models.
- Keep the Qt event thread responsive by making capture finalization and ASR
  asynchronous from Presentation.
- Make device policy, limits, cancellation, shutdown, permissions, errors,
  privacy, and cleanup observable and testable.
- Define cross-platform fake-adapter tests and a separate native Windows
  acceptance gate.
- Preserve the existing shell state semantics where possible while adding the
  minimum state required for asynchronous cancellation.

## Non-Goals

- Token-by-token or partial streaming transcription.
- A streaming ASR model, audio visualizer driven by raw samples, or waveform
  persistence.
- System-audio loopback, speaker capture, call recording, or multi-device
  mixing.
- Automatic device switching, automatic fallback to another microphone, or
  silent fallback from a missing selected device to the OS default.
- Global hotkeys, tray lifecycle, text injection, clipboard delivery, history,
  modes, prompts, replacements, cloud transcription, or AI enhancement.
- Automatic downloads, installer changes, signing, or native runtime/model
  packaging.
- Implementation of the native WASAPI helper, its installer packaging, and
  production rollout. The production boundary nevertheless requires capture
  isolation in a dedicated helper process; this RFC specifies the contract and
  acceptance obligations but does not implement that helper.
- Writing recordings to disk as an implementation shortcut.
- Making a macOS fake adapter or a Windows x64 shell launch claim native audio
  compatibility.

## Proposed Boundary

### Layer ownership

**Domain** owns only platform-neutral values and state rules: a device
reference, capture limits, recording identifiers, capture outcome categories,
and the existing canonical audio/ASR values. It must not import Qt, WASAPI,
COM, `ctypes`, `pywin32`, a third-party audio package, native handles, or
Windows error constants.

**Application** owns recording-session lifecycle, admission, state transitions,
deadlines, cancellation fencing, asynchronous orchestration, and mapping
capture/ASR failures to safe user-facing messages. It depends on an abstract
audio-input port and the existing ASR port.

**Infrastructure** owns device enumeration, Windows privacy/access behavior,
WASAPI initialization, format negotiation, native callback/event handling,
resampling/downmixing, bounded buffering, endpoint cleanup, and Windows error
classification.

**Presentation** owns commands, rendering, accessibility labels, and user
feedback. It never enumerates devices, reads samples, calls WASAPI, or calls
`AsrRuntime` directly.

### Audio-input port

The conceptual port is:

```text
AudioInputPort
  list_input_devices() -> immutable device descriptors
  open(device selection, capture limits) -> CaptureSession

CaptureSession
  start() -> accepted or typed capture failure
  request_stop(deadline) -> CanonicalAudio or typed capture failure
  cancel() -> idempotent cancellation request
  close(deadline) -> completed cleanup or typed recovery failure
```

The names are contract names, not an instruction to add these exact Python
classes. The important properties are:

- `AudioInputPort` exposes no WASAPI object, pointer, file path, raw callback,
  or native status code.
- A `CaptureSession` is single-use. It is bound to one device at `open()` and
  cannot be restarted or retargeted after `start()`.
- `request_stop()` is a finalization operation, not a polling read API. It
  waits for the adapter to stop accepting frames, drain its bounded buffer,
  validate continuity, perform conversion, and return one owned
  `CanonicalAudio` value.
- `cancel(deadline)` never returns captured audio. It performs bounded,
  idempotent signalling and causes finalization to return cancellation once
  owned resources are closed.
- `close()` is safe to call repeatedly. The application must not publish a
  session result until capture cleanup has completed or has been represented by
  a typed recovery failure.
- Device enumeration is advisory. A device may disappear between listing and
  `open()`; the adapter must classify that race as a device error, not assume
  that a listed device remains usable.

The port deliberately does not prescribe whether the implementation uses a
Python package, a `ctypes` binding, a C/C++ extension, or a small native
adapter. None is currently selected or known to be available in this
repository. That choice requires a separate feasibility spike and must not
leak into this contract.

The port is intentionally synchronous at its method boundary. A method may
block until its documented deadline, but it may be called only by the
recording orchestration worker, never by Qt or a WASAPI callback. The
application-facing protocol is asynchronous: commands return after admission
or cancellation has been requested, and a later immutable operation result is
published through the application event mechanism. This keeps platform and
threading details out of the domain port without pretending that native
finalization is non-blocking.

The minimum port-level values are:

```text
DeviceSelection = Explicit(opaque device_id) | OperatingSystemDefault
CaptureLimits = {
    max_duration,
    max_canonical_bytes,
    max_canonical_samples,
}

AudioInputPort.list_input_devices(deadline) -> tuple[InputDevice, ...]
AudioInputPort.open(DeviceSelection, CaptureLimits, deadline) -> CaptureSession
CaptureSession.start(deadline) -> None
CaptureSession.request_stop(deadline) -> CanonicalAudio
CaptureSession.cancel(deadline) -> CancelAcknowledgement
CaptureSession.close(deadline) -> None
```

`InputDevice` is immutable and contains an opaque selection ID, display name,
input capability, active capability, and default-role information only. The
port must reject malformed selections and limits before touching native state.
Every `deadline` is an absolute monotonic timestamp. Enumeration, open, start,
finalization, and close are bounded operations; none may wait forever for a
COM call, callback, device, or native client. An adapter must document which
operations are safe concurrently. The required baseline is: `cancel(deadline)`
may race with `start()` or `request_stop()`, `close()` is idempotent and is
called after cancellation, and no other concurrent method call is permitted.
A cancellation acknowledgement means that the adapter accepted the signal;
it is not a cleanup acknowledgement. If the signal cannot be acknowledged by
its deadline, the application reports `CleanupWarning` and does not claim
successful cancellation.

The application also owns a wall-clock `recording_deadline`, independent of
`max_duration`. A device stall with zero delivered frames therefore ends as
`CaptureTimeout` rather than waiting until a sample-count limit can be
reached. For every stage, the effective deadline is the minimum of the
operation deadline, the total recording-operation deadline, and the shutdown
deadline when shutdown is in progress. Cancellation wins only when its fence
is observed before a completion or timeout is committed; cleanup has its own
deadline and always runs after the winning transition.

### Device descriptor and selection

The adapter returns an immutable descriptor containing only:

- an opaque adapter-owned `device_id` suitable for selecting the same endpoint
  during the current installation;
- a display name for the user;
- input/active/default capability flags;
- optional safe capability metadata, if available.

Application code treats `device_id` as opaque. It must not parse it, construct
one, or rely on a particular Windows identifier format. Device names and IDs
must not be written to ordinary logs. If correlation is necessary, diagnostics
may use an ephemeral per-run random token; a persistent device fingerprint or
stable hash is not permitted.

## Capture and Canonical Audio Contract

### Required output

Every successful session returns exactly the existing `CanonicalAudio` value:

| Property | Required value |
|---|---|
| Channels | 1, mono |
| Sample rate | 16,000 Hz |
| Sample encoding | signed 16-bit PCM |
| Byte order | little-endian (`S16LE`) |
| Layout | contiguous samples, no gaps or interleaved metadata |
| Empty payload | forbidden |
| Ownership | immutable application-owned bytes |

The domain boundary contains raw PCM bytes, not a WAV container or a native
audio-buffer object. Each pair of bytes is one little-endian signed 16-bit
sample; `byte_length == sample_count * 2`, and
`duration == sample_count / 16_000`. The first sample is at time zero and no
padding, timestamps, channel markers, resampler metadata, or gap-filling
markers are permitted. A valid all-zero payload is silence, not an empty
capture. The adapter must transfer ownership into one contiguous immutable
allocation before returning; callers must not retain a mutable view of an
adapter buffer.

The ASR request is constructed from this value and must not contain a device
ID, path, native handle, or capture-format object. Microphone and imported
media inputs therefore converge at the same ASR boundary.

### Input-format conversion

The selected endpoint may expose a device-native sample rate, channel count,
sample type, or buffer period. The WASAPI adapter owns conversion to the
canonical format. The application must not receive native frames and must not
invoke FFmpeg for ordinary live capture.

The conversion implementation must provide these invariants:

- non-finite or malformed samples are rejected rather than converted to
  arbitrary PCM;
- conversion is bounded by the capture limits before another buffer is
  accepted;
- channel reduction and sample-rate conversion are deterministic for a given
  adapter/runtime version;
- integer output is explicitly clamped to the signed 16-bit range;
- discontinuity, dropped frames, or an overrun is a typed capture failure, not
  silently hidden by padding or sample duplication.

The exact resampler, channel-mix coefficients, rounding policy, and whether
conversion is performed by a Windows audio facility or a project-owned
library are **UNRESOLVED**. They require a native spike and golden-vector
tests before implementation. This RFC does not assume that any particular
Windows or Python conversion API is available.

### Limits and memory

The microphone path should align its first implementation with the imported
media limits:

- proposed maximum duration: 32 minutes;
- proposed maximum samples: 30,720,000;
- existing hard canonical payload limit: 64 MiB;
- at 16 kHz mono S16LE, the duration limit is the tighter bound for valid
  output.

The 32-minute microphone limit is **UNRESOLVED pending product approval**. It
is the recommended v1 value because it prevents the two local-input paths from
having different ASR admission behavior. Until approved, the implementation
must not invent a second limit or silently rely on the 64 MiB byte limit alone.

The adapter must reject the first frame/block that would exceed a limit and
must not return a truncated successful recording. A recording that contains
only silence is valid audio; an empty capture with no accepted frames is an
`EmptyCapture` failure and must not invoke ASR.

The final canonical allocation is `O(n)` space for `n` samples. The bounded
capture queue is `O(b)` for configured buffered frames `b`; it must not grow
with recording duration. Capture and conversion are `O(n)` in accepted input
frames, excluding any implementation-specific resampler constant. The ASR
runtime remains governed by its existing contract and measurements.

The 64 MiB limit bounds the canonical PCM payload, not total process RSS. The
adapter must additionally bound native-format buffers, queue slots, resampler
scratch, and any transport serialization. It must not retain both multiple
full canonical copies and the adapter queue. Before implementation approval,
the selected adapter must publish a byte budget for each category, prove that
the sum is bounded for the highest supported input format, and measure peak
RSS in the Windows acceptance lane. A transport copy is released as soon as
the ASR request has been handed to the existing service; no copy is retained
for diagnostics or retry.

The proposed v1 audio-path ceilings are:

| Category | Hard ceiling |
|---|---:|
| Canonical PCM payload | 64 MiB |
| Helper/native frames and IPC queue | 16 MiB |
| Conversion/resampler scratch | 16 MiB |
| One ASR transport framing/copy | 65 MiB |
| Capture diagnostics and status buffers | 1 MiB |
| Simultaneous full canonical payloads attributable to microphone | 2 |
| Incremental audio-path RSS over an idle ASR baseline | 192 MiB |

The 192 MiB ceiling is the sum budget with headroom, not permission to allocate
each category lazily without accounting. Admission must reject or stop before
the next buffer could exceed a category ceiling. The acceptance lane must also
record total process RSS as `idle ASR baseline + audio-path delta`; the ASR
model's baseline is not charged to the audio-path budget but must be recorded
for reproducibility. If the selected transport cannot stay within one
65 MiB framing copy, it is incompatible with this RFC and must be changed
before implementation.

## WASAPI Adapter Design

### Native isolation boundary

Production WASAPI capture must run in a dedicated native helper process. An
in-process COM/WASAPI binding cannot contain an access violation, corrupted
native state, or an uninterruptible call, so it cannot satisfy the rollback,
cleanup, or privacy guarantees in this RFC. The Python-side infrastructure
adapter owns the `AudioInputPort`; the helper owns COM initialization, the
WASAPI client, endpoint handles, callbacks, and native conversion state.

The helper is launched in a Windows Job Object with kill-on-close and is
reaped on cancellation timeout, protocol failure, crash, or application
shutdown. Its IPC is local-only, authenticated/ACL-restricted to the current
user, message-framed, and bounded. It accepts only an opaque device selection,
capture limits, and absolute deadlines; it returns bounded canonical PCM
chunks and typed status codes. It must not write audio to disk. Helper crash
or kill invalidates the session generation and produces `CaptureFailed` or
`CleanupWarning`; a fresh helper is required before another session starts.

The helper protocol, executable technology, IPC primitive, and conversion
implementation remain separate feasibility decisions, but selecting an
in-process production binding is not an allowed alternative. The helper is
not a second ASR runtime and does not change the domain port.

### Session lifecycle

The adapter must implement this lifecycle:

```text
created -> opened -> capturing -> stopping -> completed
                         |           |
                         +---------->cancelled -> closed
                         +---------->failed    -> closed
```

Only `opened` may transition to `capturing`. `request_stop()` is idempotent
from `stopping`; after a terminal result no new operation may capture. Every
terminal path closes the native audio client, event/callback registration,
buffer, and any COM/native ownership held by the adapter.

The adapter must bind to the chosen endpoint before capture begins. If the
endpoint is removed, disabled, access-denied, or otherwise unusable, the
session fails with a typed error. It must not switch devices silently.

### Threading and async boundary

The ownership and forbidden-work table is normative:

| Execution context | Owns | Must not do |
|---|---|---|
| Qt GUI thread | User commands and immutable snapshot/event delivery | Call WASAPI, wait for capture/ASR, parse PCM, or call `AsrRuntime` |
| Recording orchestration worker | One session lifecycle, deadlines, cancellation fence, finalization, and handoff to ASR | Touch Qt widgets/listeners or execute native callbacks |
| WASAPI callback/event handler | Adapter-owned frame handoff and a wake-up signal | Block, allocate unbounded storage, call Qt/application/ASR, or publish results |
| Existing ASR worker pool | Canonical `AsrRequest` execution and runtime cleanup | Access a device, native capture handle, or raw callback buffer |

- The Qt GUI thread issues application commands and receives immutable
  snapshots/events only. `start`, `stop`, and `cancel` enqueue commands and
  return without joining a worker.
- A recording orchestration worker owns `CaptureSession` lifecycle and calls
  `request_stop()` and the existing ASR application service away from the GUI
  thread. There is one orchestration worker per active recording, with no
  unbounded worker creation.
- The application command/control lane is separate from the blocking
  orchestration call. A cancel command sets the session cancellation fence and
  invokes the adapter's thread-safe `cancel(deadline)` through a bounded control
  dispatcher, so cancellation can interrupt `request_stop()` without calling
  native code from Qt. The dispatcher is one reusable application-owned lane,
  not one unbounded thread per command.
- Any WASAPI callback or native audio event handler must do bounded, minimal
  work: copy or reference only data whose lifetime is owned by the adapter,
  enqueue it into a bounded buffer, signal the capture worker, and return.
  It must never call Qt, the ASR runtime, or application listeners.
- Native callbacks must not expose Python objects whose lifetime is controlled
  by the callback provider unless the selected binding explicitly guarantees
  that ownership. The binding must marshal data across the native boundary
  into adapter-owned storage.
- The capture worker detects cancellation, deadline expiry, overflow, device
  loss, and shutdown. It then stops the endpoint, drains only frames already
  owned by the adapter, and finalizes or discards the buffer.
- The ASR call begins only after `CanonicalAudio` is complete. No ASR request
  may run on the audio callback thread.
- The application records a session generation/ID. Results from an old
  generation are discarded if cancellation, reset, shutdown, or a newer
  session has won the state transition.

The async bridge must have one terminal-publication owner: the application
orchestration worker performs the compare-and-swap on the session generation
and emits at most one terminal result. A callback, ASR worker, timeout timer,
or cleanup helper may report an event to that worker but may not publish a
shell snapshot directly. Completion callbacks must be detached or fenced
before their worker/session objects are released. Admission and state
validation errors may be returned synchronously; no native `open()` or
`start()` call may be performed synchronously on the GUI command path. An
admitted operation always receives exactly one terminal outcome after cleanup.

The precise event-vs-callback mode, worker count, buffer period, queue data
structure, and native-to-Python marshaling mechanism are **UNRESOLVED**. The
implementation must choose them from APIs actually available on the supported
Windows/Python matrix and document the choice with native evidence.

The capture control path is a blocking implementation concern, not a promise
that `cancel()` can interrupt arbitrary native code. If a selected in-process
binding cannot make `cancel()` thread-safe and bounded, the implementation
must use an isolated helper process or reject that binding; it must not claim
that an in-process kill switch contains a hung COM/native client.

### Backpressure and overflow

The capture path must have a finite buffer and a measurable overflow policy.
It must not block the native audio callback indefinitely and must not drop
frames while claiming a complete recording. If the consumer cannot keep up,
the adapter stops capture, returns `CaptureOverflow`, discards the partial
audio, and emits a bounded diagnostic containing only a code and counts.

No disk spill is allowed in v1. If a future design needs disk spill, it
requires a separate privacy and encrypted-storage decision.

## Device Policy

The proposed v1 policy is:

1. On a new session, enumerate active input-capable endpoints.
2. If the user selected a device, open exactly that opaque device ID.
3. Otherwise, request the operating system's current default input endpoint at
   session open.
4. Bind the session to that endpoint for its entire lifetime.
5. If the endpoint disappears or becomes unusable, fail visibly; do not fall
   back to another endpoint.
6. Do not capture output/loopback devices or combine multiple microphones.

The user-selected device may be persisted as an opaque setting, but the
persistence format and whether the endpoint identifier remains stable across
driver replacement are **UNRESOLVED**. A stale selection must produce a
recoverable `DeviceUnavailable` state and offer a fresh device enumeration.

The Windows default role is also **UNRESOLVED**: the implementation must
decide and document whether “default input” means the system console role, the
communications role, or an explicit product-defined role. The decision must
be tested; it must not be inferred from an unavailable or third-party API.

Shared versus exclusive WASAPI mode is **UNRESOLVED**. Shared mode is the
recommended first candidate because it is less disruptive to other desktop
audio, but the recommendation is not an implementation claim. Exclusive mode
must not be introduced without a user-visible policy for device contention
and format negotiation.

## Application Orchestration and Frontend State

### Composition

The production flow is:

```text
User command
    -> recording application service
    -> AudioInputPort / WASAPI adapter
    -> bounded in-memory finalization
    -> CanonicalAudio
    -> existing AsrApplicationService
    -> existing AsrRuntime port
    -> immutable transcript result
    -> shell snapshot
```

The recording service is the owner of one active microphone session. A second
start while a session is active is rejected as an invalid action, not queued.
Microphone capture must not consume the imported-media queue reservation or
create an imported-media workspace.

The microphone flow must still use the shared existing
`AsrApplicationService`; it must not create a second ASR worker or bypass its
bounded queue. A completed canonical recording must not be retained without a
bound while waiting for ASR admission. The proposed v1 policy is not to
reserve an ASR worker for the duration of capture. After finalization, the
recording service performs one non-blocking `try_admit()` against the shared
FIFO scheduler. Capacity counts pending, active, and reserved requests from
both microphone and imported-media sources; neither source has hidden
priority. A `QueueFull` rejection releases the canonical audio and publishes
no retry. An admitted request retains at most one canonical payload until its
request-scoped quiescence fence completes.

The current service has no `try_admit()` or request-scoped handle, so these
are explicit prerequisites for the live path. The required conceptual
contract is:

```text
AsrApplicationService.try_admit(request) -> AsrRequestHandle | QueueFull
AsrRequestHandle.cancel(deadline) -> None
AsrRequestHandle.await_result(deadline) -> TranscriptResult | AsrError
AsrRequestHandle.await_quiescence(deadline) -> None | RuntimeRecoveryPending
```

Quiescence means that this identified request is absent from the pending queue
and active-worker set, no runtime or transport operation can consume or
publish its result, and the shared service retains no reference to its
`CanonicalAudio`. Global `interrupt_active()` and an unscoped `wait_idle()` do
not satisfy this contract. The service must retain FIFO fairness across
sources and never use an imported-media request becoming idle as the fence for
the microphone request.

The service must replace the current synchronous demo-only completion seam for
the live path with an asynchronous application protocol. Presentation may
request start, stop, or cancel and subscribe to immutable results; it must not
wait on a worker using a blocking Qt call. The exact Future/callback/signal
mechanism is **UNRESOLVED** and must be selected without making Domain depend
on Qt.

### State transitions

The current states remain the semantic outcomes for successful transcription:

```text
idle -> recording -> processing -> transcript_ready
                         |              |
                         +------------> empty
                         |              |
                         +------------> error
```

The live implementation must add a transient cancellation state to the
application/presentation contract, because the current state machine has no
safe way to represent asynchronous capture and cleanup:

```text
recording  -> cancelling -> idle
processing -> cancelling -> idle
```

`cancelling` disables start/stop controls as appropriate, tells the user that
audio is being discarded, and remains visible until capture and ASR cleanup
have fenced the session. A successful cancellation publishes no transcript and
no partial audio. A cleanup failure publishes `error` with a safe recovery
message instead of silently returning to `idle`.

Whether this is represented as a new `ShellState.CANCELLING` value or as a
separate recording-operation status carried by `ShellSnapshot` is
**UNRESOLVED**. The implementation must choose one representation and update
the shell feature specification before production code. It must not overload
`processing` to hide cancellation.

Start failure before capture begins maps to `error` and leaves no active
session. Stop transitions to `processing` before finalization and ASR. A
non-empty `TranscriptResult.text` maps to `transcript_ready`; whitespace-only
text maps to `empty`, consistent with the existing shell behavior. Capture,
permission, device, limit, timeout, ASR, and cleanup failures map to visible
`error` messages through a stable error-code mapping.

The service must use an atomic session-generation/state transition so a late
ASR result cannot overwrite cancellation, reset, shutdown, or a newer
recording. The existing ASR service's best-effort runtime interruption policy
remains authoritative: cancel the request, discard late output, and clean up;
hard interruption inside native inference is not promised. The new
request-scoped handle is nevertheless required to await quiescence before
audio release or terminal publication; it must not weaken the existing
runtime's cancellation semantics.

## Cancellation, Deadlines, and Shutdown

- `cancel` from `recording` signals `cancel(deadline)`, stops capture, discards
  the bounded buffer, and closes
  the session, and never invokes ASR.
- `cancel` during finalization or ASR requests both capture/ASR cancellation,
  waits for the owned stage to fence its result, discards late output, and
  publishes cancellation only after cleanup.
- A stop deadline or maximum recording duration is absolute for the operation;
  it is not extended by a slow device or a blocked ASR runtime.
- Cancellation wins against a not-yet-published success when the application
  transition fence observes cancellation first. A result already published is
  not rewritten.
- App shutdown rejects new starts, requests cancellation for the active
  session, closes the capture adapter, and then closes the ASR application
  service according to its existing deadline/recovery policy.
- A cleanup timeout becomes a typed recovery failure and is logged only with
  safe codes/counts. The process must not claim that a native resource is
  closed when the adapter cannot prove it.

The current `AsrApplicationService.transcribe()` may return
`CancellationError` while its ASR worker is still inside the runtime. That
existing behavior is not an adequate microphone cleanup fence: the recording
service must not release the last audio reference, publish terminal
cancellation, or accept a new microphone generation until the specific ASR
request has reached quiescence. Before live capture is enabled, the shared ASR
service must therefore expose a request-scoped completion/fence contract (or
an equivalent bounded supervisor operation) that can cancel one request and
await its worker's terminal cleanup. Global `interrupt_active()` and an
unscoped `wait_idle()` are insufficient when imported-media work can coexist.
This RFC requires that contract to remain in the shared service; a second ASR
runtime or microphone-specific worker pool is forbidden.

The proposed v1 deadline configuration is explicit and must be passed as
absolute monotonic timestamps, never recomputed after a stage starts:

| Budget | Proposed value and rule |
|---|---|
| Device enumeration/open/start | 10 seconds per operation; expiry is `CaptureTimeout` before capture |
| Maximum accepted capture | 32 minutes and 30,720,000 canonical samples, whichever is reached first |
| Wall-clock capture deadline | Capture start plus 32 minutes; a device stall is `CaptureTimeout` |
| Finalization after stop | 60 seconds, bounded by the remaining processing budget |
| ASR stage | 30 minutes, passed explicitly instead of the current 30-second default |
| Post-stop processing deadline | 45 minutes, matching imported-media processing policy |
| Cleanup | 5 minutes from cleanup entry, matching imported-media cleanup policy |

The 32-minute maximum still requires product approval; until approved, the
feature cannot leave Draft or claim a shippable limit. The remaining proposed
budgets are implementation defaults and must be validated against measured
ASR RTFx before native enablement. The configured maximum audio duration must
satisfy `duration / minimum_accepted_RTFx + fixed_overhead <= ASR stage
budget`; otherwise the duration or deadline must be changed before approval.
The non-blocking post-finalization `try_admit()` policy above is normative:
`QueueFull` is visible, releases PCM, and is never retried implicitly.

## Rollout and Rollback

This feature must be introduced independently of imported-media transcription;
the existing ASR port and queue remain the only transcription path. The
recommended rollout is:

1. **Contract stage:** land the domain/application contracts, fake adapter,
   state machine, race tests, and diagnostics-redaction tests. The live
   microphone capability remains unavailable.
2. **Native validation stage:** land the selected adapter behind an explicitly
   disabled capability, run the named Windows acceptance lane on supported
   hardware, and retain the evidence artifact without raw audio or transcript
   content.
3. **Opt-in stage:** enable capture only for an internal/diagnostic cohort.
   Record local, opt-in counts and bounded timings by stable error code, adapter
   version, device capability class, and distribution mode. No remote flag or
   telemetry transport is introduced by this RFC. Do not log endpoint names,
   IDs, PCM, or transcript text.
4. **General-availability stage:** enable the capability only after the
   acceptance gate, leak/repeatability checks, and release checklist thresholds
   have passed for every supported Windows/distribution lane.

The release must provide a kill switch that disables new microphone starts
without disabling imported-media transcription or the existing ASR runtime.
When the switch is activated, an active session is cancelled, native cleanup
is awaited up to its deadline, and no partial audio or transcript is
published. A cleanup timeout remains a visible recovery error; the process
must not claim a successful rollback while a native handle is still owned.

Rollback is mandatory for a native crash, leaked handle/worker, privacy or
redaction failure, incorrect device retargeting, repeated canonical-format
violations, or an acceptance regression. The rollback action is, in order:

- disable the microphone capability for new sessions;
- preserve safe diagnostics needed to identify the adapter build;
- cancel and fence active operations;
- ship the last accepted adapter/application artifact, or revert to the
  previous release with microphone disabled;
- re-run the native smoke/acceptance gate before re-enabling the capability.

No rollback may silently switch to another microphone, persist raw captured
audio, route audio to a cloud service, or bypass the existing ASR boundary.
Because this RFC adds no schema or on-disk recording format, rollback must not
require a data migration. The exact feature-flag owner, release thresholds,
and artifact promotion mechanism are release-engineering decisions and are
blocking unresolved decisions for production enablement, not excuses to
enable an unvalidated adapter.

## Permissions and Error Contract

The adapter must classify at least these conditions without exposing raw
Windows errors across the port:

| Stable condition | User-visible result | ASR invoked |
|---|---|---:|
| `PermissionDenied` | Enable microphone access in Windows privacy settings | No |
| `DeviceUnavailable` | Select another input device or reconnect it | No |
| `DeviceBusy` | Close the other application using the microphone and retry | No |
| `UnsupportedFormat` | The selected device cannot provide a supported capture format | No |
| `CaptureOverflow` | Recording was stopped because the capture consumer fell behind | No |
| `DeviceDisconnected` | The input device was disconnected during recording | No |
| `EmptyCapture` | No audio frames were captured; try again | No |
| `ResourceLimitExceeded` | Recording exceeded the configured duration/size limit | No |
| `CaptureTimeout` | Recording did not finish within its deadline | No |
| `CaptureCancelled` | Recording was cancelled | No |
| `CaptureFailed` | Microphone capture failed; retry or choose another device | No |
| `AsrUnavailable` / existing ASR codes | Local transcription is unavailable or failed | After valid audio |
| `CleanupWarning` | VoiceInk could not finish local audio cleanup; restart before retrying | No new request |

These are conceptual stable codes. Their final placement in the existing ASR
error hierarchy or a separate capture-error hierarchy is **UNRESOLVED**. The
application must retain the distinction between permission, device, capture,
resource, cancellation, ASR, and cleanup failures. It must never turn an
exception into an empty successful transcript.

The adapter must use the normal Windows user permission model. It must not
request elevation, modify privacy settings, disable security controls, or
pretend that a desktop packaging mode has permissions it has not verified.
The exact microphone capability/manifest behavior for the future packaged and
portable distributions is **UNRESOLVED**. Permission behavior must be tested
for each supported distribution mode rather than assumed from the current
PyInstaller shell artifact.

## Security and Privacy

- Capture starts only after an explicit application command. Background
  capture, hidden listeners, and device monitoring that records audio are out
  of scope.
- Audio remains in memory and is passed only to the local ASR port. No cloud
  or non-loopback network request is permitted by this RFC.
- Raw PCM, native audio buffers, transcript text, device IDs, endpoint names,
  permission details, and full exception strings are excluded from ordinary
  logs, telemetry, crash reports, and native smoke output.
- Diagnostics may include stable error codes, bounded durations, sample/frame
  counts, backend version, and an ephemeral operation token. They must not be
  sufficient to reconstruct or persistently identify the recording or device.
- The final `CanonicalAudio` allocation is released after the ASR request and
  is not retained by the shell, fake adapter, or diagnostics layer. Python
  memory cannot be reliably zeroed; the design therefore minimizes retention
  but does not make a false secure-erasure claim.
- Callback buffers, resampler scratch, transport bodies, and exception locals
  must release their audio references on every terminal path. Capture-related
  crash reports and minidumps must be disabled or configured to exclude process
  memory and sensitive exception attachments; the acceptance harness must
  verify that no diagnostic artifact contains PCM or transcript text.
- Native resources must be closed on success, failure, cancellation, device
  loss, process shutdown, and callback exceptions. No raw audio is written to
  a temporary file as a fallback.
- If a third-party/native capture dependency is selected, it must be pinned,
  provenance-recorded, license-reviewed, and included in the same artifact
  security review as the existing native runtime. This RFC does not assume
  that dependency exists.

## Test Plan

### Cross-platform tests

All tests below must run without Windows APIs, PySide6, CUDA, model weights,
or a real microphone:

- Fake port contract tests for device listing, explicit selection, default
  selection, unavailable/busy/permission failures, single-use sessions, and
  idempotent close/cancel. They must also cover deadline expiry for listing,
  open, start, finalization, and cleanup, plus cancellation racing each
  blocking operation.
- Application state tests for start, stop, empty capture, valid silence,
  canonical audio, typed capture errors, ASR errors, cleanup errors, invalid
  actions, and retry-after-terminal-state behavior.
- Race tests using barriers/events, not sleeps, for cancel-vs-stop,
  cancel-vs-ASR-complete, reset-vs-late-result, shutdown-vs-start, and device
  loss during finalization. Each test asserts exactly one published outcome.
- Tests that capture failure, empty capture, limit overflow, cancellation, and
  cleanup failure never invoke the ASR fake and never publish partial text.
- Tests for bounded buffer behavior, max-duration/byte fencing, no truncation,
  no silent frame dropping, no disk spill, and the complete native/queue/
  conversion/transport byte budget.
- ASR integration tests that pass one `CanonicalAudio` through the existing
  `AsrApplicationService` and fake runtime, preserving deadline and cancellation
  behavior. A blocked fake runtime must prove that request cancellation does
  not publish or release audio until the request-scoped ASR fence reports
  quiescence; an unrelated imported-media request must not satisfy that fence.
- Redaction tests proving that errors and diagnostics contain no PCM, transcript,
  full device ID/name, path, raw native error string, or secret.
- Presentation contract tests proving that the GUI thread only receives
  immutable snapshots/events and that the production seam is asynchronous;
  cancellation while finalization is blocked must still reach the adapter
  through the control dispatcher; the existing synchronous fake shell tests
  remain valid as demo tests.

### Windows adapter tests

These tests require a supported Windows environment and must be separate from
macOS CI:

- Enumerate input devices and validate that a selected endpoint opens, starts,
  stops, and closes repeatedly without leaked handles or worker threads.
- Capture a controlled non-silent signal and verify canonical sample rate,
  channel count, byte order, sample alignment, duration bounds, and expected
  conversion behavior with approved golden vectors.
- Exercise the actual default-device policy and explicit-device policy.
- Deny microphone access and verify `PermissionDenied` plus a safe UI message.
- Remove/disable the selected endpoint during capture and verify
  `DeviceDisconnected` without fallback or ASR invocation.
- Force consumer backpressure/overflow and verify bounded failure and cleanup.
- Verify cancellation latency, wall-clock stall timeout, stop finalization,
  shutdown during capture, no late result publication, and peak RSS against the
  declared byte budget. Repeat the open/capture/stop/close cycle at least 100
  times and assert no increasing handle, thread, helper, or RSS baseline.
- Configure randomized PCM and transcript canaries for a helper-crash run;
  enumerate application-controlled dump/report/temp locations and byte-scan
  produced artifacts for raw, framed, and canonical canaries.
- Run with the supported portable and packaged distribution modes once those
  modes exist; do not infer permission equivalence between them.

### Native Windows acceptance gate

Native acceptance is an explicit evidence gate and cannot be satisfied by the
current macOS workstation or by the existing ASR smoke alone. The first gate
must record, without raw audio or transcript content:

1. Windows version/build, architecture, Python/runtime/package versions, and
   application commit.
2. The selected capture binding and its version/provenance.
3. Device capability metadata in redacted form and the selected policy (role,
   shared/exclusive mode, conversion path).
4. A successful short recording from a real input endpoint, with canonical
   metadata, non-zero accepted frame count, bounded duration, and a successful
   local ASR result when the ASR runtime is configured.
5. At least one cancellation run and one repeated start/stop run with no
   leaked worker/native resources.
6. Permission-denied and unavailable-device evidence, either automated or
   documented manual steps, with safe error codes.
7. Evidence that changing or removing the default device does not retarget an
   active session.
8. Evidence that capture-related logs, crash reports, minidumps, and report
   attachments contain neither PCM nor transcript text.

The acceptance harness owner, controlled signal/fixture, exact supported
Windows versions/builds, x64/ARM64 lanes, packaging identities, golden-vector
tolerances, and resource/cancellation thresholds are **UNRESOLVED**. Each
lane must be named before approval; a passing emulated run must not be
reported as native ARM64 evidence. The evidence artifact must include the
commit, adapter provenance, fixture hash, lane name, result codes, timings,
peak RSS, and leak counters, but never the signal, PCM, or transcript.
The authoritative lane is a named owner plus runner label, Windows build,
architecture, package identity, and capture-binding version. `windows-latest`,
file ingestion, emulation, or a synthetic fake adapter is not microphone
evidence. The checked-in workflow/runbook must define fixture setup/reset,
permission setup/reset, golden-vector tolerances, and pass/fail thresholds
from a clean machine before this Draft can become Approved.

## Acceptance Criteria

- Given a valid selected input endpoint, when the user starts and stops a
  session, then the application produces one bounded `CanonicalAudio` value
  and submits it through the existing ASR port without blocking the Qt thread.
- Given no selected endpoint, when a session starts, then the documented OS
  default policy is applied once and the session remains bound to that endpoint.
- Given a selected endpoint is unavailable, permission is denied, or the
  endpoint disconnects, then a stable typed error and actionable safe UI state
  are published, no fallback occurs, and ASR is not invoked.
- Given silence was captured, then the audio remains valid canonical input and
  an empty ASR result maps to the existing `empty` state; zero captured frames
  maps to `EmptyCapture` without ASR.
- Given a buffer, duration, or byte limit would be exceeded, then capture
  terminates without returning truncated audio, reports
  `ResourceLimitExceeded`, and releases all native resources.
- Given cancellation races with finalization or ASR, then exactly one fenced
  outcome is published, late output is discarded, and no partial transcript is
  visible.
- Given application shutdown, then capture is cancelled and cleaned up before
  the application claims normal shutdown; pending cleanup is visible as a
  typed recovery failure.
- Given any ordinary log or diagnostic report, then it contains no raw audio,
  transcript text, full paths, device identifiers/names, secrets, or raw native
  exception strings.
- Cross-platform fake-adapter tests pass without Windows, PySide6, a real
  microphone, native binaries, or model weights.
- A named Windows native acceptance lane passes the capture, conversion,
  permission/error, cancellation, cleanup, and repeatability checks above.

## Unresolved Decisions

The following decisions are intentionally open and block implementation claims:

1. Exact WASAPI binding or native adapter technology and its supported Python
   versions.
2. Event-driven versus callback capture, native-to-Python marshaling, buffer
   period, queue capacity, and worker ownership.
3. Exact sample-rate converter, channel downmix coefficients, rounding, and
   golden-vector source.
4. Approval of the 32-minute microphone limit and its relationship to the
   existing 64 MiB canonical limit.
5. Windows default input role: console, communications, or a product-defined
   role.
6. Shared versus exclusive WASAPI mode.
7. Persistence format and stability policy for opaque device selection IDs.
8. Final capture error hierarchy and its relationship to existing `AsrError`.
9. Future packaged/portable microphone capability and privacy-manifest rules.
10. Async Presentation/Application mechanism and the representation of the
    proposed `cancelling` state.
11. Stop/finalization and total recording deadlines.
12. Native acceptance hardware, controlled signal, supported Windows lanes,
    and the acceptance harness.
13. Amendment of the architecture overview to reconcile FFmpeg's imported-
    media boundary with live WASAPI conversion.
14. Request-scoped ASR cancellation/quiescence API and its bounded cleanup
    behavior when imported-media work is concurrent.
15. Validation of the proposed total capture-memory ceilings against the
    selected helper, highest supported input format, and ASR transport.
16. Helper executable technology, bounded IPC primitive, authentication/ACL
    setup, and restart-required recovery implementation.
17. Crash-report/minidump configuration and whether any local diagnostics are
    retained, for how long, and under which user consent.
18. Local feature-flag owner, rollout thresholds, and artifact promotion/
    rollback mechanism.

No implementation may resolve these by depending on an API, package, native
helper, Windows role, or packaging behavior that has not been verified on the
target matrix. Decisions must be recorded in an RFC amendment and the affected
feature specification before production code is added.

## Exit Criteria

This RFC may move from Draft only after the unresolved choices needed for the
first implementation are approved, the microphone feature specification is
updated, the selected adapter is available on the target matrix, fake and
Windows tests are defined, and the native acceptance lane has an owner and a
reproducible evidence format. Completion requires implementation, behavior
tests, native Windows evidence, safe diagnostics, and a passing `make check`.
