# RFC: Windows Tray Background Lifecycle

## Status

Status: Draft. This commit records the design only; it does not authorize an
implementation. The implementation must be a separate change after this RFC is
approved. This RFC is intentionally not added to `spec/catalog.yaml` because
the requested change is RFC-only.

## Summary

The packaged Windows shell must behave as a tray application rather than as a
short-lived window process:

- closing the main window hides it to the Windows notification area;
- the Qt event loop, composition, global shortcut registration, and background
  services remain alive while the window is hidden;
- the existing global start/stop shortcut continues to be owned by the session
  and remains registered while the shell is hidden;
- the tray menu provides the explicit, clean exit path;
- duplicate launches do not create a second tray icon, shortcut registration, or
  composition; and
- the user-facing PyInstaller executable remains a GUI-subsystem executable,
  so packaged launch does not flash a temporary terminal window.

The macOS VoiceInk reference uses the same product distinction: a menu-bar
presence is persistent, closing the user-facing window does not terminate the
application, and termination is an explicit menu action. The Windows version
uses `QSystemTrayIcon` and Windows lifecycle semantics rather than copying the
Swift implementation.

## Current Evidence

The current repository has the following lifecycle seams and gaps:

- `presentation.app._run_session()` owns the composition only until
  `QApplication.exec()` returns, which is the correct outer ownership point for
  a tray session.
- `MainWindow.closeEvent()` currently calls `dispose()` and accepts the close.
  `dispose()` unregisters the global shortcut, disposes page resources, and
  may close history deletion. With the default Qt last-window policy this ends
  the session instead of hiding it.
- `GlobalToggleShortcutService` owns one registration and its native adapter
  installs a Qt native event filter on `QApplication`. The registration is
  currently created and destroyed indirectly by `MainWindow` construction and
  disposal.
- `desktop_composition._DesktopComposition.close()` is synchronous and
  idempotent, drains the transcribe controller, closes the backend, history
  deletion, and persistence. It must run only for terminal shutdown, not for a
  user-requested hide.
- `scripts/frontend_build.py` already builds `voiceink-shell.exe` with
  PyInstaller `--windowed` and `voiceink-shell-smoke.exe` with `--console`.
  `scripts/frontend_package_smoke.py` checks the PE subsystem. This distinction
  must remain a regression contract.
- The portable package also writes `voiceink-shell.cmd`. A command-script
  launcher is a likely source of a transient console when launched from
  Explorer; the canonical user launch must therefore be the GUI executable,
  not the command script.

## macOS Reference

The reference is the public `Beingpax/VoiceInk` repository at commit
`c09cc1f677f40f2ee665843a61f07670210f012e`:

- [`AppDelegate.swift`](https://github.com/Beingpax/VoiceInk/blob/c09cc1f677f40f2ee665843a61f07670210f012e/VoiceInk/App/Lifecycle/AppDelegate.swift)
  returns `false` from
  `applicationShouldTerminateAfterLastWindowClosed`, so closing the last user
  window does not terminate the process.
- [`VoiceInk.swift`](https://github.com/Beingpax/VoiceInk/blob/c09cc1f677f40f2ee665843a61f07670210f012e/VoiceInk/App/VoiceInk.swift)
  declares a persistent `MenuBarExtra` alongside the main window scene.
- [`MenuBarManager.swift`](https://github.com/Beingpax/VoiceInk/blob/c09cc1f677f40f2ee665843a61f07670210f012e/VoiceInk/App/MenuBar/MenuBarManager.swift)
  hides the main window when operating in menu-bar-only mode and restores the
  accessory activation policy after the user-facing window closes.
- [`MenuBarView.swift`](https://github.com/Beingpax/VoiceInk/blob/c09cc1f677f40f2ee665843a61f07670210f012e/VoiceInk/App/MenuBar/MenuBarView.swift)
  exposes `Quit VoiceInk` as an explicit menu action that calls application
  termination.

These references establish behavior, not a Windows API or UI requirement.

## Goals

- Define one deterministic session owner for the window, tray icon, global
  shortcut service, single-instance lease, and composition.
- Make close-to-tray and tray-to-window transitions explicit and testable.
- Keep the global shortcut alive while the main window is hidden, without
  registering it twice or coupling its lifetime to a widget.
- Provide a clean, idempotent terminal shutdown path from the tray and from Qt
  application termination notifications.
- Prevent duplicate background processes and duplicate tray icons in the normal
  packaged launch path.
- Preserve the existing dependency direction:
  `Presentation -> Application -> Domain <- Infrastructure`.
- Keep the user-facing executable windowless at the Windows PE subsystem level
  while retaining a separate console executable for CI smoke tests.
- Define acceptance evidence that separates cross-platform tests from real
  Windows desktop validation.

## Non-Goals

- Implementing microphone/WASAPI capture or making the currently unavailable
  recording capability available.
- Adding a second hotkey, push-to-talk, hotkey configuration migration, or a
  new native shortcut protocol.
- Replacing the existing Win32 `RegisterHotKey` adapter.
- Turning the tray into a second full application UI. The tray is a lifecycle
  and activation surface; feature settings remain in `MainWindow`.
- Implementing startup-at-login, an installer, auto-update, service mode, or a
  Windows service.
- Supporting multiple concurrent user sessions with one shared global mutex.
- Hiding a console-enabled executable as a substitute for building the GUI
  executable with PyInstaller `--windowed`.
- Adding an IPC activation channel for a second launch. A later RFC may make a
  second launch activate the already-running window; this RFC only requires the
  second launch to exit without creating another session.

## Proposed Architecture

### Ownership boundary

Introduce one presentation-side session coordinator, named
`TrayLifecycleCoordinator` in this RFC. The name is descriptive; the final
module name may be selected during implementation without changing the
contract.

```text
presentation.app
    -> TrayLifecycleCoordinator
       -> MainWindow                  (borrowed view)
       -> QSystemTrayIcon              (owned tray surface)
       -> GlobalToggleShortcutService  (owned application service)
       -> SingleInstanceLease          (owned infrastructure resource)
       -> DesktopComposition            (owned composition resource)
```

Responsibilities:

| Owner | Owns | Must not own |
|---|---|---|
| `TrayLifecycleCoordinator` | Session state, hide/show policy, tray actions, shutdown intent, one shortcut service, single-instance lease, terminal cleanup ordering | SQLite internals, ASR protocol, Windows hotkey implementation details |
| `MainWindow` | Qt widgets, page subscriptions, user-facing rendering, borrowed shortcut update calls | Tray icon, single-instance lease, composition lifetime, shortcut unregister |
| `GlobalToggleShortcutService` | Exactly one typed registration and callback routing to the recording controller | Tray visibility, process exit, window creation |
| `WindowsGlobalShortcutPort` | Win32 registration, native event filter, native unregister | Application state, tray actions, persistence |
| `DesktopComposition` | Imported-media bootstrap, transcribe controller, backend, history cleanup, persistence | Window hiding, tray menu, single-instance policy |
| `QSystemTrayIcon` adapter | Notification-area icon, menu, activation signal | Composition shutdown implementation |
| `SingleInstanceLease` | One process lease for the interactive VoiceInk shell | Hotkey conflict handling or user data |

The coordinator may pass the shortcut service to `MainWindow` as a borrowed
dependency so Settings can call `update_shortcut()`. `MainWindow.dispose()`
must not unregister that service; only the coordinator owns registration
cleanup. This prevents a user close event from disabling background hotkeys.

The coordinator is a session orchestrator, not a new domain state machine. The
existing `ShellController` remains the authority for recording state, and the
existing shortcut service remains the authority for shortcut registration
status and callback routing.

### Runtime lifecycle states

The coordinator exposes a small state model for tests and guards; the names are
conceptual and need not become public domain values:

| State | Meaning | Allowed terminal action |
|---|---|---|
| `Starting` | Lease, composition, tray, window, and shortcut are being assembled | Fail startup and release everything already acquired |
| `Visible` | Main window is shown; tray and session resources are alive | Hide, show again, or request shutdown |
| `Hidden` | Main window is hidden; tray, event loop, composition, and shortcut remain alive | Show again or request shutdown |
| `ShutdownRequested` | A terminal request has been accepted; repeated requests are no-ops | Transition once to shutdown |
| `ShuttingDown` | User-facing controls are quiescing and resources are released in order | Wait for the event loop to return |
| `Terminated` | Shortcut is unregistered, composition is closed, lease is released | None |
| `Rejected` | Another instance owns the lease, or startup cannot establish a safe tray session | Exit without creating a background session |

The normal transitions are:

```text
Starting -> Visible
Starting -> Rejected
Starting -> ShuttingDown -> Terminated   (partial-start failure)
Visible  --main-window close--> Hidden
Hidden   --tray Show--> Visible
Visible  --tray Quit / Qt quit--> ShutdownRequested
Hidden   --tray Quit / Qt quit--> ShutdownRequested
ShutdownRequested -> ShuttingDown -> Terminated
```

The main-window close event has two different meanings:

1. With a tray available and no terminal shutdown intent, the coordinator hides
   the window, the event is ignored, and the state becomes `Hidden`.
2. During terminal shutdown, the coordinator marks the shutdown intent first;
   the window may then accept its close as part of cleanup. `dispose()` is still
   called from the single shutdown owner, not as an accidental side effect of
   the ordinary close event.

`QApplication.setQuitOnLastWindowClosed(False)` must be set for this session.
Hiding the only top-level window must not cause `QApplication.exec()` to return.
The tray icon and any required background workers keep the event loop alive.

### Window and tray behavior

The tray adapter uses the existing application icon and creates one
`QSystemTrayIcon` for the session. Its menu contains at least:

- `Show VoiceInk` — show, raise, and activate the existing main window;
- a recording status/toggle action only if the current recording capability and
  controller support it; and
- `Quit VoiceInk` — request terminal shutdown through the coordinator.

Clicking or activating the tray icon shows the existing window; it does not
construct a second `MainWindow`. The coordinator keeps a single window
reference for the session. Tray menu actions are disabled or rendered as
unavailable when the controller reports the existing unavailable capability;
the tray itself remains usable for `Show` and `Quit`.

If `QSystemTrayIcon.isSystemTrayAvailable()` is false, the coordinator must not
hide the last window. It keeps the window visible and treats a close request as
terminal shutdown, because silently hiding a process with no discoverable exit
surface would be unsafe. Windows desktop validation must use a session with a
working notification area.

### Global shortcut lifetime and monitoring

The coordinator creates one `GlobalToggleShortcutService` after the composition
controller exists and before the session becomes interactive. It registers it
once and keeps the returned `GlobalShortcutRegistration` alive in both
`Visible` and `Hidden` states. The existing Win32 native event filter therefore
continues receiving `WM_HOTKEY` messages while the main window is hidden.

The callback continues to call the existing controller start/stop transition;
it does not depend on a visible widget and does not create a second recording
service. A shortcut callback received during a hidden state must be delivered
through the Qt event loop and must not directly manipulate tray widgets from a
foreign thread.

Settings updates continue to use the existing persisted `hotkeys.start_stop`
value and `update_shortcut()` replacement behavior. A replacement unregisters
the old native registration before registering the new one. Conflict and
unavailable status remains typed and non-fatal.

The current service deliberately refuses native registration when the
controller is `UNAVAILABLE`. This RFC does not change that capability policy:
background hotkey monitoring is acceptance-tested with an enabled controller
and the real Windows port, while the current unavailable build continues to
report the existing unavailable status rather than pretending that recording
works.

Registration and state transitions are `O(1)` in application-owned state and
`O(1)` additional space. Native registration, Qt event dispatch, and controller
listener work are excluded from that bound.

### Terminal shutdown

Every terminal path funnels through one idempotent coordinator operation:

1. transition `Visible` or `Hidden` to `ShutdownRequested`; reject repeated
   requests;
2. stop accepting new tray/window actions and mark shutdown intent;
3. ask the Qt application to leave the event loop, or accept an already pending
   `aboutToQuit` path;
4. unregister the global shortcut and remove its native event filter;
5. dispose the main window and its presentation subscriptions exactly once;
6. stop the tray icon and release the tray menu;
7. call `DesktopComposition.close()` exactly once so bootstrap, transcription,
   backend, history cleanup, and SQLite close under their existing idempotent
   contracts; and
8. release the single-instance lease last, then return the original Qt exit
   code.

The coordinator must not call `DesktopComposition.close()` for a hide. It must
also not let a cleanup exception replace the original startup or event-loop
exception. Cleanup failures are logged using the existing project logging
policy and remain visible in diagnostics; the resource that failed remains
retryable where its existing contract permits it.

An active recording or transcription must follow the existing application and
composition cancellation/drain contract. This RFC does not add a second
shutdown implementation or silently discard an in-flight result.

Shutdown is `O(1)` coordinator state work plus the existing bounded resource
drain. Additional coordinator memory is `O(1)`; resource memory and worker
drain costs are governed by their existing contracts.

### Single-instance lease

The interactive packaged shell must acquire a stable per-user/per-interactive
session lease before building the composition, tray, or global shortcut. A
small infrastructure port keeps the coordinator independent of Win32 details;
the Windows adapter should use a named `Local\VoiceInk-Win` mutex (or an
equivalent atomic user-scoped lease) and report whether the lease was acquired.

Requirements:

- the second launch exits cleanly with no second tray icon, no composition, no
  database worker, and no `RegisterHotKey` attempt;
- the lease is released after all other resources have closed;
- a crash cannot permanently block relaunch; the kernel-owned mutex naturally
  satisfies this better than a manually deleted marker file;
- the lease identity is stable across source and packaged launches of the same
  user-facing product; smoke executables must use a separate identity or remain
  excluded from interactive singleton validation; and
- the first implementation does not need to activate the existing window from
  a second launch. That IPC/activation enhancement is explicitly deferred.

Acquisition and release are `O(1)` time and `O(1)` space, excluding the native
kernel operation.

### Packaging and launch contract

The build contract remains deliberately split:

| Artifact | PyInstaller mode | Purpose | User-facing |
|---|---|---|---|
| `voiceink-shell.exe` | `--onefile --windowed` | Installed/portable tray application | Yes |
| `voiceink-shell-smoke.exe` | `--onefile --console` | Offscreen CI smoke test | No |

The GUI artifact must continue to be validated as an x64 PE32+ image with
Windows subsystem `2` (`IMAGE_SUBSYSTEM_WINDOWS_GUI`). The smoke artifact may
remain subsystem `3` and is never the launch target in documentation, a
shortcut, or a package association.

The portable package can retain package-relative runtime discovery because the
application already resolves `voiceink-package.json` beside
`sys.executable`. The user-facing launch path must invoke
`voiceink-shell.exe` directly. The existing `.cmd` wrapper must either be
removed from the user launch path or explicitly documented as a developer/CLI
helper; it must not be the Explorer shortcut target if it causes a console
flash. No implementation may switch the GUI artifact to `--console` and hide
the resulting window as a workaround.

The packaging implementation must preserve the current runtime assets, icon,
migrations, and package descriptor. This RFC changes the launch/lifecycle
contract only; it does not change artifact trust or runtime discovery.

## Acceptance Tests

These tests are part of the later implementation change. This RFC adds no code
or tests.

### Cross-platform/unit and offscreen tests

- A fake tray, window, shortcut service, composition, and instance lease prove
  that closing a tray-capable window ignores the close event, hides the same
  window, does not call `dispose()`, does not close the composition, and leaves
  the shortcut registered.
- Showing from the tray restores the same window instance, raises it, and
  transitions `Hidden -> Visible` without constructing a second window.
- Tray Quit from both `Visible` and `Hidden` requests shutdown exactly once;
  repeated Quit and `aboutToQuit` notifications do not duplicate unregister,
  window disposal, tray disposal, composition close, or lease release.
- Cleanup ordering is asserted as shortcut unregister, window/tray disposal,
  composition close, then lease release. The original event-loop exception is
  preserved when cleanup also fails.
- A hidden-session hotkey callback reaches the controller and toggles the same
  recording state as a visible-session callback. No widget lookup is required.
- Shortcut replacement while visible or hidden unregisters the old registration
  before registering the new one; conflict and unavailable statuses remain
  non-fatal.
- `QApplication` is configured not to quit when the last window is hidden.
- A missing tray causes close to take the safe terminal path instead of leaving
  an undiscoverable background process.
- A failed or conflicting instance lease prevents composition, tray, shortcut,
  and window creation. A partial-start failure releases the lease and all
  already-acquired resources.
- Existing composition lifecycle tests continue to prove that a hide does not
  call `DesktopComposition.close()` and that terminal shutdown still calls it
  once.

### Packaging contract tests

- Source-level build tests assert that the GUI command contains
  `--windowed`, the smoke command contains `--console`, and both retain the
  expected runtime data and icon inputs.
- PE validation asserts x64 PE32+ plus subsystem `2` for
  `voiceink-shell.exe` and subsystem `3` only for `voiceink-shell-smoke.exe`.
- Package tests assert that the documented/canonical launch target is the GUI
  executable and that no user-facing shortcut or package instruction points to
  the console smoke artifact.
- A packaged runtime started from a path containing spaces still discovers the
  package descriptor and starts without requiring a command-shell environment
  variable.

## Windows-Only Validation

Cross-platform tests cannot prove notification-area behavior, Win32 mutex
ownership, `RegisterHotKey` delivery, or the absence of a console window. The
implementation is not complete until a real Windows 10 22H2 or Windows 11
x64 interactive-desktop validation has passed:

1. Build with the pinned environment using `make build` on `windows-2022` or a
   supported local Windows x64 host. Confirm the GUI PE subsystem and launch
   the direct `voiceink-shell.exe`, not the smoke executable or `.cmd` helper.
2. Start the packaged GUI by double-click/Explorer and from PowerShell's
   `Start-Process`. Confirm no terminal window flashes, the app has one tray
   icon, and the process remains alive after the main window is closed.
3. Open the tray menu, select Show, and confirm the original window returns;
   repeat close/show several times and confirm there is still one process and
   one tray icon.
4. With an enabled recording test configuration, register a dedicated test
   shortcut through the real Win32 adapter. Trigger it while the window is
   visible and hidden, confirm the same start/stop callback is delivered, then
   exit and confirm the shortcut can be registered by a second test process.
5. Launch a second copy while the first is hidden. Confirm it exits without a
   second tray icon, hotkey attempt, or composition/database ownership.
6. Select `Quit VoiceInk` from the tray while visible and hidden. Confirm the
   tray icon disappears, the process exits, the native shortcut is released,
   and a subsequent launch succeeds without a stale singleton or hotkey
   conflict.
7. Exercise Windows logoff/shutdown or application termination notification and
   confirm the same cleanup path runs without leaving a child runtime process.
8. Run the existing relocation/package smoke after the lifecycle checks. The
   smoke executable remains a console/offscreen CI binary and is not evidence
   for GUI tray behavior.

The Windows evidence must record OS build, Python version, x64 architecture,
PySide6/PyInstaller pins, artifact names, launch method, and the observed
process/tray/shortcut results. macOS tests may validate fake lifecycle seams
and Qt offscreen behavior but must not be reported as Windows evidence.

## Exit Criteria

This RFC can move to Approved when:

- the lifecycle states, close-versus-quit semantics, and ownership table are
  accepted;
- the existing global shortcut service remains the only registration owner,
  with its lifetime moved from `MainWindow` to the session coordinator;
- the single-instance policy and no-IPC v1 limitation are accepted;
- the direct GUI-executable launch contract is accepted, including the
  `--windowed`/`--console` split and `.cmd` risk treatment; and
- all open questions below have an explicit decision or are recorded as
  implementation constraints.

Implementation completion additionally requires all acceptance tests and the
Windows-only validation matrix to pass. `make check` alone is not sufficient
evidence for tray, Win32 shortcut, singleton, or console-subsystem behavior.

## Open Questions

1. Should the tray menu expose a recording toggle in this slice, or only Show
   and Quit until microphone capability is enabled? The default recommendation
   is to expose the toggle only when the controller is available.
2. Should the second launch remain a silent no-op, or should a follow-up IPC
   channel activate the existing window? The RFC currently chooses silent exit
   for the smallest safe v1.
3. What exact user-visible copy and localization keys should be used for tray
   actions, unavailable recording, and the single-instance rejection? The
   lifecycle contract does not prescribe English/Russian wording.
4. Does the portable package need a shell helper for environment setup after
   direct executable launch is made canonical? Current packaged runtime
   discovery indicates that the adjacent descriptor makes the helper
   unnecessary for the normal launch path.
5. What deadline and user-visible reporting should apply when an active capture
   or transcription does not drain during terminal shutdown? The implementation
   must use the existing bounded close contracts until a dedicated shutdown
   reliability RFC changes them.

## Risks

- **Hotkey capability gap:** the current service intentionally does not register
  when the controller is `UNAVAILABLE`; tray lifecycle work alone cannot prove
  end-to-end recording hotkeys until an enabled Windows recording composition
  exists.
- **Close semantics regression:** any path that calls `MainWindow.dispose()` or
  lets Qt quit on the last hidden window will silently disable hotkeys or close
  persistence. The coordinator must be the only terminal owner.
- **Native cleanup ordering:** unregistering `RegisterHotKey` after the Qt event
  filter or application is gone can leave a conflict until process teardown.
  Unregister before disposing the application-owned event-loop resources.
- **Launcher mismatch:** the existing `.cmd` package helper can flash a
  terminal even when the embedded executable is windowed. Direct GUI launch
  must be the documented and tested path.
- **Tray availability:** remote sessions, shell restarts, notification-area
  policy, or platform quirks can make the tray unavailable. The safe fallback
  must be visible-and-terminal, never invisible-and-running.
- **Duplicate process behavior:** a mutex prevents two sessions but does not
  activate the first session. Users may interpret a second launch that exits
  silently as a failed start until a future activation channel or notification
  is added.
- **Shutdown during work:** forced process termination, logoff, or a short
  drain deadline could interrupt an active capture/transcription. Evidence must
  verify the existing cancellation and child-process cleanup contracts rather
  than treating tray disappearance as sufficient.
