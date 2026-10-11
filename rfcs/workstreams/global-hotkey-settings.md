# RFC: Configurable Global Start/Stop Hotkey

## Status

Status: Draft - implementation blocked.

This RFC is a design and acceptance contract only. It authorizes no production
code, migration, UI change, packaging change, or Windows native implementation.

## Summary

VoiceInk will expose one configurable global command: start or stop the active
microphone recording. The default combination is `Ctrl+Space`. On Windows the
combination is registered with the Win32 `RegisterHotKey` API and delivered
through `WM_HOTKEY`, so it works while VoiceInk is unfocused, minimized, or
hidden behind the system tray. A Qt focused shortcut (`QShortcut`, widget
`QKeySequence`, or `QApplication` key handling) is not an implementation of
this feature.

The Settings page will capture a single modifier-plus-key chord from the
keyboard, validate it, and submit it to an application-owned settings/use-case
boundary. A candidate is not persisted as the active preference until its
native registration succeeds. Conflicts, invalid combinations, persistence
failures, unavailable recording, and unsupported platforms remain visible as
localized feedback; they never crash the desktop session.

The global-hotkey owner will be a process/background lifecycle component, not a
`MainWindow` or Settings-page resource. Closing a window to the tray keeps the
registration alive. Application shutdown unregisters it before the Qt event
loop and recording/backend resources are torn down.

## Context and Existing Behavior

### Current implementation

The current branch contains the implementation from merged PR #38:

- `domain.shortcuts` defines `GlobalShortcut`, typed registration failures, and
  the default `Ctrl+Alt+Space`.
- `application.global_shortcut` owns one registration and toggles the existing
  `ShellController` through a `GlobalShortcutPort`.
- `infrastructure.global_shortcut` calls Win32 `RegisterHotKey` on Windows and
  uses a native Qt event filter to observe `WM_HOTKEY`.
- Non-Windows uses `UnavailableGlobalShortcutPort`; it does not emulate a
  global shortcut with Qt.
- `Settings.hotkeys` is a JSON mapping in the existing SQLite singleton. The
  accepted update field is `hotkeys.start_stop`; no migration was added.
- `SettingsPage` currently exposes a free-text `QLineEdit` and updates the
  registration after persistence completes.
- `MainWindow` currently registers in its constructor and unregisters in
  `dispose()`.

### PR #38 behavior and gaps

PR #38 (`feat: add global recording toggle shortcut`, merged as PR #38) proves
the initial port and lifecycle seam, but is not the final configurable-settings
contract. Its behavior is deliberately superseded in the following areas:

1. The default is `Ctrl+Alt+Space`, not the required `Ctrl+Space`.
2. The Settings control accepts text rather than recording a keyboard chord.
3. Persistence happens before native registration; a conflicting value can be
   stored even though it is not active.
4. Registration is owned by the window, so a future hide-to-tray path would
   accidentally remove the background hotkey.
5. Registration status exists in the application service but is not rendered as
   localized Settings feedback.
6. Tests cover fake ports and parser behavior, but do not prove delivery from a
   real unfocused Windows desktop session.
7. The current desktop composition intentionally exposes an unavailable shell
   controller while microphone capture is unfinished. The hotkey implementation
   must not treat this state as evidence that a native Windows hotkey works.

The existing `hotkeys` JSON shape and `SettingsPort` are reusable. The feature
does not require a new SQLite table or a schema migration.

## Goals

- Make `Ctrl+Space` the effective default for a missing or blank setting.
- Let a user record one supported modifier-plus-key chord from the keyboard.
- Register a real process-wide Windows hotkey independent of window focus.
- Keep Win32, `ctypes`, and Qt native-event details outside Domain and
  Application ports.
- Validate before commit and keep persisted, displayed, and active values
  consistent after failure.
- Surface registered, conflict, invalid, unavailable, unsupported-platform,
  and persistence-error states in English and Russian.
