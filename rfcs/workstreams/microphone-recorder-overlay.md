# RFC: Windows Microphone Access, Recording, and Persistent Recorder Overlay

## Status

Status: Proposed; implementation blocked by the native helper gate.

This RFC is design-only. It authorizes no Python capture fallback, fake
production provider, WASAPI binding, native helper, packaging change, or
composition enablement. The existing microphone RFC remains authoritative for
the D1-D19, G1, G2, G3a, G3b, G4a, and G4b gates.

## Decision summary

The implementation will add a presentation-facing controller around the
existing `MicrophoneRecordingService`. It will not create a second capture
service and it will not make `ShellController` perform synchronous recording.

The controller will:

1. request microphone access by invoking the existing service's real
   `AudioInputPort.open()` path on an application worker, rather than treating
   `MicrophoneStatus` as permission proof;
2. start and stop the existing `MicrophoneRecordingService` and retain its
   request-scoped cancellation and ASR quiescence behavior;
3. publish immutable recorder snapshots to the Qt presentation layer;
4. forward only a bounded normalized input-level value to the overlay; and
5. keep an independent, small, rounded, always-on-top top-level window alive
   after the main window is hidden.

The default composition remains truthful: without a verified Windows provider,
the microphone is unavailable. A macOS test, an injected provider, or a
Windows CI job without a real audio fixture cannot change that claim.

## Evidence read and current seam

This design is based on the following current repository contracts:

- `rfcs/microphone-recording.md`: native capture is blocked; the proposed
  helper is a separate C++17 process; the shared ASR scheduler and recovery
  fences are mandatory.
- `rfcs/workstreams/windows-microphone-recording.md`: the platform-independent
  `AudioInputPort`/`AudioCaptureSession` slice exists, but the default adapter
  must remain unavailable.
- `rfcs/workstreams/windows-microphone-capture-implementation.md`:
  `WindowsAudioInputAdapter` maps provider failures and currently has no
  native provider, permission request, or level stream.
- `src/voiceink_win/application/microphone_service.py`:
  `MicrophoneRecordingService.start()` opens one session, starts a worker,
  supports `stop()`/`cancel()`, finalizes bounded PCM, and submits through the
  shared ASR service.
- `src/voiceink_win/domain/ports.py` and `audio_capture.py`: native details
  stop at `AudioInputPort`; chunks crossing the boundary are bounded
  canonical PCM16; status is a capability/readiness value, not access proof.
- `src/voiceink_win/presentation/main_window.py`: the current
  `FloatingRecorderWindow` is 300 x 92, frameless, rounded by the shared
  stylesheet, and currently animates the waveform for every recording state.
  It is parented to `MainWindow` and does not set `WindowStaysOnTopHint`.
- `src/voiceink_win/desktop_composition.py` and `composition.py`: the desktop
  shell starts with an unavailable `ShellController`; a configured backend
  creates `MicrophoneRecordingService(WindowsAudioInputAdapter(), asr)`, but
  that service is not yet wired to the recorder UI.
- Existing presentation tests prove offscreen Qt rendering and unavailable
  copy. They do not prove Windows window-manager behavior, microphone access,
  WASAPI, or native capture.

## Goals

- Request/attempt microphone access at the user start action on Windows where
  the selected Windows application model exposes a consent API; otherwise make
  the actual WASAPI open attempt the authoritative access check.
- Start, stop, and cancel through the current
  `MicrophoneRecordingService`, without blocking the Qt event thread.
- Keep one active recording generation and one terminal outcome.
- Keep the overlay visible while the main window is hidden, including during
  recording, stopping, processing, cancellation, and a visible error.
- Animate waveform bars only while a recent normalized input level is above a
  defined sound threshold. Silence must not look like captured sound.
- Present unavailable, denied, disconnected, busy, timeout, cleanup, and
  recovery states without inventing success or exposing native diagnostics.
- Make all behavior except actual Windows access/native window-manager behavior
  testable on macOS with fakes and offscreen Qt.

## Non-goals

- No streaming transcription, partial transcript, recording file, waveform
  persistence, raw-frame telemetry, or device auto-switching.
