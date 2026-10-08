# Feature: Windows Frontend Shell

## Status and Scope

Status: proposed amendment to the implemented presentation baseline. The
unavailable-shell composition described by
`rfcs/desktop-composition-boundary.md` is not implemented yet.

This feature provides the first PySide6 presentation slice for VoiceInk-Win. It
ports the macOS VoiceInk visual hierarchy into a Windows-friendly desktop shell
without copying SwiftUI or AppKit implementation details. The slice includes a
dashboard, a small floating recorder panel, and an application-owned state
controller. Production startup is a no-resource unavailable shell; injected
backends are test seams, while any optional demo is a separate development-only
path.

It excludes microphone capture, global hotkeys, imported-media selection,
history persistence, real ASR runtime wiring, system tray integration, and
Windows-specific APIs.

## User Scenarios

- A user opens VoiceInk and sees that recording is unavailable in this build,
  with an explanation that microphone capture and ASR are not included.
- A user opens the recorder explanation panel and sees a disabled recording
  action, inactive waveform, and no synthetic transcript.
- Focused tests may use an injected fake backend; a separately named,
  development-only demo is optional and is not the production composition.

## Product Intent

- The main window keeps the macOS reference's quiet system surfaces, warm orange
  progress card, and compact transcript rows. Its navigation is a 68 px
  icon-only rail with repository-owned Lucide-style tiles; original destination
  names remain available through tooltips and accessibility labels.
- The unavailable explanation is represented by a black, compact floating panel
  with a disabled action and inactive waveform; it has no processing indicator
  or recording timer.
- The production unavailable state is explicit and user-visible. It does not
  render idle/recording copy, a success timestamp, or a transcript.
- The shell is usable without native ASR dependencies because its production
  composition creates no backend and exposes an unavailable controller state.

## Architecture

The dependency direction remains:

```text
Presentation -> Application -> Domain <- Infrastructure
```

- Domain owns `ShellState` and immutable `ShellSnapshot` values.
- Application owns `ShellController` and the `ShellTranscriptionBackend`
  protocol. The controller never imports Qt, subprocess, HTTP, or Windows APIs.
- `FakeShellBackend` is a test fixture and is not exported by the production
  infrastructure package. An optional separately named development-only demo
  may use that fixture directly; production composition must not import,
  construct, package, or select it.
- The desktop composition owns the controller and exposes only the no-resource
  lifecycle API defined by `rfcs/desktop-composition-boundary.md`.
- Presentation owns the optional PySide6 window and maps snapshots to widgets.

## Functional Requirements

1. The production composition creates an unavailable controller with no
   backend, runtime, process, microphone, or network resource.
2. An unavailable controller starts in `UNAVAILABLE`; start, stop, and complete
   actions return `False`, while reset is an idempotent no-op.
3. Unavailable snapshots have no transcript or error, and unavailable actions
   do not publish snapshots or notify listeners.
4. The presentation renders the exact unavailable copy and disables the
   recorder action with an accessible explanation.
5. The waveform is inactive and no recording, processing, or animation timer
   runs in the unavailable state.
6. The production entrypoint obtains its controller only from
   `build_desktop_composition()` and closes that composition on normal and
   exceptional event-loop paths.
7. Tray behavior, microphone capture, and runtime integration remain deferred.

## Non-Functional Requirements

- Core application and domain tests must not import PySide6 or require a display.
- The presentation layer must not import subprocess, HTTP, Windows APIs, or
  concrete ASR infrastructure.
- The shell must keep user-visible failure state explicit and avoid logging or
  displaying raw audio data.
- The shell follows the operating system light/dark color scheme at launch and
  applies semantic tokens to the window, rail, viewport, cards, controls, and
  waveform. If Qt reports an unknown scheme, the effective window palette is
  used as a fallback.
- Sidebar destinations use repository-owned SVG-backed QIcons, retain their
  original names through tooltips/accessibility labels, and remain disabled
  until their destination behavior exists.

## Error and Cancellation Behavior

The unavailable state has no live audio or cancellation path. User actions
cannot start recording, processing, or transcription. No exception is mapped
to a synthetic transcript. If composition construction fails, the exception
propagates. If window construction or the Qt event loop fails after composition
construction, the composition is closed and the original exception propagates;
normal Qt exit codes are preserved. Aggregate cleanup failure and cleanup exit
codes are deferred to a shutdown-reliability RFC.

## Acceptance Criteria

- `pytest` covers unavailable behavior and the injected enabled-controller
  transitions without requiring PySide6 or native runtime dependencies.
- The core tests import and run without PySide6 or a display server.
- `PySide6` is an optional dependency and the regular package remains importable
  when it is absent.
- Offscreen presentation tests cover exact unavailable copy, disabled controls,
  inactive waveform, and absent timers.
- `make spec-check` passes on the registered source-of-truth documents.
- The commit does not add microphone, hotkey, history, media queue, or ASR
  runtime behavior.

## Test Plan

- Unit tests cover initial state, valid transitions, empty results, typed
  runtime failures, invalid actions, and listener removal.
- Existing repository tests remain the regression suite for backend contracts.
- Non-GUI tests cover light/dark token selection, stylesheet surface coverage,
  system-scheme fallback parsing, and sidebar registry invariants.
- The Windows packaging workflow validates the frozen x64 PE and launches the
  shell through its real entrypoint with Qt's offscreen platform plugin.
- Interactive GUI validation remains a Windows user acceptance step.

## Open Questions and Deferred Work

- Windows tray icon, close-to-tray policy, and application activation policy.
- Native global hotkey and WASAPI adapters.
- Desktop archive inspection and frozen-bundle content policy.
- Full composition cleanup aggregation and resource rollback.
- Connecting the shell controller to `AsrApplicationService` after microphone
  capture and an audio input port are specified; runtime details remain in the
  Parakeet runtime RFC.
- The PyInstaller Windows x64 shell artifact is now packaged by the pinned
  `Windows Frontend Shell Build` workflow; packaging a production ASR runtime
  remains deferred.