- Keep exactly one active start/stop registration per process.
- Define ownership that survives future window/tray/background lifecycle work.
- Prove portable Domain/Application behavior and provide explicit Windows
  desktop-session evidence for the native claim.

## Non-Goals

- A cancel shortcut, push-to-talk mode, second global command, or shortcut
  profiles.
- A Qt-only focused shortcut fallback on any platform.
- Registering hotkeys before the microphone/recording capability exists.
- Taking over a conflicting combination, changing Windows privacy settings, or
  modifying registry/system shortcut configuration.
- Text injection, tray implementation, overlay implementation, or microphone
  capture implementation itself.
- Supporting arbitrary key syntax that the approved Windows adapter cannot
  represent.

## Decisions

### D1. Default and stored value

`Ctrl+Space` is the canonical default. The effective value is resolved as:

1. a valid non-blank `Settings.hotkeys["start_stop"]`, if present;
2. otherwise `Ctrl+Space`.

The default is shown in Settings and registered at runtime even when no row has
yet been written. The first successful user change writes the canonical string
to `hotkeys.start_stop`. Existing explicit values, including an explicit
`Ctrl+Alt+Space` written by PR #38, are preserved as user choices; they are not
silently rewritten. A blank or missing legacy value receives the new default.

### D2. Windows is the only supported native platform

On Windows, the infrastructure adapter must use `RegisterHotKey` with a stable
process-owned hotkey identifier and must consume `WM_HOTKEY`. The registration
must not depend on a visible `HWND`; a thread/process registration is valid for
the background case. A Qt native event filter may be used only as the delivery
bridge after Win32 registration; it is not the shortcut mechanism.

On non-Windows platforms the adapter always reports
`UNSUPPORTED_PLATFORM`/`UNAVAILABLE`, never installs `QShortcut`, never listens
for application-level key events as a fallback, and never claims global
behavior. The Settings control is read-only/disabled with an explicit
Windows-only explanation. A previously persisted value is retained for a
future Windows run but is not registered or changed locally.

### D3. Supported v1 chord grammar

The user records exactly one chord containing at least one modifier and one
non-modifier key. The canonical display/storage order is:

```text
Ctrl | Alt | Shift | Win + key
```

Duplicate modifiers are rejected. Pure modifiers, multi-stroke sequences,
mouse buttons, text input, dead keys, and unsupported keys are rejected before
calling the native adapter. The initial Windows key matrix is the matrix
already exercised by PR #38: `Ctrl`, `Alt`, `Shift`, and `Win`, with `A-Z`,
`0-9`, `Space`, `Enter`, `Tab`, `Escape`, and `F1-F24`. The adapter remains the
final authority because Windows can reject an otherwise syntactically valid
combination.

The stored value is canonical text, not a Qt serialization and not a native
virtual-key integer. Domain/Application can validate grammar without importing
Qt or Windows; Infrastructure maps the canonical value to modifier flags and a
virtual key.

### D4. Failed updates do not become active settings

Changing a hotkey is a transactional application operation from the user's
perspective:

1. capture and domain-validate the candidate;
2. attempt to replace the native registration while retaining the current
   committed value for rollback;
3. persist the canonical candidate only after registration succeeds;
4. if persistence fails, restore the previous registration and previous
   displayed value;
5. if registration fails, keep both the old registration and old persisted
   value, and show the typed reason.

The implementation may use a short unregister/register window if the native
port cannot prepare a second registration, but it must attempt rollback and
must report `UNAVAILABLE` if restoration cannot be proven. A conflict must not
leave a configured-but-inactive hotkey silently presented as active.

The application boundary should expose stable status/reason codes rather than
leaking raw Win32, Qt, or exception text into the UI. Persistence remains
serialized through the existing `PersistenceService`/SQLite worker.

### D5. One owner and one active registration