- No change to Windows privacy settings, registry, device defaults, or endpoint
  state.
- No Python `ctypes`, `pywin32`, PortAudio, `sounddevice`, FFmpeg, or fake
  microphone provider in production.
- No claim that the current macOS host can capture audio through the Windows
  adapter, and no claim that x64 emulation is native ARM64 support.
- No system tray, global shortcut, text injection, or main-window redesign.

## Architecture and ownership

```text
Qt presentation
    -> MicrophoneRecorderController (application)
    -> MicrophoneRecordingService (existing application service)
    -> AudioInputPort (domain-owned port)
    -> WindowsAudioInputAdapter (infrastructure)
    -> verified Windows provider/helper (native gate)
    -> shared AsrApplicationService
```

The new controller is a presentation adapter/use case, not a capture engine.
It owns the recorder snapshot, command serialization, pending-start handling,
level hysteresis, and mapping of `RecordingResult` to safe UI text. It does not
own PCM, ASR workers, native handles, endpoint IDs, HRESULTs, or Qt objects.

`MicrophoneRecordingService` remains the only application service that opens a
capture session, accumulates bounded PCM16, invokes shared ASR admission, and
waits for request-scoped quiescence. The controller must never call an ASR
runtime directly.

`WindowsAudioInputAdapter` remains the only production Python infrastructure
adapter. The future native provider owns COM, WASAPI, endpoint identity,
permission/access classification, packet conversion, helper lifecycle, and
native cleanup. Only safe domain values, bounded PCM16 chunks, stable errors,
and normalized level values cross the boundary.

### Access request semantics

`MicrophoneStatus` is advisory and must not be used as proof that access was
granted. The user start command enters `REQUESTING_ACCESS` and invokes the
service's `AudioInputPort.open()` path on an application worker. The provider's
`open()` contract is extended as follows:

1. On a packaged Windows application model where a documented Windows consent
   request is applicable, the provider performs that request before opening the
   endpoint and waits for its result.
2. On the current classic desktop/portable Win32 path, no generic programmatic
   privacy-setting mutation or consent dialog is assumed. The provider performs
   the real bounded endpoint access attempt (resolve, activate, and initialize
   the approved shared capture stream), then returns a typed access result or a
   capture session. This is the access request for that application model.
3. A denied or inaccessible attempt returns `PERMISSION_DENIED`; it never
   becomes `DEVICE_UNAVAILABLE`, `EMPTY`, or a successful idle state.
4. The provider releases all probe/session resources before returning a failure.
5. The application never retries in a loop and never changes Windows settings.

The exact consent API, if any, must be selected and evidenced in the native
helper gate. This RFC deliberately does not claim that a classic WASAPI
desktop process can display a consent prompt merely by calling WASAPI.

### Proposed application-facing port additions

These are implementation targets, not code authorization:

```text
MicrophoneRecorderController
    snapshot -> RecorderSnapshot
    subscribe(listener: RecorderListener) -> unsubscribe
    start(selection_token: str | None = None) -> bool
    stop() -> bool
    cancel() -> bool
    dismiss() -> bool
    close(deadline) -> None

AudioCaptureSession
    start() -> None
    read_chunk(deadline) -> bytes | None
    subscribe_level(listener: InputLevelListener) -> unsubscribe
    stop() -> None
    cancel() -> None
    close() -> None
```

`subscribe_level` is a typed application/infrastructure seam. An
`InputLevel` contains only a normalized finite value in `[0.0, 1.0]` and a
monotonic sample timestamp; it contains no PCM, device identity, or native
metadata. The provider may calculate it from capture packets inside the
helper. A provider without level support reports `0.0`; it must not fabricate
sound.

The existing service may retain its public `start()` method, but the
controller must call it outside the Qt thread. If the service is adjusted to
defer `open()` into its worker, the externally observable start/stop/cancel
contract must remain the same. A cancel received while `start()` is pending is
remembered and applied immediately after a handle is returned.

No level event is allowed to retain raw audio. The controller keeps only the
latest level and a bounded timestamped event slot; stale events are dropped.

