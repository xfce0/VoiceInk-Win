# RFC: Availability Recording UI

## Status

Implemented on `fix/availability-recording-ui`.

## Summary

The Dashboard currently describes the unavailable recording capability with
build and implementation language such as "in this build", "not included",
and "locked". Those strings expose internal delivery state instead of telling
the user which action is available. The Dashboard, floating recorder, and
Settings page also need a consistent distinction between a usable navigation
surface and a disabled capability.

## Existing Contracts

- `ShellController.unavailable()` publishes `ShellState.UNAVAILABLE` and is
  the presentation/application boundary for the current recording slice.
- `MainWindow` and `FloatingRecorderWindow` render that immutable shell
  snapshot; they do not own microphone or ASR behavior.
- `MicrophoneRecordingService.status` delegates to the existing
  `AudioInputPort.status()` contract. `WindowsAudioInputAdapter` currently
  reports `MicrophoneAvailability.UNAVAILABLE` and must remain unavailable.
- Settings persistence availability is independent from microphone recording
  availability. The Settings top banner is reserved for unavailable local
  storage; field values carry feature-specific capability placeholders.

## Decision

- Replace Dashboard copy that refers to a build, inclusion, enablement, or
  locking with localized neutral capability/status copy.
- Keep the Dashboard recorder entry enabled because it opens a status panel;
  keep the floating recorder's record control disabled while the recording
  capability is unavailable.
- Keep Insights disabled and label it as unavailable rather than locked.
- Keep all unavailable-state labels and accessible descriptions in the
  English/Russian translation catalog.
- Keep the Settings unavailable banner hidden when persistence is usable. When
  persistence is absent, show the localized storage state in that banner and
  disable settings controls. Show unavailable model/audio preferences inline
  as field values, not as a second page-level error state.
- Do not add native microphone capture, a fake adapter, or an ASR fallback.
  The existing `AudioInputPort` status boundary remains the hook for the
  future microphone workstream.

## Verification

- Offscreen GUI tests assert the English Dashboard, recorder, Insights, and
  Settings states, including enabled/disabled controls and banner visibility.
- Equivalent Russian assertions cover the neutral capability copy.
- Tests assert that the unavailable recorder has no active waveform or
  processing timer and that its record action cannot be invoked.
- Focused presentation tests and the repository's relevant full test suite
  must pass.