The application/background lifecycle owns one `GlobalToggleShortcutService`.
Settings pages and windows observe or request changes through that service; they
do not call `RegisterHotKey`, own a native registration, or unregister it when
they are destroyed. Repeated start/register and stop/unregister operations are
idempotent. Replacing a chord releases the previous native registration exactly
once.

### D6. Recording command semantics

The native callback sends one non-blocking toggle command to the existing
recording application boundary:

- `RECORDING` -> request stop;
- `IDLE`, `TRANSCRIPT_READY`, `EMPTY`, or `ERROR` -> request start;
- `PROCESSING` -> ignore the toggle until processing completes;
- `UNAVAILABLE` -> ignore it and retain the unavailable status.

The callback does not capture audio, wait for ASR, run persistence, or touch
widgets. Any required worker dispatch is owned by Application. A failed start
or stop remains visible through the existing recording state/error mechanism.

## Proposed Architecture

```text
Settings keyboard capture
        -> Presentation adapter
        -> GlobalHotkeySettings application use case
        -> GlobalToggleShortcutService + SettingsPort
        -> GlobalShortcutPort
        -> Win32 RegisterHotKey / WM_HOTKEY
        -> recording application controller
```

### Domain

Domain owns:

- the `GlobalShortcut` value object and canonical grammar;
- the `Ctrl+Space` default;
- validation error categories;
- `GlobalShortcutPort` and registration lifetime protocols; and
- platform-independent registration/status reason values.

Domain imports no PySide6, Qt key enums, `ctypes`, Win32 constants, or native
handles. `Settings` continues to treat `hotkeys` as JSON-safe data, while the
hotkey application boundary validates the `start_stop` value before use.

### Application

Application owns:

- effective-setting resolution and the one active registration;
- replacement/rollback sequencing;
- mapping controller state to the toggle command;
- stable status snapshots for Presentation; and
- the persistence operation and its generation fencing.

The application service must not use a `QWidget`, wait on a `Future` from the
Qt thread, or expose native event types. The settings update callback must be
safe against stale asynchronous results: a late result from an older candidate
cannot replace a newer committed candidate.

### Infrastructure

`WindowsGlobalShortcutPort` owns:

- Win32 modifier and virtual-key mapping;
- a stable hotkey ID scoped to the process;
- `RegisterHotKey`/`UnregisterHotKey` calls;
- `WM_HOTKEY` delivery through the existing Qt native-event bridge; and
- mapping `ERROR_HOTKEY_ALREADY_REGISTERED` to `CONFLICT`, unsupported syntax
  or Windows rejection to typed unavailable/invalid reasons.

The adapter must clean up its native event filter and registration on every
normal replacement and shutdown path. If filter installation fails after
`RegisterHotKey` succeeds, it must immediately unregister and report failure.

`UnavailableGlobalShortcutPort` remains a safe boundary for non-Windows and
for missing Qt/event-loop sessions. It must not import Windows-only modules on
other platforms.

## Settings UI Contract

The current free-text `QLineEdit` is replaced by a dedicated keyboard-capture
control. The control is an editor only; it is not a `QShortcut` and never
registers a local key handler for application behavior.

Required behavior:

- the control is focusable and accessible by keyboard;
- a key chord is recorded from `QKeyEvent` data and rendered in canonical form;
- modifier-only, unsupported, duplicate, or multi-stroke input is rejected
  inline without touching persistence;
- Escape cancels an in-progress capture and restores the committed value;
- the committed value is visibly distinct from a pending candidate;
- a successful update shows the active canonical value and registered status;
- conflict, invalid, unavailable, and persistence errors retain the last
  committed value and show actionable inline feedback; and
- when the platform is not Windows, the control is disabled/read-only and the
  Windows-only explanation is visible.

The Settings page must subscribe to the service status. It must not infer
registration success from a completed SQLite future. The page may be destroyed
and recreated while the service and its registration remain alive.

## Persistence Contract

Use the existing SQLite `settings` singleton and `hotkeys_json` column:

- no new table;
- no new migration;
- update allow-list remains explicit and includes `hotkeys.start_stop`;
- successful values are stored as canonical strings;
- unrelated `hotkeys.cancel`, language, theme, mode, and preference mappings
  are preserved; and
- the serialized SQLite worker remains the only persistence executor.

Load behavior must distinguish missing/blank, valid, and invalid stored values:

| Stored value | Effective runtime value | User-visible result |
|---|---|---|
| missing or blank | `Ctrl+Space` | normal default/registered state on Windows |
| valid canonical or accepted legacy spelling | canonical value | normal registered state on Windows |
| invalid/corrupt | `Ctrl+Space` for the session | localized invalid-stored-value warning; do not overwrite silently |

The invalid-stored-value path is fail-closed: it never passes unchecked text to
Win32. It may be repaired by an explicit user save.

## Localization and Error Mapping

Add typed English/Russian catalog entries for the hotkey label, capture hint,
Windows-only availability, active/registered state, conflict, invalid chord,
native registration failure, restoration failure, persistence failure, and
invalid stored value. Every new key must have both locales and matching
format placeholders, consistent with `test_localization_gui.py`.

Presentation maps stable reasons to localized text. Raw Win32 error numbers,
Qt exception strings, process paths, and native key internals are not shown to
the user or written to ordinary logs. Structured diagnostics may retain a
bounded reason code and operation phase.

## Background and Application Lifecycle Integration

The following is the required integration contract for the future tray and
background work:

1. The composition root creates one hotkey service after `QApplication` exists
   and injects it into the desktop/background session owner.
2. Startup loads persisted settings through the existing asynchronous
   persistence service, resolves the effective value, and then registers once
   the recording capability is available. A window constructor must not be the
   source of truth for registration.
3. `MainWindow` and the future tray/overlay are consumers of immutable status
   snapshots. Opening, hiding, minimizing, navigating, or recreating a window
   does not change registration.
4. If microphone capability starts asynchronously, the lifecycle owner
   reconciles the pending effective setting when the capability becomes ready.
   An unavailable controller does not cause a fake or Qt fallback registration.
5. A settings change from any foreground/background surface goes through the
   same application use case and generation fence.
6. On normal application quit, stop accepting new toggle commands, unregister
   the native hotkey and remove the native event filter, then close recording,
   backend, and persistence resources. `QApplication.aboutToQuit` must cover
   shutdown paths that do not close the main window.
7. If unregister fails, log a structured failure and expose the unavailable
   status; do not hide the failure behind window disposal. Process exit remains
   the final OS cleanup boundary, but explicit unregister is mandatory.

This ordering prevents a hidden tray session from losing its global command and
prevents a callback from racing a destroyed controller or Qt event filter.

## Test and Evidence Plan

### Domain/Application tests on every platform

- default resolution is `Ctrl+Space` for missing and blank values;
- canonicalization accepts supported modifier order and rejects invalid chords;
- explicit legacy `Ctrl+Alt+Space` remains unchanged;
- start/stop dispatch covers idle-like, recording, processing, unavailable,
  and terminal states;
- registration and unregistration are idempotent and exactly once;
- replacement keeps the old registration on conflict or invalid input;
- persistence failure restores the old registration/value;
- stale asynchronous settings results cannot overwrite a newer candidate;
- unsupported-platform status never invokes a callback or Qt fallback; and
- background owner survives window disposal while shutdown unregisters once.

### Persistence/UI tests

- settings round-trip stores the canonical `hotkeys.start_stop` string and
  preserves unrelated settings;
- missing/blank settings render and use `Ctrl+Space` without a migration;
- keyboard capture records `Ctrl+Space` and another legal chord without text
  editing;
- pure modifiers, unsupported keys, and multi-stroke sequences show inline
  validation and do not submit persistence;
- conflict and persistence-error feedback preserve the prior value;
- English and Russian status/error text render for every status; and
- non-Windows UI is explicitly disabled/read-only and never claims global
  registration.