## Recorder state machine

### Internal operation states

```text
UNAVAILABLE
    -> IDLE
IDLE
    -> REQUESTING_ACCESS
REQUESTING_ACCESS
    -> STARTING
    -> ERROR
    -> UNAVAILABLE
STARTING
    -> RECORDING_SILENT
    -> ERROR
    -> CANCEL_REQUESTED
RECORDING_SILENT
    -> RECORDING_SOUNDING       [level >= sound_on_threshold]
    -> STOPPING                  [stop]
    -> CANCEL_REQUESTED          [cancel/close]
    -> ERROR
RECORDING_SOUNDING
    -> RECORDING_SILENT          [level <= sound_off_threshold]
    -> STOPPING                  [stop]
    -> CANCEL_REQUESTED          [cancel/close]
    -> ERROR
STOPPING
    -> PROCESSING                [capture finalized and ASR admitted]
    -> EMPTY
    -> ERROR
    -> CANCEL_REQUESTED
PROCESSING
    -> SUCCEEDED
    -> EMPTY
    -> ERROR
    -> CANCEL_REQUESTED
CANCEL_REQUESTED
    -> CANCELLED                 [capture/ASR quiescence and cleanup proven]
    -> RECOVERY_PENDING          [cleanup/release not proven]
RECOVERY_PENDING
    -> ERROR                     [release proven and failure is publishable]
    -> UNAVAILABLE               [process kill/restart required by gate]
SUCCEEDED | EMPTY | CANCELLED | ERROR
    -> IDLE                      [dismiss/reset]
```

`RECORDING_SILENT` and `RECORDING_SOUNDING` are presentation projections of
the same capture generation; they are not separate service states. A missing
level event never transitions to `RECORDING_SOUNDING`.

The controller publishes exactly one terminal snapshot per generation. A late
service result, level event, or worker callback is ignored unless its
generation matches the current controller generation. `RECOVERY_PENDING` is
not terminal success and blocks a new start.

### Level thresholds and timing

The initial UI constants are recommendations for implementation and test:

| Constant | Value | Rule |
|---|---:|---|
| `sound_on_threshold` | `0.08` | enter sounding after one fresh level at or above it |
| `sound_off_threshold` | `0.04` | leave sounding after one fresh level at or below it |
| `level_stale_after` | `250 ms` | stale level is treated as silent |
| `level_update_period` | `50–100 ms` | provider target, not a UI timer requirement |

The lower off threshold prevents rapid visual chatter. A level outside
`[0.0, 1.0]`, non-finite level, or backwards timestamp is a typed provider
failure in native tests and is treated as silent only at the defensive UI
boundary. The waveform animation timer runs only in `RECORDING_SOUNDING`.

## Overlay presentation contract

### Window flags and ownership

`FloatingRecorderWindow` becomes an independent top-level Qt window. It must
not be constructed with `MainWindow` as its QWidget parent because a child
window may follow the parent's visibility. `MainWindow` retains an explicit
Python/Qt ownership reference and closes/disposes the overlay during session
cleanup.

Required flags:

```text
Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
```

The overlay is not `WindowTransparentForInput`; its controls must remain
usable and keyboard accessible. `WA_DeleteOnClose` remains false while the
main window owns the object. `closeEvent` uses the same cancel-or-dismiss
policy as the explicit close control.

### Geometry

The first implementation preserves the existing compact footprint:

| Item | Geometry/constraint |
|---|---:|
| Outer window | fixed `300 x 92` logical px |
| Outer radius | `14 px` stylesheet radius |
| Outer border | `1 px` theme border |
| Content margins | `12 px` on all sides |
| Row gap | `10 px` |
| Record/stop control | minimum width `82 px`; height from style |
| Close/cancel control | fixed `28 x 28 px`; accessible name changes by state |
| Waveform region | minimum `88 x 32 px`, preferred `112 x 32 px` |
| Placement | bottom-right of the anchor screen, `24 px` inset, clamped to available geometry |

`show_near(anchor)` computes global coordinates from the anchor's screen and
clamps the result so the complete overlay is visible. It may be called while
the main window is visible; after that, hiding the main window must not hide
the overlay. The overlay must not move to a different monitor implicitly while
recording.

