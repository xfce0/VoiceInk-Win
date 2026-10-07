# Feature: Windows Frontend Shell

## Status and Scope

Status: implemented.

This feature provides the first PySide6 presentation slice for VoiceInk-Win. It
ports the macOS VoiceInk visual hierarchy into a Windows-friendly desktop shell
without copying SwiftUI or AppKit implementation details. The slice includes a
dashboard, a small floating recorder panel, and an application-owned state
controller backed by an injected transcription protocol.

It excludes microphone capture, global hotkeys, imported-media selection,
history persistence, real ASR runtime wiring, system tray integration, and
Windows-specific APIs.

## User Scenarios

- A user opens VoiceInk and sees a dashboard with a clear first-session action.
- A user opens the floating recorder, starts and stops a demo session, and sees
  recording, processing, and transcript-ready feedback.
- A user receives a useful empty or error state instead of a silent failure.

## Product Intent

- The main window keeps the macOS reference's 220 px navigation rail, quiet
  system surfaces, warm orange progress card, and compact transcript rows.
- Recording is represented by a black, compact floating panel with a record
  button, waveform, and processing indicator.
- Idle, recording, processing, transcript-ready, empty, and error states are
  explicit and user-visible. Empty and error states explain the next action.
- The shell is usable on macOS without PySide6 or native ASR dependencies by
  using a deterministic fake backend through an application protocol.

## Architecture

The dependency direction remains:

```text
Presentation -> Application -> Domain <- Infrastructure
```

- Domain owns `ShellState` and immutable `ShellSnapshot` values.
- Application owns `ShellController` and the `ShellTranscriptionBackend`
  protocol. The controller never imports Qt, subprocess, HTTP, or Windows APIs.
- Infrastructure provides `FakeShellBackend` for the shell demo and tests.
- Presentation owns the optional PySide6 window and maps snapshots to widgets.

## Functional Requirements

1. A fresh controller starts in `idle` with no transcript or error.
2. Starting a session moves the controller to `recording`; stopping it moves to
   `processing` before the backend is called.
3. A non-empty backend result becomes `transcript_ready`; an empty result
   becomes `empty`; backend failures become `error` with a safe message.
4. The presentation must not construct or call a concrete ASR runtime.
5. The floating panel can be shown, hidden, and closed without a Windows API.
6. Closing the main window closes the floating panel and exits the Qt process.
7. Tray behavior is deferred behind a future lifecycle adapter; the shell must
   not claim tray support in this slice.

## Non-Functional Requirements

- Core application and domain tests must not import PySide6 or require a display.
- The presentation layer must not import subprocess, HTTP, Windows APIs, or
  concrete ASR infrastructure.
- The shell must keep user-visible failure state explicit and avoid logging or
  displaying raw audio data.

## Error and Cancellation Behavior

The first slice has no live audio cancellation path. A user can dismiss the
floating panel, which resets the demo session to `idle`; an in-flight Qt timer
is stopped before reset. Backend errors become `error` snapshots and do not
become empty successful transcripts.

## Acceptance Criteria

- `pytest` covers every controller state transition and invalid transition.
- The core tests import and run without PySide6 or a display server.
- `PySide6` is an optional dependency and the regular package remains importable
  when it is absent.
- `make check` passes on the existing macOS development environment.
- The commit does not add microphone, hotkey, history, media queue, or ASR
  runtime behavior.

## Test Plan

- Unit tests cover initial state, valid transitions, empty results, typed
  runtime failures, invalid actions, and listener removal.
- Existing repository tests remain the regression suite for backend contracts.
- GUI smoke validation is deferred because PySide6 is optional and macOS CI
  may not provide a display server.

## Open Questions and Deferred Work

- Windows tray icon, close-to-tray policy, and application activation policy.
- Native global hotkey and WASAPI adapters.
- Connecting the shell controller to `AsrApplicationService` after microphone
  capture and an audio input port are specified.
- Packaging PySide6 and selecting the supported Qt deployment strategy.
