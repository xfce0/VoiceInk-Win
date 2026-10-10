# RFC: Audio Page Controls

## Status

Status: proposed. This workstream implements the Qt Audio page only. Native
microphone capture, device enumeration, playback, and global shortcuts remain
unavailable and are not enabled by this RFC.

## Summary

Add the Audio destination to the existing Qt shell. The page presents the input
route and recording-behavior preferences already described by the desktop-page
contract, loads values from the existing SQLite `Settings.audio_preferences`
mapping, and shows a safe unavailable state while the native backend is absent.

## Goals

- Show input-device list/status and the currently selected input route.
- Show contract-aligned recording behavior preferences: mute, media pause,
  resume delay, start sound, and stop sound.
- Keep native-dependent controls disabled and explain why they cannot be used.
- Preserve supported audio preference values through the existing settings store.
- Add English/Russian catalog entries and GUI coverage for navigation, state,
  localization, and persisted values.

## Non-Goals

- Implementing WASAPI, COM, a native helper, device enumeration, capture,
  playback, permissions, or recording orchestration.
- Registering or handling global shortcuts.
- Persisting native endpoint IDs or process-local selection tokens.
- Changing the blocked microphone feature gates or claiming native evidence.

## Proposed Architecture

`MainWindow` owns an `AudioPage` beside the existing persisted pages. The page
uses `PersistenceService.get_settings()` through `FutureBridge`, reads only
safe JSON values from `Settings.audio_preferences`, and never calls an audio
port or native API. Device status is an explicit unavailable snapshot; the
route and behavior widgets render persisted values but remain disabled until a
verified native backend supplies capabilities.

The supported preference keys are `input_route`, `mute_while_recording`,
`pause_media_while_recording`, `resume_delay_seconds`, `start_sound`, and
`stop_sound`. `input_route` stores only the route mode (`system_default`,
`selected_device`, or `priority_order`); no endpoint identity is stored.

## Exit Criteria

- Audio is reachable from the enabled sidebar and all page text is localized.
- Existing settings load without blocking the Qt thread and are displayed
  without inventing devices or backend readiness.
- GUI tests cover unavailable controls, persisted values, navigation, and both
  locales.
- Targeted format, lint, spec, and GUI tests pass.
- Native capture and shortcut behavior remain absent from the diff.

## Open Questions

- Which approved native device descriptor and selection-token lifecycle will
  populate the device list after the microphone gates close?
- Which native backend owns playback pause/resume and start/stop sounds?
- What Windows evidence is required before any disabled control can be enabled?