### Controls and visibility

- The main action button starts recording only from `IDLE` or a retryable
  terminal state, and stops only from a recording state. It is disabled during
  access request, start, stopping, processing, cancellation, recovery, and
  unavailable states.
- The close/cancel button is always visible. During
  `REQUESTING_ACCESS`/`STARTING`/recording/`STOPPING`/`PROCESSING` it requests
  cancellation and remains visible until cancellation is acknowledged by the
  service. In `IDLE` and terminal states it dismisses the overlay. In
  `RECOVERY_PENDING` it remains a close affordance but cannot claim cleanup.
- Closing the main window cancels active work, waits only within the existing
  bounded application shutdown contract, then closes the overlay. It must not
  silently abandon the helper or ASR ownership fence.
- The status label uses stable localized messages only. Native HRESULTs,
  endpoint IDs, helper paths, and raw exception text never appear in the UI.

The overlay remains visible while the main window is hidden. It may be hidden
only by explicit dismissal in a non-active state or by application shutdown.

### Waveform rendering

The existing 15-bar `WaveformWidget` is retained as the visual primitive. Its
timer is started only when the snapshot projects to `RECORDING_SOUNDING`; it is
stopped for silent recording, all non-recording states, stale levels, errors,
and unavailable states. Silent recording renders a static low baseline, not an
animated waveform. The widget does not receive PCM and does not calculate an
audio level itself.

## Composition and lifecycle

1. `build_desktop_composition()` creates an unavailable recorder controller
   immediately, so the UI can be constructed without a runtime or native
   helper.
2. The existing asynchronous backend bootstrap remains outside the Qt thread.
   When a verified backend is ready, composition attaches its existing
   `BackendApplication.microphone` service to the controller. If the backend
   is unavailable, the controller remains `UNAVAILABLE` with the safe reason.
3. `MainWindow` receives the controller as a presentation dependency and
   creates one independent overlay. No UI object imports
   `WindowsAudioInputAdapter` or `MicrophoneRecordingService` directly.
4. On start, the controller increments a generation, publishes
   `REQUESTING_ACCESS`, and dispatches the service start to an application
   worker. The Qt thread returns immediately.
5. On a returned recording handle, the controller subscribes to levels,
   publishes `RECORDING_SILENT`, and keeps the handle until terminal cleanup.
6. Stop calls `handle.stop()` exactly once and projects `STOPPING`/`PROCESSING`.
   Cancel and active close call `handle.cancel()` exactly once and project
   `CANCEL_REQUESTED`.
7. The service result is mapped to `SUCCEEDED`, `EMPTY`, `CANCELLED`, or
   `ERROR` only after the service's existing cleanup and ASR quiescence fences.
8. Dismissal removes listeners, clears the level slot, and hides the overlay;
   it does not reset or terminate an active generation.
9. Application shutdown cancels the active generation, closes the controller,
   then closes the overlay. Cleanup remains idempotent.

The controller's worker/executor is bounded to one recorder command stream.
There is no microphone-specific ASR pool and no second runtime. A command is
`O(1)` in controller state. Capture and canonical assembly retain the existing
`O(n)` time and `O(n)` final audio storage with bounded queue space `O(b)`;
level publication is `O(1)` time and `O(1)` retained space per generation.

## Privacy and error behavior

| Condition | State/message behavior |
|---|---|
| Provider/helper absent | `UNAVAILABLE`; disable start; state says capture is unavailable in this build |
| Unsupported platform (including macOS) | `UNAVAILABLE`; no Windows probe or fallback is attempted |
| Unsupported native architecture | `UNAVAILABLE`; ARM64 is not claimed without native evidence |
| Windows consent/access denied | `ERROR`; say to allow desktop microphone access in Windows Privacy settings and retry |
| No active/stale selected device | `ERROR`; say the microphone is unavailable; no fallback device |
| Device busy | `ERROR`; say the microphone is busy; no silent retry |
| Device disconnected/invalidated | `ERROR`; discard partial capture and require a fresh start |
| Unsupported format/overrun/limit | `ERROR`; no ASR submission and no truncated success |
| User cancellation | `CANCELLED` only after cleanup/quiescence is proven |
| Shared ASR queue full | `ERROR`/rejected recording with a bounded retry message; no retained PCM |
| Cleanup/recovery not proven | `RECOVERY_PENDING`; block new starts and show recovery progress |
| Unexpected provider/ASR failure | `ERROR` with a fixed safe message; log only stable code and bounded metadata |

