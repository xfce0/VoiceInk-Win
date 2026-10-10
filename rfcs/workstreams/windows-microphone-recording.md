# RFC: Windows Microphone Recording Workstream Slice

## Status

Implemented as a platform-independent vertical slice. Native WASAPI capture
remains unavailable because the existing microphone RFC and contract scaffold
keep `implementation_allowed: false`; no G4a approval or G3b/G4b evidence is
present. This RFC does not change those gates and does not claim a production
WASAPI implementation.

## Scope

This slice adds:

- an explicit `AudioInputPort` and single-use `AudioCaptureSession` boundary;
- a bounded canonical PCM16 assembler with the existing 64 MiB `CanonicalAudio`
  contract and the proposed 32-minute/32 KiB-chunk limits;
- start, stop, cancellation, cleanup, and typed terminal results;
- `WindowsAudioInputAdapter`, which reports a stable unavailable state rather
  than using a fake, `ctypes`, `pywin32`, FFmpeg, or in-process WASAPI fallback;
- microphone submission through `AsrApplicationService.try_admit()` and its
  request-scoped handle. No microphone-specific ASR pool or admission path is
  introduced.

Global shortcuts and Audio page UI are intentionally out of scope.

## Data Flow

```text
caller -> MicrophoneRecordingService -> AudioInputPort
       -> bounded PCM16 assembly -> CanonicalAudio
       -> shared AsrApplicationService.try_admit()
```

Only `InputDevice`, bounded PCM16 chunks, and `CanonicalAudio` cross the
application boundary. Native endpoint IDs, COM objects, WASAPI packets, and
helper protocol values do not cross it.

## Safety Rules

- A second active recording is rejected.
- Empty capture produces `EMPTY` and never calls ASR.
- Chunks must be complete PCM16 samples and no larger than 32 KiB.
- Aggregate capture is capped at 30,720,000 samples / 61,440,000 bytes.
- A limit crossing fails closed; audio is not truncated, padded, dropped, or
  spilled to disk.
- `stop()` ends capture and permits final ASR submission; `cancel()` cancels
  capture or the identified ASR request and waits for the existing ASR
  quiescence/release contract before terminal publication.
- Queue-full admission is returned as `REJECTED`; it does not retry or retain
  PCM.

## Gate Limitation

The implementation is testable with a local port double, but no fake microphone
adapter is part of production infrastructure. The Windows adapter remains
unavailable until the native contract supplies the approved D1-D19 decisions,
`G1`, `G2`, `G3a`, and `G4a=GO`, followed by the `G3b` real-device and `G4b`
clean-machine/package evidence. The existing protocol/build/artifact lock
files remain blocked and unchanged.

## Verification

The workstream is covered by bounded assembler, unavailable-state, stop/cancel,
queue admission, and shared-ASR scheduling tests. Platform-independent tests
prove the seam only; they are not Windows microphone evidence.
