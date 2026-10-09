# RFC: First Windows Microphone Capture Slice

## Status

Status: Draft - implementation blocked.

This RFC is documentation and evidence planning only. It authorizes no
production microphone code, WASAPI binding, native helper, IPC transport,
packaging change, fake adapter, or ASR service change. Implementation remains
blocked until D1-D19 and the pre-implementation portions of G1, G2, G3a, and
G4a have written approvals. G4a must have outcome `GO`. G3b and G4b remain
post-implementation enablement gates.

The values marked `RECOMMENDED` are concrete engineering recommendations for
review. They are not approvals, evidence, or permission to implement.

## Summary

The first Windows slice will support one explicit local microphone recording
per operation:

1. enumerate active capture endpoints;
2. select one opaque selection token, or resolve the approved Windows default role
   once at open;
3. start and stop one single-use capture session;
4. enforce a bounded 32-minute capture and bounded memory;
5. convert in the Windows adapter to one immutable `CanonicalAudio` value;
6. submit that value to the existing shared ASR application service; and
7. publish one typed terminal outcome after capture, ASR, and cleanup fences.

This is batch transcription, not streaming ASR. Native frames, COM objects,
WASAPI handles, endpoint IDs, and Windows status values never cross into Domain
or Application. The application receives only typed device descriptors,
canonical PCM16 chunks during internal assembly, and the final immutable
`CanonicalAudio` value.

The proposed production data flow is:

```text
Presentation
    -> Recording application service
    -> AudioInputPort
    -> Windows Python adapter
    -> authenticated named pipe
    -> C++17 WASAPI helper
    -> CanonicalAudio assembly
    -> shared AsrApplicationService
    -> typed terminal outcome
```

The design keeps the existing D1-D19 and G1-G4 discipline. A recommendation
does not close a gate. The catalog and feature specification must retain
`implementation_allowed: false` until the approval records and decision
synchronization are complete.

## Context and References

### Existing Windows contracts

- `CanonicalAudio` is immutable, contiguous mono 16 kHz signed PCM16
  little-endian data with a 64 MiB payload ceiling.
- `AsrRequest` accepts canonical audio, metadata, an absolute monotonic
  deadline, and optional cancellation. It has no path, device handle, native
  type, or runtime URL.
- Imported media already has bounded admission, FIFO scheduling, typed results,
  cleanup, and a 32-minute/64 MiB canonical limit. Microphone work must use the
  same ASR capacity and must not create a second pool.
- The current ASR implementation exposes synchronous `transcribe()` and
  service-wide `wait_idle()`/`interrupt_active()` behavior. G2 must add a
  request-scoped handle and quiescence fence before microphone integration.
- The desktop shell is not evidence of microphone support. A fake-backed shell
  must not become a production fallback.

### Mac reference reconciliation

The Mac implementation was inspected at:

- `VoiceInk/Infrastructure/Audio/CoreAudioRecorder.swift`
- `VoiceInk/Infrastructure/Audio/PCMAudioConverter.swift`
- `VoiceInk/Infrastructure/Audio/Devices/AudioDeviceManager.swift`
- `VoiceInk/Features/Recording/Capture/Recorder.swift`

The shared behavior worth preserving is device enumeration, explicit device
binding, off-main-thread hardware work, conversion to mono 16 kHz PCM16, and
typed "no usable microphone" presentation. The Windows slice intentionally
does not copy these Mac behaviors:

| Mac behavior | Windows v1 decision |
|---|---|
| Core Audio AUHAL callback and ring slots | Native WASAPI event-driven capture in a helper process |
| Prepared AudioUnit and device switch during a recording | Single-use session bound to one endpoint; no retargeting |
| Custom/prioritized/default selection with fallback | Explicit selection or one `eConsole` default resolution; no silent fallback |
| ExtAudioFile/WAV and streaming chunk callback | No recording file and no ASR streaming; bounded canonical chunks only |
| Float32 callback conversion, including linear interpolation | Pinned native converter with approved matrix and golden vectors |
| macOS microphone authorization and lid-specific fallback | Windows privacy/access classification; no registry/settings mutation |
| In-process Core Audio object lifetime | Process-isolated helper with Job Object recovery |

The Mac code is compatibility and product-behavior reference evidence. It is
not Windows evidence and does not approve any Windows gate.

### External technical references

The recommendation uses the Windows contracts documented by Microsoft:

- [About WASAPI](https://learn.microsoft.com/en-us/windows/win32/coreaudio/wasapi)
- [`IMMDeviceEnumerator::GetDefaultAudioEndpoint`](https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-immdeviceenumerator-getdefaultaudioendpoint)
- [`IAudioClient::Initialize`](https://learn.microsoft.com/en-us/windows/win32/api/audioclient/nf-audioclient-iaudioclient-initialize)
- [Capturing a stream](https://learn.microsoft.com/en-us/windows/win32/coreaudio/capturing-a-stream)
- [Recovering from an invalid-device error](https://learn.microsoft.com/en-us/windows/win32/coreaudio/recovering-from-an-invalid-device-error)

These references establish the API shape and known invalidation/error cases;
they are not a substitute for the named Windows evidence in G3b/G4b.

## Goals

- Define one implementation-ready Windows audio-input boundary.
- Enumerate and explicitly select active local input endpoints.
- Support start, stop, cancel, bounded capture, and one session per operation.
- Produce exactly mono 16 kHz S16LE `CanonicalAudio` or a typed failure.
- Submit microphone audio through the existing shared ASR scheduler only.
- Make deadlines, cancellation, quiescence, cleanup, device loss, and
  permission failures observable and testable.
- Recommend a concrete native technology, IPC contract, package layout, and
  evidence matrix before implementation.
- Preserve privacy: no raw PCM, transcript, device identity, or full paths in
  ordinary diagnostics or evidence artifacts.

## Non-Goals

- Streaming or partial transcription, waveform persistence, or live waveform
  data from raw frames.
- Loopback/system audio, call recording, mixing, multi-device capture, or
  automatic device switching.
- Global hotkeys, history, text injection, cloud transcription, enhancement,
  automatic downloads, installer rollout, or model changes.
- Writing a recording to disk or using disk spill as a memory workaround.
- Implementing any code in this RFC.
- Claiming that a macOS test, emulated Windows run, or `windows-latest` run
  without an audio fixture is microphone evidence.

## Proposed Architecture

### Layer ownership

```text
Presentation -> Application -> Domain <- Infrastructure
```

**Domain** owns immutable device descriptors, capture limits, session IDs,
capture error codes, terminal outcome values, canonical audio, and state rules.
It imports no Qt, COM, WASAPI, `ctypes`, `pywin32`, native handles, third-party
audio type, or Windows error constant.

**Application** owns admission, one active recording, generation fencing,
deadlines, command/state transitions, ASR admission, request-scoped
cancellation, quiescence, and terminal publication. It calls ports only.

**Infrastructure** owns enumeration, Windows privacy/access classification,
the helper process, COM/WASAPI, format conversion, bounded queues, named-pipe
framing, Job Object cleanup, package verification, and Win32 error mapping.

**Presentation** owns commands, accessible labels, and immutable snapshots. It
never enumerates devices, waits for capture/ASR, touches native handles, or
calls the runtime directly.

### Recommended capture technology (D13/D8: not approved)

Use a small C++17 helper built with MSVC and the Windows SDK. It should use:

- `IMMDeviceEnumerator` and `IMMDeviceCollection` for endpoint discovery;
- `IMMDevice::Activate(IID_IAudioClient)` for a capture client;
- `IAudioClient` in `AUDCLNT_SHAREMODE_SHARED` with
  `AUDCLNT_STREAMFLAGS_EVENTCALLBACK`;
- `IAudioCaptureClient` to read packets from the capture endpoint buffer;
- `IMMNotificationClient` for device invalidation notifications; and
- COM ownership and all native cleanup inside the helper.

Shared event-driven mode is recommended because it avoids taking exclusive
ownership from other applications, accepts the Windows audio-engine mix format,
and provides a bounded event wait. Exclusive mode is rejected for v1: it has
more user-controlled failure cases, may be disabled per endpoint, and offers
no product value for batch dictation.

The helper is one process per capture generation, not a long-lived audio
daemon. Its capture thread owns the `IAudioClient` and `IAudioCaptureClient`.
The control/IPC thread sends commands to that owner through a bounded internal
queue. The owner thread is initialized with COM before any Core Audio call.
The implementation must not perform COM or WASAPI calls on the Qt thread or in
the Python adapter.

Rejected alternatives for review are direct Python `ctypes`/COM, `pywin32`
WASAPI bindings, PortAudio, `sounddevice`, FFmpeg, Media Foundation capture,
and exclusive WASAPI. They either widen the Python/native ABI, add an
uncontrolled callback/runtime dependency, conflate file conversion with live
capture, or weaken the required crash containment. D13 remains blocked until
the comparison and feasibility record are approved.

### Recommended Python/native boundary (D13/D14: not approved)

Python owns `AudioInputPort`, `CaptureSession`, generation IDs, monotonic
deadlines, typed result mapping, and the final `CanonicalAudio` allocation.
The helper owns COM, endpoint handles, WASAPI packets, native conversion
state, and native cleanup. Python must not import a Windows audio API.

The recommended transport is a per-generation Windows named pipe:

- the parent creates the pipe server before launching the helper, requests
  `FILE_FLAG_FIRST_PIPE_INSTANCE`, sets `PIPE_REJECT_REMOTE_CLIENTS`, and
  applies a DACL limited to the current user SID;
- the parent uses a random 128-bit instance name and passes the server handle
  plus a 32-byte bootstrap challenge through an inherited handle, never in
  command-line text or logs;
- the first `HELLO` must prove protocol version, generation, and challenge;
- each subsequent frame carries the generation, sequence number, deadline, and
  an HMAC-SHA-256 tag made with a per-generation session key;
- frame payload is capped at 64 KiB, canonical audio chunks are capped at
  32 KiB, and the aggregate IPC queue is capped at 4 MiB;
- malformed, replayed, oversized, out-of-order, wrong-generation, or
  wrong-version messages are rejected before payload allocation; and
- pipe close, helper exit, authentication failure, or deadline expiry
  invalidates the generation and discards all partial audio.

The parent is the named-pipe client and the helper is the server: the parent
creates exactly one server object, passes its inherited server handle to the
helper, and then opens the random name as the client. The helper never creates
or races another process to create the pipe server. The parent verifies that
the connected server PID equals the child PID returned by `CreateProcess`,
then verifies the helper HMAC proof and one-time challenge before sending
`OPEN`. There is no reconnect path. The session key
is derived with HKDF-SHA-256 from the challenge and generation, used for
direction-specific HMACs, and best-effort zeroed by both sides at close.

The wire frame is fixed for protocol version 1:

```text
magic[4] = "VIKA"
version:u16, type:u8, flags:u8
generation:u64, sequence:u64, deadline_ns:u64, payload_length:u32
payload[payload_length], hmac_sha256[32]
```

The HMAC covers the header and payload. `HELLO`, `OPEN`, `START`, `AUDIO`,
`STOP`, `CANCEL`, `STATUS`, `RESULT`, and `CLOSE` are the only message types.
Sequence numbers are strictly increasing per direction; replay, wrong
direction, wrong generation, wrong version, malformed, and oversized messages
fail closed before payload allocation.

The helper sends already converted mono 16 kHz S16LE chunks. These chunks are
an internal bounded transport representation, not streaming ASR and not a
public Domain value. Python appends them to one pre-budgeted buffer and creates
`CanonicalAudio` only after `STOP` reports a complete conversion.

The feasibility review must compare this named pipe with loopback TCP plus a
one-time bearer handshake, shared memory plus control pipe, and a no-go result.
The named-pipe recommendation is not a G4a approval.

### Device and default-role policy (D7: not approved)

Enumeration is a point-in-time snapshot of `eCapture` endpoints in
`DEVICE_STATE_ACTIVE`. Each descriptor contains:

```text
InputDevice {
    selection_token: SelectionToken
    display_name: SafeDisplayName
    is_default_console: bool
    channels: 1 | 2
    sample_rates: tuple[int, ...]
    sample_types: tuple[PCM16 | Float32, ...]
}
```

The selection token is a random, non-derivable infrastructure token, not the
`IMMDevice::GetId` value. The Windows adapter keeps a process-local
`selection_token -> native_device_id` map and resolves the native ID only at
`open()`. Domain/Application may hold and return the token, but may not parse,
synthesize, persist, log, or compare native ID internals. Tokens expire when
the enumeration snapshot is replaced or the process exits. Names are bounded
for display and are redacted from diagnostics.

The recommendation is `eCapture + eConsole` for the default. It matches a
general dictation application rather than the communications role. The
application resolves that role once during `open()` and binds the returned
endpoint for the entire session. An explicit selection resolves exactly its
selection token.
If the selected endpoint is missing, disabled, busy, inaccessible, or
unsupported, the session fails; it never falls back to the default or another
endpoint. A default-device change during capture cannot retarget the session.

Unlike the Mac reference, v1 has no prioritized-device list, lid-specific
fallback, device switch, or "best available" policy. The UI may offer a fresh
selection after a terminal failure, but the active operation is never repaired
by changing devices.

### Native format and conversion policy (D9/D10/D11: not approved)

The helper opens shared mode using the endpoint's `GetMixFormat()` and accepts
only this proposed matrix:

| Field | Proposed v1 value |
|---|---|
| Subformat | `KSDATAFORMAT_SUBTYPE_PCM` or `KSDATAFORMAT_SUBTYPE_IEEE_FLOAT` |
| Sample type | PCM 16-bit packed little-endian, or IEEE float 32-bit packed |
| Channels | 1 or 2, interleaved |
| Channel mask | mono, or front-left/front-right stereo |
| Sample rates | 16000, 32000, 44100, 48000, or 96000 Hz |
| Output | mono, 16000 Hz, signed PCM16 little-endian |

Any other subtype, valid-bits layout, channel count/mask, or rate produces
`UnsupportedFormat` before ASR. The helper must not silently ask the device for
another format or use FFmpeg.

The proposed converter is a pinned static SpeexDSP resampler wrapper, with the
exact source/version recorded in the G3a evidence. Conversion rules are:

1. reject malformed packet sizes, invalid block alignment, and non-finite
   float samples;
2. map PCM16 to float64 with `s < 0 ? s / 32768 : s / 32767`, then downmix
   stereo before resampling using `0.5 * left + 0.5 * right`;
3. resample with initial source phase zero and cumulative target count
   `floor(total_input_frames * 16000 / input_rate)`; an end-of-stream flush
   may emit only samples whose source position is below `total_input_frames`;
4. map finite float output with `x >= 0 ? round(x * 32767) : round(x * 32768)`,
   using round-half-away-from-zero, then clamp to `[-32768, 32767]`;
5. serialize each integer as little-endian S16; and
6. flush only on a normal stop. Any converter output count other than the
   cumulative target is a conversion failure, not hidden truncation, padding,
   repetition, timestamp metadata, or silent frame drop.

PCM16 and float32 inputs use the same decode-to-float64, channel-reduction,
resampling, and final-quantization pipeline; only the input decoder differs.
An overrun, packet gap, later discontinuity, or converter failure is a typed
failure, not silence insertion. Golden vectors define the exact resulting
bytes, including the mono/no-resample case.

G3a must approve the exact SpeexDSP version, coefficient behavior, input tuple
matrix, output-length rule, golden-vector tolerance, and ownership of the
converter. The recommendation is deliberately not a compatibility claim.

For WASAPI packet handling, `AUDCLNT_BUFFERFLAGS_SILENT` is valid silence for
the reported frame count and is converted to zero samples; it is not a dropped
packet. `AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY` is tolerated only on the
first accepted packet, where no preceding continuity exists, and fails the
session on every later packet. `AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR`, a
non-empty packet with invalid alignment, or any failed `ReleaseBuffer` is a
typed capture failure. The capture loop calls `GetBuffer` and
`ReleaseBuffer` exactly once per packet, drains until `AUDCLNT_S_BUFFER_EMPTY`
on stop, and treats zero accepted frames as `EmptyCapture`.

### Limits and memory accounting (D1/D11/D17: not approved)

The recommended first-slice limits are:

| Resource | Recommended limit |
|---|---:|
| Capture duration | 32 minutes |
| Canonical samples | 30,720,000 |
| Canonical PCM payload | 61,440,000 bytes, also bounded by existing 64 MiB ceiling |
| Native capture queue | 4 MiB total |
| One IPC queue | 4 MiB total |
| IPC frame | 64 KiB |
| Canonical chunk | 32 KiB |
| Conversion scratch | 16 MiB |
| Simultaneous microphone payloads | 1; ASR queue accounting is shared and approved separately |
| Helper process memory | 64 MiB Job Object hard limit, subject to native probe |
| Audio-path RSS delta | 192 MiB acceptance ceiling; runtime hard cap is not claimed |

The next input packet that would cross duration, sample, queue, or byte limits
fails with `ResourceLimitExceeded` and discards the session. It must not
return truncated success, silently drop frames, or spill to disk. The Python
assembly buffer is allocated only after validating the announced limit and may
grow in fixed bounded increments up to the canonical payload limit.

The accounting must include native packet buffers, converter scratch, IPC
copies, Python canonical assembly, ASR framing copies, helper overhead, and
any queued/recovery-owned ASR requests. The helper Job Object enforces its
process cap; Python rejects any allocation that would exceed the pre-budgeted
canonical/transport caps; the operation fails closed with
`ResourceLimitExceeded` if a pre-budgeted allocation would exceed its cap. A
native evidence run that measures process-tree RSS above 192 MiB fails the
lane and keeps enablement blocked; this is an acceptance ceiling, not a claim
that Windows can enforce one RSS cap across Python, ASR, and helper. The
numbers above are proposals requiring G3a/G4a approval, not permission to
allocate each category independently. Capture/conversion are `O(n)` in
accepted input samples with `O(b)` queue space for configured bound `b`; final
canonical storage is `O(n)`.

### Deadlines, cancellation, and quiescence (D2/D4/D5/D6: not approved)

The recommended absolute monotonic deadlines are:

| Operation | Deadline |
|---|---:|
| Device enumeration | 2 seconds |
| Helper launch, handshake, and open | 5 seconds |
| Stop, drain, and final conversion | 10 seconds |
| ASR request after admission | 45 minutes |
| Cleanup and process reap | 5 minutes |

Capture itself is bounded by the 32-minute limit. Every port call receives an
absolute deadline; each nested call receives the minimum of its own deadline
and the parent operation deadline. No event wait, pipe read, COM call, or
cleanup call may wait forever.

The application state machine is:

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

`cancel()` is idempotent signalling. It acknowledges only that the signal was
accepted, not that resources are closed. During capture, the helper stops
accepting frames, drains/discards its bounded queue, closes the endpoint, and
the application publishes `Cancelled` only after the helper is quiescent and
reaped. During ASR, the application cancels the specific request handle and
waits for request-scoped quiescence. An unrelated imported-media request
becoming idle cannot satisfy this fence.

The shared ASR ownership sequence is:

```text
reserved -> queued -> active -> quiescent -> released
                           \-> recovery_owned
```

`recovery_owned` is not quiescence and does not permit releasing audio,
publishing cancellation, or accepting a new microphone generation. A named
supervisor must prove all audio, ASR, helper, and native ownership released;
otherwise `RuntimeRecoveryPending` remains visible and the microphone lane is
blocked. The recommended ASR reservation point is immediately after
finalization, before transferring the canonical reference to the shared
scheduler. Capture does not hold an ASR slot. FIFO order is the order of
successful `try_admit()` linearization across microphone and imported-media
requests; `QueueFull` is returned at stop with no lease or implicit retry.
There is no reservation expiry because the canonical value is transferred or
released in the same transaction. G2 must approve this transaction and the
recovery supervisor.

### Permissions and device loss

The helper maps Windows access and endpoint failures to stable codes without
including HRESULT text in user-visible diagnostics:

| Condition | Stable outcome |
|---|---|
| Windows microphone privacy/access denial | `PermissionDenied` |
| No active input or stale explicit ID | `DeviceUnavailable` |
| Exclusive holder or open contention | `DeviceBusy` |
| Unsupported tuple or converter input | `UnsupportedFormat` |
| `AUDCLNT_E_DEVICE_INVALIDATED`, removal, disable, or unplug | `DeviceDisconnected` |
| Capture packet/queue overrun | `CaptureOverflow` |
| Any operation deadline | `CaptureTimeout` or `AsrTimeout` |
| User cancellation | `CaptureCancelled` / `Cancelled` |
| Unexpected native/helper failure | `CaptureFailed` |
| Cleanup not proven by deadline | `CleanupWarning` / `RuntimeRecoveryPending` |

The application does not change Windows privacy settings, registry keys, device
defaults, or endpoint state. On denial it reports an actionable instruction to
enable desktop microphone access in Windows privacy settings. On device loss
it discards partial audio and requires a new explicit/default open; it never
rebinds the active session.

### Packaging and recovery recommendation (D13/D15/D16/D18/D19: not approved)

The first implementation should claim only Windows 10 22H2 and Windows 11
23H2/24H2, x64, Python 3.12-3.14, and a portable onedir bundle. The evidence
matrix is exactly 18 lanes: nine runtime lanes (`R-{10,11-23H2,11-24H2}-
{312,313,314}`) and nine frozen onedir lanes with the same OS/Python pairs
(`P-{10,11-23H2,11-24H2}-{312,313,314}`). ARM64 is not claimed until it has a
native helper build and its own G3b/G4b evidence. An installer/MSIX is
deferred; a CI artifact is not a release claim.

The bundle should contain:

```text
voiceink-shell.exe
audio/voiceink-audio-helper.exe
audio/voiceink-audio-helper.manifest.json
README.txt
```

The helper path is resolved relative to the verified bundle root, never PATH.
The adjacent manifest is metadata only and is not its own trust root. The
expected helper SHA-256, publisher certificate thumbprint, and manifest
schema/version are in the read-only artifact lock embedded in the frozen
Python bundle and reviewed in source. Launch opens the package helper
no-follow, records its volume/file identity, hashes the opened bytes, checks
the locked digest, and calls `WinVerifyTrust` with the pinned publisher policy.
The verified bytes are copied into a per-generation private directory whose
DACL denies modify/delete, the destination is reopened no-follow and checked
against the same digest, and the helper is started suspended from that
destination. Before resume, the parent compares the child image path and
volume/file identity with the verified destination and rechecks the digest;
any mismatch terminates the Job Object before audio starts. The helper and
lock are never accepted merely because a mutable adjacent manifest says they
match. The release helper must be
Authenticode-signed; unsigned developer builds are not enablement evidence.
The existing artifact-lock style is the reference for verification and must be
extended rather than bypassed.

The helper runs in a Windows Job Object with kill-on-close and a one-process
active-process limit. On normal close, cancellation, malformed IPC, timeout,
crash, or shutdown, the parent closes the pipe, waits for the process, and
reaps it. If the cleanup deadline expires, the generation enters
`recovery_owned`; the supervisor sends `CANCEL`, closes the pipe, calls
`TerminateJobObject` once, and waits an additional 60 seconds for process and
ASR ownership proof. If proof succeeds, it publishes `Failed(CleanupWarning)`.
If proof does not succeed, the process-level recovery action is controlled
application shutdown with a separate 30-second watchdog deadline. The
watchdog owns the helper/ASR child jobs, terminates those jobs and the
application process if the shutdown deadline expires, and records no terminal
recording result. The next launch keeps microphone composition disabled until
the sanitized recovery record is reviewed. A kill switch disables microphone
composition for the remainder of the process. A helper crash never triggers an
in-process capture fallback or automatic endpoint fallback.

D19 must define whether dumps are disabled or filtered, exact dump locations,
retention/consent, scanner version, canaries, negative controls, and the
security approver. Ordinary logs and evidence may contain only stable codes,
bounded durations/counts, adapter/helper versions, and an ephemeral operation
token. They must not contain PCM, transcript text, endpoint ID/name, paths,
secrets, or raw native exception strings.

## Concrete Port and Outcome Contract

The following is an implementation target, not permission to create these
types while the RFC is blocked:

```text
AudioInputPort.list_input_devices(deadline)
    -> tuple[InputDevice, ...] | CaptureError

AudioInputPort.open(selection: ExplicitDevice | DefaultConsole,
                   limits: CaptureLimits,
                   deadline)
    -> CaptureSession | CaptureError

CaptureSession.start(deadline) -> None | CaptureError
CaptureSession.request_stop(deadline) -> CanonicalAudio | EmptyCapture | CaptureError
CaptureSession.cancel(deadline) -> CancelAcknowledgement | CaptureError
CaptureSession.close(deadline) -> None | CleanupError

AsrApplicationService.try_admit(request)
    -> AsrRequestHandle | QueueFullError
AsrRequestHandle.cancel(deadline) -> None | AsrError
AsrRequestHandle.await_result(deadline) -> TranscriptResult | AsrError
AsrRequestHandle.await_quiescence(deadline) -> None | RuntimeRecoveryPending
```

The public recording result is a discriminated union:

```text
Succeeded(transcript)
Empty
Failed(code, safe_message)
Cancelled
RejectedRequest(code=QueueFull | InvalidSelection | InvalidLimits)
```

`RejectedRequest(QueueFull)` is a stage rejection after capture cleanup and
creates no ASR handle. An admitted recording operation publishes exactly one
terminal outcome. A successful all-zero buffer is valid silence; zero accepted
frames is `Empty` and does not invoke ASR. Exceptions never become empty
success.

## Gated Work Plan

| Gate | Scope | Exit artifact | Current status |
|---|---|---|---|
| G1 | recording contract, state machine, outcomes, ownership, limits | approved contract, decision record, race matrix | BLOCKED |
| G2 | shared ASR admission, request handle, quiescence, recovery | approved scheduler contract and race evidence | BLOCKED |
| G3a | Windows endpoint, WASAPI, format, converter, limits | approved policy matrix and golden-vector plan | BLOCKED |
| G3b | implemented native endpoint/conversion behavior | Windows native evidence | NOT STARTED |
| G4a | helper, IPC, auth, package, recovery feasibility | approved feasibility `GO` record | BLOCKED |
| G4b | implemented helper, crash, security, packaging validation | clean-machine/package evidence | NOT STARTED |

Dependencies are strict: G1 precedes G2 and G4a; G2 and G4a precede G3a;
G3a precedes G3b and G4b. D1-D19 must be approved before production code.
No gate may be closed by a passing macOS test or by the existence of this
recommendation.

## Implementation Slices After Approval

These slices are the recommended implementation order. They are not authorized
until the stated gates are approved.

### Slice 0: Approval and contract synchronization

- Approve D1-D19 and G1/G2/G3a/G4a with immutable/versioned records.
- Copy the selected values, rejected alternatives, evidence hashes, and record
  references into the RFC, feature spec, and catalog metadata.
- Amend `spec/architecture/overview.md` if D12 is approved.
- Keep the feature disabled until G3b/G4b are complete.

### Slice 1: Domain and application recording contract

- Add immutable device/selection, limits, session, error, and terminal outcome
  values under `domain`.
- Add a recording orchestration service under `application` with one active
  generation, terminal CAS, absolute deadlines, and no Qt dependency.
- Add the request-scoped ASR admission/quiescence interface to the shared
  service, preserving imported-media FIFO capacity.
- Add cross-platform behavior tests using injected ports only.

### Slice 2: Native helper and Python adapter

- Build the C++17 helper as a separate artifact with a versioned protocol.
- Implement active endpoint enumeration, explicit/default open, shared event
  capture, bounded queues, and stable native error mapping.
- Keep native frames inside the helper; send only canonical chunks.
- Implement Job Object ownership and generation invalidation before integration.

### Slice 3: Conversion and limits

- Implement the approved format matrix and pinned converter.
- Add deterministic synthetic golden vectors for mono/stereo, all approved
  rates/types, full-scale/clamp boundaries, non-finite rejection, output
  length, and repeatability.
- Prove no truncation, padding, silent drop, or disk spill at every boundary.

### Slice 4: Shared ASR integration

- Admit canonical audio through the shared scheduler only.
- Test queue full, FIFO fairness with imported media, request cancellation,
  blocked runtime, request-scoped quiescence, late-result fencing, cleanup,
  and recovery ownership.
- Do not add a microphone worker pool or a second runtime.

### Slice 5: Presentation and package composition

- Add device selection and recording commands as asynchronous presentation
  events after the domain/application contract exists.
- Map stable errors to actionable privacy/device/format messages without
  displaying native strings or endpoint IDs.
- Package the verified helper beside the shell and validate the artifact lock.

### Slice 6: Native evidence and enablement

- Run the named G3b and G4b Windows lanes, including clean-machine package
  validation, crash recovery, permission denial, device loss, cancellation,
  and memory/handle/thread repetition tests.
- Enable microphone capture only after every required acceptance criterion is
  linked to sanitized evidence.

## Test and Evidence Plan

### Cross-platform tests

Before Windows APIs are involved, tests must cover:

- selection validation and one active generation;
- legal and illegal state transitions;
- absolute deadline arithmetic and limit boundaries;
- zero-frame empty versus valid silence;
- cancellation races with start, stop, finalization, ASR completion, reset,
  and shutdown;
- typed error mapping and exactly-once terminal publication;
- queue-full ownership rollback and no retained PCM;
- shared FIFO admission and request-scoped quiescence;
- late-result fencing and cleanup/recovery ownership; and
- log/evidence redaction and no disk spill.

These tests use fake ports only after G1/G2 approval and do not claim Windows
capture evidence.

### Windows native evidence

The evidence owner must name the runner, Windows build, architecture, Python
version, helper/compiler/SDK/converter versions, package mode, fixture device,
repetition count, thresholds, and retention policy. The minimum recommended
matrix is:

| Lane | Target | Mode |
|---|---|---|
| R-10-312 | Windows 10 22H2 x64, Python 3.12 | runtime, controlled USB microphone |
| R-10-313 | Windows 10 22H2 x64, Python 3.13 | runtime, controlled USB microphone |
| R-10-314 | Windows 10 22H2 x64, Python 3.14 | runtime, controlled USB microphone |
| R-11-23H2-312 | Windows 11 23H2 x64, Python 3.12 | runtime, controlled USB microphone |
| R-11-23H2-313 | Windows 11 23H2 x64, Python 3.13 | runtime, controlled USB microphone |
| R-11-23H2-314 | Windows 11 23H2 x64, Python 3.14 | runtime, controlled USB microphone |
| R-11-24H2-312 | Windows 11 24H2 x64, Python 3.12 | runtime, controlled USB microphone |
| R-11-24H2-313 | Windows 11 24H2 x64, Python 3.13 | runtime, controlled USB microphone |
| R-11-24H2-314 | Windows 11 24H2 x64, Python 3.14 | runtime, controlled USB microphone |
| P-10-312 | Windows 10 22H2 x64, Python 3.12 | frozen onedir, clean package |
| P-10-313 | Windows 10 22H2 x64, Python 3.13 | frozen onedir, clean package |
| P-10-314 | Windows 10 22H2 x64, Python 3.14 | frozen onedir, clean package |
| P-11-23H2-312 | Windows 11 23H2 x64, Python 3.12 | frozen onedir, clean package |
| P-11-23H2-313 | Windows 11 23H2 x64, Python 3.13 | frozen onedir, clean package |
| P-11-23H2-314 | Windows 11 23H2 x64, Python 3.14 | frozen onedir, clean package |
| P-11-24H2-312 | Windows 11 24H2 x64, Python 3.12 | frozen onedir, clean package |
| P-11-24H2-313 | Windows 11 24H2 x64, Python 3.13 | frozen onedir, clean package |
| P-11-24H2-314 | Windows 11 24H2 x64, Python 3.14 | frozen onedir, clean package |

The native plan must exercise enumeration, explicit selection, default-role
selection, default change without retargeting, privacy denial, missing/stale
ID, busy endpoint, unsupported format, unplug/disconnect, overrun, 0/1/limit/
limit+1 frames, stop, capture cancellation, ASR cancellation, helper crash,
malformed IPC, replay/wrong generation, normal close, and repeated open/start/
stop/close. A real device or a named hardware fixture is required for capture
claims; a synthetic adapter is only application contract evidence.

Golden conversion reports contain vector IDs, hashes, output lengths, bounded
error metrics, versions, and thresholds, never raw PCM. Runtime reports contain
codes, counts, timings, RSS, handle/thread deltas, and operation tokens, never
PCM, transcript, endpoint identity, full paths, or raw native errors.

G3b requires identical canonical bytes for repeated conversion of the same
golden vector. G4b requires helper crash containment, ACL/authentication,
bounded framing, Job Object cleanup, artifact verification, and clean-machine
portable/package evidence. `windows-latest` without the fixture definition is
not sufficient.

## Acceptance Criteria

- **RFC-AC-001** - The RFC, feature spec, and catalog retain draft/blocked
  status and `implementation_allowed: false` while any D1-D19, G1, G2, G3a,
  or G4a approval is missing.
- **RFC-AC-002** - Every D1-D19 row names one recommendation, rejected
  alternatives, owner, approver, required evidence, approval record, and
  remains visibly `RECOMMENDED / APPROVAL REQUIRED` until signed.
- **RFC-AC-003** - The selected technology keeps COM/WASAPI/native handles out
  of Python Domain/Application and sends no native frames across the boundary.
- **RFC-AC-004** - A valid selected/default endpoint can produce exactly one
  bounded canonical value and submits it only through the shared ASR service;
  Qt never blocks.
- **RFC-AC-005** - Missing, denied, busy, unsupported, disconnected, overrun,
  deadline, and cleanup conditions map to stable typed outcomes, invoke ASR
  zero times when capture did not complete, and never become empty success.
- **RFC-AC-006** - Valid silence with accepted frames remains valid canonical
  audio; zero accepted frames is `Empty` and invokes ASR zero times.
- **RFC-AC-007** - The next frame crossing any approved limit fails without
  truncated success, padding, silent drop, disk spill, or leaked ownership.
- **RFC-AC-008** - Cancellation races publish exactly one fenced outcome only
  after capture/ASR request-scoped quiescence and cleanup; late results cannot
  overwrite a newer generation.
- **RFC-AC-009** - Queue-full admission releases canonical ownership exactly
  once after capture cleanup, creates no ASR handle, and publishes one visible
  stage rejection without retry.
- **RFC-AC-010** - Conversion golden vectors are deterministic and match the
  approved matrix, output-length rule, and tolerance on each claimed Windows
  lane.
- **RFC-AC-011** - A helper crash, malformed protocol, or kill-on-close path
  invalidates the generation, discards partial audio, reaps the Job Object, and
  blocks new work until resource release is proven.
- **RFC-AC-012** - Every claimed package locates and verifies the locked helper
  without PATH or unsigned-artifact assumptions on a clean machine.
- **RFC-AC-013** - Native tests demonstrate no uncontrolled growth in RSS,
  handles, threads, capture queues, or IPC queues across the approved repeats.
- **RFC-AC-014** - Sanitized logs, crash reports, dumps, and evidence contain
  no PCM, transcript, endpoint identity, full path, secret, or raw native
  exception string.
- **RFC-AC-015** - G3b and G4b remain enablement gates even after all
  pre-implementation decisions are approved.
- **RFC-AC-016** - A stuck-helper/runtime recovery test reaches
  `recovery_owned`, prevents a new generation, executes the approved 60-second
  escalation, and either proves release before `Failed(CleanupWarning)` or
  performs controlled application shutdown without claiming a terminal result.

## Decision Register

All rows are recommendations only. `Selected alternative`, `Evidence`, and
`Approval record` must be replaced by signed, versioned values before status
can become `APPROVED`. Until then every status is `RECOMMENDED / BLOCKED` and
every evidence/hash field is intentionally `NONE`.

| ID | Decision | Recommended alternative (not approved) | Rejected alternatives to evaluate | Owner | Approver | Evidence required | Status | Gate |
|---|---|---|---|---|---|---|---|---|
| D1 | Duration/sample/byte limit | 32 min; 30,720,000 samples; 61,440,000 canonical bytes; existing 64 MiB ceiling | shorter/longer product value; byte-only limit | Product owner | Product owner + maintainer | boundary accounting and product sign-off | RECOMMENDED / BLOCKED | G1 |
| D2 | Shell cancellation projection | Keep normative `cancelling`; expose as shell operation status until UI review | hide it; map to processing/idle | Application owner | Maintainer + UI owner | snapshot/race review | RECOMMENDED / BLOCKED | G1 |
| D3 | Error hierarchy | Separate `CaptureError` family mapped into typed recording `Failed` | extend `AsrError`; generic exception | Application owner | Maintainer | type/error mapping review | RECOMMENDED / BLOCKED | G1 |
| D4 | Deadlines | enumerate 2s; open 5s; finalize 10s; ASR 45m; cleanup 5m | unbounded; one global timeout | Application owner | Product owner + maintainer | timing/race matrix | RECOMMENDED / BLOCKED | G1/G2 |
| D5 | Scheduler transaction | lock-protected reservation/handle state machine; reserve only at post-finalization `try_admit()` | second pool; capture-time reservation; global idle; unscoped interrupt | ASR owner | Maintainer | shared scheduler race tests | RECOMMENDED / BLOCKED | G2 |
| D6 | Recovery | per-request supervisor; `recovery_owned` blocks new generation; 60s escalation plus 30s watchdog shutdown | release on signal; immediate new generation | ASR owner | Maintainer + security owner | blocked-runtime recovery probe | RECOMMENDED / BLOCKED | G2 |
| D7 | Default role | `eCapture` + `eConsole`, resolved once at open | communications; auto best device | Windows owner | Product owner + maintainer | role/device probe | RECOMMENDED / BLOCKED | G3a |
| D8 | WASAPI mode | shared, event-driven, no loopback | exclusive; polling; FFmpeg | Windows owner | Product owner + maintainer | contention/format probe | RECOMMENDED / BLOCKED | G3a |
| D9 | Input matrix | PCM16/float32; mono/stereo; 16/32/44.1/48/96 kHz; approved masks | arbitrary formats; silent OS fallback | Windows owner | Maintainer | mix-format inventory | RECOMMENDED / BLOCKED | G3a |
| D10 | Conversion | pinned static SpeexDSP wrapper; 0.5/0.5 downmix; floor output length; exact clamp/round policy | Media Foundation; FFmpeg; ad hoc linear conversion | Audio owner | Maintainer + Windows owner | vectors, tolerance, license/version | RECOMMENDED / BLOCKED | G3a |
| D11 | Capture buffering | 4 MiB native queue; event-driven; overflow is failure | unbounded queue; drop-and-continue | Windows owner | Maintainer | stress and RSS report | RECOMMENDED / BLOCKED | G3a |
| D12 | Conversion boundary | imported media through FFmpeg; live frames in Windows adapter/helper | FFmpeg for live input; Python conversion | Architecture owner | Maintainer | architecture amendment | RECOMMENDED / BLOCKED | G3a |
| D13 | Helper technology | C++17 MSVC/Windows SDK helper, one process per generation | in-process Python; PortAudio; Media Foundation capture | Windows owner | Maintainer | build/ABI/crash feasibility | RECOMMENDED / BLOCKED | G4a |
| D14 | IPC | current-user named pipe; challenge + HMAC; 64 KiB frame/4 MiB queue bounds | loopback TCP; shared memory; no-go | Security owner | Maintainer + Windows owner | auth/ACL/protocol threat review | RECOMMENDED / BLOCKED | G4a |
| D15 | Package target | signed x64 portable onedir with sibling locked helper; ARM64 and installer deferred | PATH helper; one unverified executable; early ARM64 claim | Release owner | Maintainer + security owner | clean bundle and provenance plan | RECOMMENDED / BLOCKED | G4a |
| D16 | Job/recovery | kill-on-close Job Object, one active process, bounded reap, generation invalidation | orphan helper; in-process fallback | Windows owner | Maintainer + security owner | crash/timeout/reap probe | RECOMMENDED / BLOCKED | G4a |
| D17 | Audio memory budget | 4 MiB native + 4 MiB IPC + 16 MiB scratch + bounded canonical/ASR copies; helper 64 MiB hard cap; 192 MiB process-tree acceptance ceiling | independent unbounded categories; runtime RSS enforcement claim | Performance owner | Maintainer | peak RSS/ownership accounting | RECOMMENDED / BLOCKED | G3a/G4a |
| D18 | Native evidence matrix | 9 runtime lanes plus 9 frozen onedir lanes: each Win10/11 x64 and Python 3.12/3.13/3.14 combination | unqualified `windows-latest`; ARM64 without evidence | Release owner | Maintainer | runner/fixture/version/threshold record | RECOMMENDED / BLOCKED | G4a |
| D19 | Dump/privacy policy | filtered or disabled dumps; approved locations/retention; canaries and scanner before G4a | raw dumps; post-hoc byte scan only | Security/privacy owner | Maintainer + security owner | policy, scanner, negative controls | RECOMMENDED / BLOCKED | G4a |

The full approval record must additionally contain selected alternative,
rejected alternatives, approver, decision date, evidence path, evidence
SHA-256, approval-record path, and the RFC/spec/catalog commit. No current row
contains those values because no decision is approved.

The approval metadata is part of each row, not free-form review history:

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

An approver must replace every `NONE` value before changing any status. The
catalog may not change `implementation_allowed` or `g4a_outcome` based on a
prose recommendation.

## Exit Criteria

This RFC may change from Draft only when:

1. D1-D19 have signed, versioned approvals and no conflicting open question;
2. G1, G2, G3a, and G4a have pre-implementation artifacts, with G4a=`GO`;
3. the selected values are identical in this RFC, the feature spec, and the
   catalog decision metadata;
4. any approved D12 architecture wording is committed in the architecture
   overview; and
5. a separate implementation plan names code slices, test owners, native
   lanes, rollback owner, and kill-switch owner.

G3b native evidence and G4b helper/package validation are required before
microphone enablement. Closing the pre-implementation gates does not claim
that a device, converter, helper, or package works.

## Rollout and Rollback

### Rollout

1. Land the contract/application and shared-ASR changes behind a disabled
   capability; imported-media behavior remains unchanged.
2. Land the helper and adapter only in a Windows development/acceptance lane.
3. Run cross-platform tests, then G3b/G4b native and clean-machine evidence.
4. Enable the feature only when the catalog and evidence records allow it.
5. Keep the helper artifact hash-locked and the portable package reproducible.

### Rollback

If capture loses frames, leaks ownership, violates privacy, blocks cleanup,
misclassifies permission/device loss, or fails any G3b/G4b criterion:

- disable microphone capability at composition/configuration level;
- preserve imported-media transcription and the unavailable-state UI;
- do not fall back to Python/in-process capture, another endpoint, fake ASR,
  or silent retry;
- stop and reap helper generations under the approved Job Object recovery;
- retain only sanitized failure evidence; and
- revert the microphone implementation/package slice as one logical unit after
  preserving the failing report.

There is no data migration and no recording file to clean up. A rollback must
not alter user device defaults or Windows privacy settings.

## Open Questions

The following are approval questions, not implementation instructions:

- Does product approval accept `eConsole` rather than `eCommunications` for
  dictation default selection?
- Does the exact approved device matrix need 8 kHz or multichannel support, or
  is rejecting those formats acceptable for v1?
- Is pinned SpeexDSP acceptable for licensing, deterministic output, and
  package maintenance, or does G3a select another converter?
- Does the security review accept current-user named-pipe ACL plus challenge/
  HMAC, or require a different local transport?
- What is the approved cleanup recovery owner and process-level recovery budget?
- Which signed release artifact and installer policy will follow the x64
  portable first slice?
- Which concrete dump policy and Windows hardware fixture satisfy D19/D18?

Until these questions are resolved in the decision register, the only allowed
work is documentation, review, and attaching external evidence. Production
microphone code, helper prototypes, packaging changes, fake adapters, and ASR
service changes remain forbidden.