The UI must not call a Settings URI as a substitute for an access attempt. It
may offer a future explicit "Open Windows Privacy settings" action, but that is
separate from this RFC and must not mutate settings or imply that the setting
was changed.

## macOS versus Windows verification

### Verifiable on the current macOS host

- Domain/application state transitions, generation fencing, cancellation, stop,
  empty capture, typed error mapping, ASR admission, and cleanup using injected
  ports/services.
- The controller invokes the real `MicrophoneRecordingService` API in tests;
  the capture port is a deterministic test double, not a production adapter.
- Offscreen Qt geometry, rounded stylesheet, accessible names, button
  enablement, level threshold/hysteresis, waveform timer start/stop, independent
  overlay visibility after `MainWindow.hide()`, and idempotent disposal.
- Composition behavior when the provider is absent: unavailable must remain
  visible and no native fallback may be selected.

These tests prove contracts only. They do not prove Windows access, a consent
dialog, WASAPI, helper IPC, native capture, topmost behavior, or package
security.

### Requires Windows evidence

- The actual access/consent path for each claimed package/application model,
  including allowed, denied, revoked, and settings-changed cases.
- Real WASAPI endpoint activation, shared-mode initialization, capture level,
  device invalidation, privacy denial, and a named physical microphone.
- `WindowStaysOnTopHint` behavior when the main window is hidden/minimized and
  when another application is foreground; DPI/monitor placement on claimed
  Windows displays.
- Signed/hash-locked native helper startup, named-pipe authentication, Job
  Object cleanup, crash recovery, and clean-machine package behavior.

The evidence must be attached to G3b/G4b and must name Windows version,
architecture, package mode, Python/helper/converter versions, fixture device,
repetitions, and thresholds. `windows-latest` without a fixture and all macOS
runs are insufficient for native capture claims.

## Explicit native helper gate

The following gate is mandatory before enabling the provider in production:

```text
NATIVE-MICROPHONE-OVERLAY-GATE = GO only when:
  D1-D19 approved and synchronized
  G1/G2/G3a approved
  G4a helper/IPC/package feasibility = GO
  G3b real-device Windows capture evidence = PASS
  G4b clean-machine helper/package/recovery evidence = PASS
  access denied/allowed evidence is sanitized and retained
```

Until `GO`:

- `WindowsAudioInputAdapter` remains unavailable by default, even on Windows
  x64;
- no unverified helper is loaded from PATH or an adjacent mutable manifest;
- no test provider is composed into production;
- no macOS test or emulated Windows run is described as native capture; and
- the overlay may be exercised with a fake application service only as UI
  contract evidence.

The gate does not require the overlay UI itself to wait for native evidence,
but the UI must keep the capture capability unavailable until the gate is met.

## Acceptance tests

### Application and port contract

- **AC-001**: Starting from unavailable never calls the microphone service and
  publishes no ready/listening state.
- **AC-002**: Starting from an available controller publishes
  `REQUESTING_ACCESS`, invokes `MicrophoneRecordingService.start()` off the Qt
  thread, and returns control to Qt before the bounded open deadline expires.
- **AC-003**: The Windows provider's real access/open path is invoked on every
  user start that reaches the provider; status polling alone cannot satisfy the
  test. Access denial maps to `PERMISSION_DENIED` and the actionable safe
  message.
- **AC-004**: Stop reaches the current recording handle exactly once, produces
  one terminal result, and submits completed audio only through the shared ASR
  service.
- **AC-005**: Cancel during access request, capture, finalization, or ASR is
  idempotent; no partial transcript appears; terminal `CANCELLED` is published
  only after the existing cleanup/quiescence fence.
