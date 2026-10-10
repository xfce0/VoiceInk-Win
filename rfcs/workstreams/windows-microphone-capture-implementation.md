# RFC: Windows Microphone Capture Implementation Slice

## Status

Status: implementation-ready seam; native capture remains disabled.

This workstream records the smallest production change that can be verified in
the current repository and on the current development host. It does not approve
or implement the C++ WASAPI helper described by `rfcs/microphone-recording.md`.
The machine-readable WASAPI contracts remain `implementation_allowed: false`,
and microphone enablement remains blocked by G3b and G4b.

## Summary

Implement a provider-backed `WindowsAudioInputAdapter` behind the existing
`AudioInputPort`. The adapter owns platform/capability reporting, delegates only
safe `InputDevice` values and bounded PCM16 chunks, maps infrastructure failures
to stable domain `CaptureErrorCode` values, and makes session cleanup idempotent.
The existing `MicrophoneRecordingService` remains responsible for one active
recording, bounded assembly, cancellation, terminal states, and shared ASR
admission through `AsrApplicationService.try_admit()`.

The default composition deliberately supplies no native provider. It therefore
reports an explicit unavailable capability instead of pretending that Python,
FFmpeg, a fake backend, or an unverified executable can capture audio.

## Goals

- Preserve the existing `Presentation -> Application -> Domain <- Infrastructure`
  dependency direction.
- Make the infrastructure seam usable by a future verified Windows helper
  without exposing COM, WASAPI packets, native handles, endpoint IDs, or HRESULTs.
- Report supported, unsupported-platform, unsupported-architecture, and
  missing-provider states explicitly.
- Preserve bounded PCM capture, cancellation, exactly-once terminal publication,
  cleanup-before-result, and shared ASR admission.
- Make provider failure mapping and cleanup behavior testable without Windows or
  a physical microphone.

## Non-Goals

- No C++ helper, COM/WASAPI binding, named-pipe serializer, ctypes, pywin32,
  PortAudio, `sounddevice`, FFmpeg live capture, or new runtime dependency.
- No fake microphone backend in production infrastructure.
- No native Windows evidence claim from macOS tests, injected test providers, or
  generic CI.
- No streaming ASR, recording-file persistence, endpoint fallback, device
  retargeting, or UI enablement.

## Proposed Architecture

```text
Presentation
    -> MicrophoneRecordingService
    -> AudioInputPort
    -> WindowsAudioInputAdapter
    -> WindowsCaptureProvider (future verified helper boundary)
    -> bounded canonical PCM16 assembly
    -> shared AsrApplicationService.try_admit()
```

`WindowsAudioInputAdapter` is the only concrete production object currently
created by the composition root. Its optional provider is an infrastructure
protocol, not a domain API. A future provider must own all native details and
return only the already-approved `AudioCaptureSession`/`InputDevice` shapes.

The application state behavior is unchanged:

```text
start -> capture -> stop -> cleanup -> ASR -> terminal
                    \-> cancel -> cleanup -> Cancelled
capture failure -> cleanup -> Failed
zero accepted frames -> cleanup -> Empty
ASR queue full -> cleanup -> Rejected
```

No ASR request is created for unsupported capability, capture failure, empty
capture, cancellation before finalization, or cleanup failure.

## Decision

1. Use the existing domain `AudioInputPort` as the stable boundary.
2. Add a domain-level `MicrophoneCapability` to `MicrophoneStatus`; availability
   describes current device readiness while capability describes whether this
   build can capture at all.
3. Keep provider failure kinds in infrastructure and map them to stable domain
   capture codes with sanitized messages. Native error text never crosses the
   boundary.
4. Make the provider-backed session single-start and idempotently closable. A
   failed close remains retryable; the application publishes no success until
   close succeeds.
5. Keep default production capability disabled until a verified native helper is
   supplied. A test provider may exercise the seam, but it is not production
   capture evidence.

## Platform Claims And Limitations

The intended native target remains Windows 10 22H2 and Windows 11 23H2/24H2,
x64, with Python 3.12-3.14. This commit proves none of those native claims.

- macOS and Linux: explicitly unsupported; the default adapter never attempts
  platform probing or fallback capture.
- Windows x64: the architecture is reserved for a future verified helper, but
  the current composition reports `unsupported` because no provider/helper is
  packaged or configured.
- Windows ARM64: native ARM64 microphone support is not claimed and is reported
  as `unsupported_native_architecture`. Running an x64 process under emulation
  does not become a native ARM64 claim.
- Windows privacy permissions, device loss, WASAPI format support, helper crash
  recovery, named-pipe authentication, Job Object cleanup, packaging, and real
  device capture remain unverified until G3b/G4b evidence exists.

## ASR Integration

When a future provider returns a non-empty canonical PCM16 session, the existing
`MicrophoneRecordingService` submits exactly one `AsrRequest` through the shared
`AsrApplicationService`. No microphone-specific ASR pool, runtime, retry, or
fallback is introduced. The existing request-scoped cancellation and quiescence
fence remains authoritative.

## Error And Cleanup Mapping

Provider failures are mapped as follows:

| Infrastructure failure | Domain code |
|---|---|
| permission denied | `PERMISSION_DENIED` |
| no device/stale selection | `DEVICE_UNAVAILABLE` |
| device contention | `BUSY` |
| unsupported format | `UNSUPPORTED_FORMAT` |
| device invalidated/disconnected | `DEVICE_DISCONNECTED` |
| bounded queue overrun | `CAPTURE_OVERFLOW` |
| deadline | `TIMEOUT` |
| user cancellation | `CANCELLED` |
| cleanup failure | `CLEANUP_FAILED` |
| other provider failure | `FAILED` |

Mapping uses fixed safe messages. The native exception, HRESULT, endpoint ID,
and full path are discarded at the adapter boundary.

## Verification

The targeted tests cover:

- supported provider capability and delegation;
- unsupported platform, missing provider, and explicit ARM64 rejection;
- session start/stop/cancel/close transitions and idempotent cleanup;
- stable mapping for each provider failure class;
- bounded application stop/cancel/empty/queue-rejected/ASR paths; and
- no ASR call when capture is unsupported or fails.

These tests are cross-platform contract evidence only. They do not establish
WASAPI, native helper, Windows packaging, x64 hardware, or ARM64 support.

## Exit Criteria

This seam is complete when the adapter, status/capability values, failure map,
cleanup behavior, tests, and this decision are committed and relevant local
checks pass. Native enablement requires a later change with a verified helper,
the approved protocol/build/artifact contracts, and G3b/G4b evidence.

## Open Questions

- Which approved C++17 helper implementation and converter will satisfy the
  existing D1-D19/G3a/G4a decision register?
- Where will the signed, hash-locked x64 helper be provisioned in the portable
  package?
- Which Windows hardware fixture and clean-machine lanes will provide G3b/G4b?
- Will a future native ARM64 helper be built, or will x64 emulation remain the
  only ARM64 deployment mode without a native support claim?