### Infrastructure tests

Use an injected fake `user32` and native-event filter for portable deterministic
tests of:

- modifier/key mapping, including `Ctrl+Space`;
- `RegisterHotKey` success and `ERROR_HOTKEY_ALREADY_REGISTERED` conflict;
- `WM_HOTKEY` dispatch only for the owned ID;
- filter installation rollback;
- exactly-once `UnregisterHotKey` and filter removal; and
- non-Windows adapter selection without importing Win32 APIs.

These tests prove adapter control flow, not Windows behavior.

### Required Windows evidence

A Windows desktop-session smoke lane must prove, with the actual packaged or
runtime application:

- `Ctrl+Space` registers and toggles recording while another application owns
  focus;
- the hotkey works with VoiceInk minimized/hidden and, when implemented, in
  the system tray;
- an independently owned `Ctrl+Space` is reported as a conflict without
  replacing the previous valid registration;
- a changed hotkey releases the old combination and activates the new one;
- shutdown releases the registration and a subsequent process can claim it;
- unsupported/invalid combinations are rejected without crashing; and
- repeated start, replacement, hide/show, and close cycles do not leak native
  registrations or callbacks.

The evidence must name the Windows version, architecture, Python/Qt/package
mode, desktop-session type, owner process, repetition count, and sanitized
result codes. A macOS/Linux run, an offscreen Qt test, or a `windows-latest`
run without a real interactive desktop session is not evidence of global
delivery.

## Implementation Slices After Approval

These slices are sequencing guidance, not authorization to implement while this
RFC is Draft:

1. Amend Domain/Application contracts for default, grammar, stable status
   reasons, transactional replacement, and lifecycle ownership.
2. Add the persistence coordinator and default/invalid-value handling without a
   schema migration.
3. Replace the Settings text field with the keyboard-capture control and add
   English/Russian status feedback.
4. Move registration ownership from `MainWindow` to the desktop/background
   lifecycle owner while preserving the Win32 adapter boundary.
5. Harden and test the Windows adapter, including rollback after partial native
   setup.
6. Run portable tests, then the required interactive Windows evidence lane.

## Acceptance Criteria

- **GHK-AC-001** - New or blank settings resolve to `Ctrl+Space`; explicit
  persisted choices remain explicit and are not silently rewritten.
- **GHK-AC-002** - Windows registration uses Win32 `RegisterHotKey` and
  `WM_HOTKEY`; no Qt-only focused shortcut is used for global behavior.
- **GHK-AC-003** - A captured chord is validated before persistence and stored
  in canonical form through the existing SQLite settings path.
- **GHK-AC-004** - Conflict, invalid input, native failure, persistence
  failure, and restoration failure are typed, localized, visible, and
  non-fatal.
- **GHK-AC-005** - A failed candidate leaves the previous persisted and active
  value intact, or visibly reports that restoration could not be proven.
- **GHK-AC-006** - Exactly one service owns registration; window/tray lifetime
  changes do not unregister it, while application shutdown does.
- **GHK-AC-007** - Non-Windows behavior is explicitly unsupported, has no Qt
  fallback, and never claims global registration.
- **GHK-AC-008** - Portable unit/UI/persistence tests cover the behavior matrix,
  and an interactive Windows smoke lane proves unfocused/background delivery,
  conflict, replacement, and cleanup.

## Open Questions

The product decisions in D1-D6 are fixed for implementation planning. The
remaining questions are evidence/operational details only:

- Which maintained Windows CI or acceptance machine can provide an interactive
  desktop session rather than a headless runner?
- Will the future tray owner expose an explicit `hide-to-background` state, or
  only `QApplication.aboutToQuit` plus a tray menu? The registration contract
  is the same either way.
- Which recording-capability readiness signal will the approved microphone
  work expose to the background lifecycle owner?

Until these operational details are named and this RFC is approved, work is
limited to review, decision synchronization, and evidence planning.