- **AC-006**: A stale callback or level event from an older generation cannot
  overwrite the current snapshot.
- **AC-007**: Missing provider, unsupported platform/architecture, device loss,
  busy, denied, timeout, cleanup, and helper failure remain typed and visible;
  none becomes empty success or silent retry.
- **AC-008**: Level events retain no PCM, accept only finite `[0, 1]` values,
  apply on/off thresholds, and clear to silent after `250 ms` without an event.
- **AC-009**: A full shared ASR queue releases audio exactly once and leaves no
  active microphone generation or retained PCM.

### Presentation and lifecycle

- **AC-010**: The overlay is a top-level `Qt.Tool` with
  `FramelessWindowHint` and `WindowStaysOnTopHint`, fixed at `300 x 92`, with a
  14 px rounded surface and accessible controls.
- **AC-011**: After showing the overlay, `MainWindow.hide()` leaves the overlay
  visible and interactive; closing/disposal of the application closes it.
- **AC-012**: The close/cancel control cancels active work and stays visible
  until cancellation/recovery is honestly represented; in an idle/terminal
  state it dismisses without starting a new recording.
- **AC-013**: The waveform timer is running only for a fresh level at or above
  the sound-on threshold while recording. Silent, stale, unavailable, error,
  and processing states have no animated bars.
- **AC-014**: All controls expose English and Russian accessible names and
  descriptions, and the error label never includes HRESULTs, endpoint IDs,
  private paths, raw PCM, or raw helper exception text.
- **AC-015**: Composition constructs an unavailable recorder before backend
  readiness, attaches only the existing microphone service after readiness, and
  closes controller, service, ASR, and overlay idempotently.

### Native Windows evidence

- **AC-016**: On each claimed Windows package/application model, an allowed
  access case opens a real named microphone and produces bounded capture; a
  denied case produces `PERMISSION_DENIED` and the expected settings guidance.
- **AC-017**: Revoking access, unplugging the device, changing the default, and
  closing the main window do not retarget or orphan the active session.
- **AC-018**: The overlay remains topmost and usable while the main window is
  hidden on the claimed Windows DPI/monitor configurations.
- **AC-019**: Native helper crash, malformed IPC, timeout, and shutdown prove
  Job Object cleanup and keep new microphone work blocked until ownership is
  released, with no raw audio in evidence.

## Implementation order after approval

1. Add/approve the recorder snapshot/controller contract and race matrix without
   enabling production capture.
2. Extend the application service/session seam for access/open lifecycle and
   normalized level subscription; preserve existing bounded PCM and ASR tests.
3. Wire a stable unavailable controller through desktop composition and replace
   the recorder's synchronous `ShellController` path with the controller
   snapshot bridge.
4. Convert the recorder to an independent top-level window; implement geometry,
   close/cancel behavior, level-driven waveform animation, localization, and
   offscreen tests.
5. Only after `NATIVE-MICROPHONE-OVERLAY-GATE` is `GO`, implement and package
   the approved provider/helper, then run G3b/G4b evidence.
6. Enable the Windows capability only from verified composition/configuration;
   retain the unavailable UI and rollback path.

## Rollback

If access classification, capture, cleanup, privacy behavior, level telemetry,
or topmost lifecycle fails on Windows, disable the microphone capability at
composition level. Keep the overlay and unavailable state available for
diagnostics, preserve imported-media behavior, stop/reap helper generations
under the approved recovery policy, and remove the provider enablement as one
logical change. Never fall back to Python capture, another endpoint, or silent
retry.

## Open decisions before implementation

- Which Windows application/package model is shipped for the first native
  helper, and therefore whether a separate Windows consent API is applicable?
- What exact evidence proves that the selected consent/access path is an actual
  request rather than status polling?
- Are the proposed level thresholds and 250 ms stale timeout acceptable for the
  product's microphone fixtures?
- Should a future Settings-link action be included, or is text guidance enough
  for v1?
- Which existing `ShellController` snapshot consumers are migrated to
  `MicrophoneRecorderController` in the composition slice without preserving a
  second recording state machine?
