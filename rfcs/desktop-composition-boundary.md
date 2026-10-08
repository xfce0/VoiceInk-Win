# RFC: Desktop Composition Boundary Without a Synthetic Runtime

## Status

Draft. This document is architecture-only. It authorizes no production
implementation until it is approved.

The RFC is intentionally the only file changed by this draft commit. Before
approval, add `rfcs/desktop-composition-boundary.md` to `spec/catalog.yaml` as
`kind: rfc`, `status: draft`. That catalog change is a required follow-up and
is not part of this RFC-only commit.

## Summary

The packaged PySide6 shell currently constructs
`ShellController(FakeShellBackend())`. The shell therefore presents a
successful recording flow and a fixed transcript even though microphone
capture and production ASR wiring are not implemented. This is a false
success, not a useful demo boundary.

This RFC defines the smallest safe correction:

- the production desktop composition creates an explicitly unavailable shell;
- injected fake backends remain available only to focused tests and future
  adapter tests;
- the desktop composition becomes the owner of the controller and future
  capture/runtime resources;
- presentation shutdown is ordered, idempotent, and does not hide cleanup
  failures; and
- this slice adds no microphone, ASR, native-runtime, or imported-media
  behavior.

The production path must not construct a fake backend, ASR composition root,
microphone adapter, model, or native sidecar merely to open the desktop shell.

## Problem and Evidence

### User-visible problem

The current frontend feature describes a demo session as if it were recording
capability. A packaged user can press the recorder action and receive the
fixed text `VoiceInk is ready for your next thought.` without a microphone,
audio session, or ASR runtime. The UI therefore claims a capability that the
artifact cannot fulfill.

The constitution requires explicit platform boundaries and visible failure.
The runtime feature requires setup/runtime failures rather than synthetic
success. A missing capability must consequently be represented as unavailable,
not as a successful transcript and not as an opaque backend error.

### Repository evidence

The current production path is:

```text
scripts/frontend_entrypoint.py
  -> voiceink_win.presentation.app.main
     -> ShellController(FakeShellBackend())
        -> fixed TranscriptResult
```

The repository confirms the following:

- `src/voiceink_win/presentation/app.py` imports `FakeShellBackend` from the
  infrastructure barrel and constructs it on every shell launch.
- `src/voiceink_win/infrastructure/fake_shell.py` returns the fixed transcript
  unless a test-configured error is supplied.
- The installed `voiceink-win-shell` entry point and
  `scripts/frontend_entrypoint.py` both delegate to `presentation.app.main`;
  the fake is therefore reachable from packaged startup, not only from tests.
- `ShellController` requires a backend and starts in `IDLE`. `ShellState` has
  no capability-absent value, and `ShellSnapshot` has no unavailable invariant.
- `MainWindow` and `FloatingRecorderWindow` map `IDLE` to copy such as
  `Ready`, `Start recording`, and `Your first transcript will appear here
  after you record.` There is no unavailable mapping.
- `MainWindow.closeEvent()` disposes the child and subscription directly.
  `presentation.app.main` has no composition owner or `try/finally` spanning
  window construction and `application.exec()`.
- The application-owned theme callback in `main` captures `window` and is not
  disconnected by the window lifecycle.
- `voiceink_win.composition` is an existing ASR composition root that reads
  runtime configuration and owns native-sidecar lifecycle. It is not a
  microphone composition and must remain outside this slice.
- `spec/features/004-frontend-shell.md` excludes microphone capture and real
  ASR wiring but still specifies a fake-backed demo session. That is the
  specification contradiction this RFC resolves.
- `spec/features/003-parakeet-runtime-integration.md` explicitly forbids
  turning runtime failure into synthetic successful text.

## Goals

- Remove fake construction from every production desktop entrypoint and its
  default transitive composition path.
- Represent the absence of microphone capture and ASR as a stable,
  user-visible capability state.
- Establish one desktop composition boundary for future capture, workers,
  transport, runtime, and shutdown ownership.
- Keep `ShellController` independent of Qt, subprocesses, HTTP, Windows APIs,
  and native runtime details.
- Preserve explicit backend injection as a focused test/future-adapter seam;
  do not make it an argument to the production desktop builder.
- Keep `voiceink_win.composition` and the existing imported-media/ASR runtime
  behavior unchanged.
- Define cleanup ordering and failure precedence before external resources are
  introduced.

## Non-Goals

- Microphone, WASAPI, global-hotkey, permission, or audio-device support.
- Connecting the desktop shell to `AsrApplicationService` or
  `BackendApplication`.
- Device selection, canonical audio capture, model loading, runtime discovery,
  sidecar startup, or network requests.
- A fake fallback, environment-controlled demo mode, automatic runtime
  discovery, or a synthetic transcript in the production path.
- Changes to imported-media behavior, the ASR runtime composition, manifests,
  packaging policy, or native smoke behavior beyond the tests needed to prove
  this boundary.
- Tray, close-to-tray, history, persistence, text delivery, or installer
  behavior.
- Claiming that recording will become available in the current build.

`FakeShellBackend` remains a test fixture only. The implementation migration
must move it to `tests/support/fake_shell.py`, remove it from the packaged
`src/voiceink_win` surface and infrastructure barrel, and update enabled
controller tests. A manual demo requires a separately named non-production
harness and is outside this RFC.

## Proposed Architecture

### Boundary and dependency direction

The dependency direction remains:

```text
Presentation -> Application -> Domain <- Infrastructure
                     ^
                     |
          desktop composition root
```

The desktop composition root is an outer adapter. It wires application and
domain objects; Qt widgets do not select concrete infrastructure adapters.
The default unavailable path imports neither the infrastructure barrel nor the
ASR composition root.

The new package-level module is `voiceink_win.desktop_composition`. Its
conceptual public surface is deliberately narrow:

```python
class DesktopComposition(Protocol):
    @property
    def controller(self) -> ShellController: ...

    def close(self) -> CleanupResult: ...


def build_desktop_composition() -> DesktopComposition: ...
```

`CleanupResult` belongs to the application layer, for example
`voiceink_win.application.cleanup`, so both presentation and the outer
composition root depend on an application-owned value rather than on one
another. Its conceptual immutable shape is:

```python
@dataclass(frozen=True, slots=True)
class CleanupFailure:
    code: str
    error_type: str


@dataclass(frozen=True, slots=True)
class CleanupResult:
    failures: tuple[CleanupFailure, ...] = ()
```

`CleanupFailure` contains only stable allowlisted codes and safe normalized
exception type names. The implementation uses these stable codes for
`desktop.cleanup.theme`, `desktop.cleanup.window_subscription`,
`desktop.cleanup.recorder`, `desktop.cleanup.composition`, and
`desktop.cleanup.construction_rollback`. It never stores an exception object,
raw message, path, or other sensitive value. The owners cache their first
result; a later close/dispose returns that result without repeating cleanup.

`build_desktop_composition()` has no backend override, environment switch, or
fallback callback. This prevents an exception handler, missing runtime, or
untrusted command-line option from selecting a fake in production.

Focused controller tests may continue to construct
`ShellController(fake_backend)`. If composition lifecycle tests need factories
or spies, those seams are private/test-only helpers, not production builder
arguments.

### Minimal implementation slice

After approval, implement exactly this coherent slice:

1. Add `ShellState.UNAVAILABLE` and extend `ShellSnapshot` invariants:
   unavailable snapshots have empty transcript and error fields; only
   `TRANSCRIPT_READY` may contain transcript text; only `ERROR` may contain an
   error message.
2. Add an explicit unavailable construction path, preferably
   `ShellController.unavailable()`. Keep the enabled constructor explicit and
   backend-requiring; do not make its backend optional and do not add an
   `UnavailableShellBackend` that pretends to be a runtime.
3. Add `voiceink_win.desktop_composition` with a no-resource default
   composition that owns the unavailable controller and an idempotent
   `close()`.
4. Refactor `presentation.app.main` to obtain the controller only from the
   desktop composition. It must close the window and composition through one
   `try/finally` lifecycle, including construction and event-loop failures.
5. Add the unavailable mapping to `MainWindow` and
   `FloatingRecorderWindow`: explanatory copy, disabled recorder action,
   accessible name/description, and an inactive waveform with no running
   animation timer.
6. Move `FakeShellBackend` to `tests/support/fake_shell.py`, remove its
   infrastructure export, and update enabled-controller tests.
7. Update the affected frontend feature specification and add the tests in
   this RFC. Those implementation files are intentionally not changed by this
   draft.

The resulting production flow is:

```text
presentation.app.main
  -> build_desktop_composition()
     -> ShellController.unavailable()
  -> MainWindow(composition.controller)
  -> Qt event loop
  -> MainWindow.dispose()
  -> DesktopComposition.close()
```

No step creates a microphone, timer for recording, ASR runtime, model,
sidecar, process, or network endpoint. A Qt object may exist for rendering,
but no unavailable-state animation or processing timer may be running.

### Unavailable backend behavior

`UNAVAILABLE` means that this build does not contain the capture/ASR capability.
It is not a failed transcription request. The production controller owns no
backend object in this state.

| Action | Result | Snapshot | Side effect |
|---|---|---|---|
| Construct unavailable controller | succeeds | `UNAVAILABLE` | none |
| `start_recording()` | `False` | unchanged | none |
| `stop_recording()` | `False` | unchanged | none |
| `complete_processing()` | `False` | unchanged | none |
| `reset()` | idempotent no-op | remains `UNAVAILABLE` | no publish, timer, or call |

Unavailable actions must not publish `RECORDING`, `PROCESSING`, `EMPTY`, or
`ERROR`, and must not invoke a backend. Controller actions are O(1), excluding
listener notification. A normal publish is O(L) time and O(L) transient space
for the defensive listener snapshot, where L is the listener count. Unavailable
no-op actions use O(1) time and O(1) space.

The injected-backend constructor retains the current enabled transitions and
listener behavior for tests and future real adapters. If a later build has a
capture adapter but its runtime is misconfigured, that is a different state
and error decision; it must not be silently mapped to `UNAVAILABLE`.

### Required unavailable UI contract

The dashboard and recorder must make capability absence unambiguous:

- state pill: **Recording unavailable**;
- hero headline: **Recording is unavailable in this build.**;
- hero detail: **Microphone capture and ASR are not included.**;
- page subtext: **Recording cannot start because microphone capture and ASR are
  not included.**;
- transcript text: **Transcripts are unavailable because recording and ASR are
  not included.**;
- transcript metadata: **Capability unavailable**;
- recorder status: **Unavailable**;
- recorder action disabled with an accessible name/description explaining that
  microphone capture is not connected;
- waveform inactive, with no animation timer running.

The dashboard may keep **Open recorder** enabled only as an explanatory panel
action. It must not imply that recording can start. The unavailable path must
not render `Ready`, `Listening`, `Transcribing`, progress, success timestamps,
`Start recording`, `Start again`, `Try again`, `Working...`, or a transcript.
It must also replace the current static `Record a thought, then let VoiceInk
turn it into clear text.` and `No sessions yet` strings, and must not render
`Ready for your voice` in the production unavailable state. The labels must be
state-rendered fields rather than immutable local construction-time text.

### Lifecycle ownership

Ownership is explicit:

1. `QApplication` owns the Qt event loop.
2. `presentation.app.main` owns `DesktopComposition` for the entire event-loop
   session and initiates shutdown.
3. `DesktopComposition` owns `ShellController` and every future capture,
   worker, transport, runtime, or backend resource created by this boundary.
4. `MainWindow` owns `FloatingRecorderWindow`, its controller subscriptions,
   and its theme subscription.
5. `ShellController` owns only an in-memory snapshot and listener registrations;
   it never owns Qt objects, threads, subprocesses, native handles, or runtime
   state.

Construction and cleanup rules:

- `MainWindow` construction is transactional. The controller subscription,
  recorder construction, theme subscription, and initial render are either all
  established or all rolled back. The original construction error remains the
  primary error.
- The theme signal is passed into `MainWindow` as an optional constructor
  dependency. `MainWindow` creates a named callback and stores the exact
  signal/callback pair needed for disconnection; `main` must not retain an
  anonymous callback that captures the window. Theme connection is part of the
  transactional construction and rollback sequence.
- `FloatingRecorderWindow` follows the same rule for controls, waveform and
  timers, controller subscription, and initial render. Partial resources are
  released if a later step fails.
- `MainWindow.dispose()` is idempotent and uses this order: disconnect theme,
  unsubscribe the main controller, dispose the recorder. Every step runs even
  if an earlier step fails.
- Recorder disposal stops processing and waveform timers, unsubscribes from the
  controller, then hides/releases the child. Disposal does not call
  `controller.reset()` or publish a snapshot. User-triggered dismiss/reset is a
  separate live-window action.
- `MainWindow.closeEvent()` delegates to the same cached disposal operation.
  The event handler cannot communicate an exit code to Qt; `main` inspects the
  cached `CleanupResult` in its `finally` block.
- `DesktopComposition.close()` runs after presentation disposal, is idempotent,
  and currently has no resources to release. Its ownership contract is
  established now so future resources do not acquire ad hoc widget cleanup.
- `build_desktop_composition()` is transactional for future resource
  acquisition: if a later owned resource fails to initialize, already-created
  resources are closed in reverse acquisition order. Rollback failures are
  aggregated under the same cleanup policy, while the construction exception
  remains primary.
- The entrypoint `try/finally` spans window construction, `show()`, and
  `application.exec()`. Cleanup runs for normal return, construction failure,
  and event-loop exception.

All cleanup failures are collected from window disposal, composition close, and
construction rollback, then safely logged even when a primary exception also
exists. Presentation cleanup runs before composition cleanup, and both results
are merged into one `CleanupResult`. A primary exception always keeps priority
and is re-raised with its original traceback; cleanup failures must not mask or
replace it. With no primary exception, any cleanup failure makes the desktop
entrypoint return exit code `4`, aligned with the native-smoke cleanup-failure
category. The logger is `voiceink.desktop`; it may record only stable cleanup
codes and safe error types, never paths or raw exception text.

## Security and Dependency Direction

This slice has no audio or native runtime, but the boundary must enforce the
following:

- The default composition reads no environment variables, runtime manifests,
  artifact locks, model files, credentials, audio devices, or network
  endpoints.
- It imports no `voiceink_win.infrastructure` barrel and no module that starts
  a process or HTTP server. `voiceink_win.composition` remains a separate ASR
  composition root.
- No production module imports `FakeShellBackend`. The test fixture is outside
  the `src` package and must be absent from the wheel and PyInstaller archive.
- No exception is converted into a successful empty transcript. Capability
  absence is `UNAVAILABLE`; a future runtime failure is typed and visible.
- Unavailable UI copy contains no local paths, executable paths, model IDs,
  exception strings, credentials, audio, or transcript data.
- The production default cannot be changed by an environment variable,
  broad fallback handler, or untrusted CLI switch.
- Presentation depends on controller/application contracts and domain
  snapshots, never on concrete infrastructure adapters. Future adapter
  selection occurs only at the composition boundary.

The implementation must include a static import-graph check and a subprocess
import test. The subprocess test imports the production app, builds the
default composition, and proves that neither `fake_shell` nor native ASR
runtime modules are loaded. The packaging gate must build a wheel with
`python -m pip wheel --no-deps --wheel-dir wheelhouse .`, inspect its ZIP member
names, and inspect both frozen executables with
`pyi-archive_viewer --list` (or the equivalent PyInstaller TOC). It rejects
`tests/`, `tests.support`, `fake_shell`, and `FakeShellBackend` in all release
artifacts; the existing PE/subsystem smoke remains a separate gate.

## Test Plan

Tests verify behavior and boundaries, not implementation details.

### Domain and application

- Preserve all enabled-controller tests: recording, processing,
  transcript-ready, empty, typed error, unexpected error, invalid actions,
  reset, and unsubscribe.
- Construct the unavailable controller without a backend and assert its initial
  snapshot, no-op actions, listener behavior, and snapshot invariants.
- Assert unavailable actions do not invoke a backend, publish synthetic text,
  create a processing timer, or change state.
- Run these tests without PySide6, a display server, Windows APIs, or native
  runtime artifacts.

### Composition and lifecycle

- Build the production composition without arguments and assert an unavailable
  initial snapshot.
- In a subprocess, assert that building the default composition does not read
  runtime configuration, start a process, create a sidecar, instantiate
  `BackendApplication`, or import fake/native runtime modules.
- Test idempotent composition close and verify that future owned resources
  would close once and only after presentation disposal.
- Exercise normal event-loop return, event-loop exception, window-construction
  failure after child creation, child-disposal failure, composition-close
  failure, and simultaneous cleanup failures.
- Verify transactional rollback, cleanup ordering, primary-exception
  preservation, cached results, and sanitized diagnostics through a private
  test-only session runner. No factory injection is added to the public
  production API.

### Presentation and packaging

- With Qt's offscreen platform, assert unavailable copy, disabled recorder
  action, inactive waveform, no active timer, and no synthetic transcript or
  success timestamp.
- Assert `MainWindow.dispose()` and `closeEvent()` are idempotent and release
  theme/controller subscriptions even when child disposal fails.
- Emit a theme-change signal after disposal and assert that no callback reaches
  the window. Assert disposal does not publish a reset snapshot.
- Assert the source/import contract for `presentation.app.main`, the installed
  script, and `scripts/frontend_entrypoint.py` contains no fake construction.
- Extend the existing Windows frontend packaging smoke to run the explicit
  wheel/archive-content gate above, then launch the real frontend entrypoint
  in offscreen smoke mode. The workflow must fail closed if any inspection
  command fails.

## Acceptance Criteria

The implementation is acceptable only when all of the following hold:

1. The installed shell, `scripts/frontend_entrypoint.py`, and PyInstaller
   paths reach `build_desktop_composition()` without constructing or importing
   `FakeShellBackend`.
2. The default composition imports no fake/native ASR module, reads no runtime
   configuration, and starts no process, microphone, sidecar, model,
   recording timer, or network endpoint.
3. `FakeShellBackend` is test-only, absent from the wheel and frozen archive,
   and cannot be selected as a production fallback.
4. The production shell starts in `UNAVAILABLE`, renders the exact unavailable
   contract, disables recorder actions, and never displays synthetic text or a
   recording promise.
5. Every unavailable recording/process action has the specified no-op result
   and no backend, timer, or listener side effect.
6. Injected-backend tests retain coverage of enabled transitions, error
   mapping, listener removal, and reset behavior.
7. Composition and presentation cleanup are ordered, idempotent, and covered
   across normal, exceptional, and cleanup-failure paths; cleanup never masks
   a primary exception.
8. Core tests run without PySide6 or a display server, and the offscreen UI
   test proves unavailable rendering and shutdown.
9. The real frontend packaging smoke path passes, frozen artifacts contain no
   test-support/fake-shell module, and `make check` passes.
10. No production code in this slice adds microphone capture, ASR invocation,
    runtime startup, model loading, imported-media behavior, or a fake-success
    fallback.

The unavailable UI acceptance also checks the page subtext and transcript
metadata values above, and proves that the current recording-promise strings
are absent from the rendered production state.

## Migration and Rollback

There is no data, schema, model, artifact, or recording migration. The
implementation must be released as one coherent entrypoint/state/UI change;
partial migration would either retain false success or expose an inconsistent
state.

Before approval, perform this separate documentation-only gate in a follow-up
commit: add this RFC to `spec/catalog.yaml` with `kind: rfc` and `status: draft`,
then run `make spec-check`. This draft intentionally cannot make that catalog
change while keeping the requested RFC-only scope.

After approval, implementation order is:

1. Amend `spec/features/004-frontend-shell.md` to replace the fake-demo user
   promise with the unavailable-shell contract.
2. Add `UNAVAILABLE`, snapshot invariants, and the explicit unavailable
   controller construction path while preserving injected enabled behavior.
3. Add the desktop composition and its idempotent close operation.
4. Refactor entrypoint ownership, theme subscription, window construction, UI
   mapping, and disposal.
5. Move the fake fixture out of `src`, update tests, and verify package
   contents.
6. Add the boundary, lifecycle, UI, import, and packaging tests; run the full
   quality gate and frontend smoke.
7. Update the RFC status and affected feature specification only after all exit
   criteria and artifact gates pass.

Rollback is also artifact-atomic. If implementation validation fails, revert
the whole unavailable-shell change before publishing an artifact, or publish
no desktop shell. Republishing the old fake-enabled artifact is not an
acceptable rollback because it reintroduces the false-success defect. A
rollback must never catch composition failure and substitute
`FakeShellBackend`.

## Open Questions

These questions are deliberately deferred to the recording/ASR RFC and do not
block this unavailable-shell boundary:

1. When capture exists but runtime startup is not ready, should the shell gain
   a distinct `RUNTIME_NOT_READY` state or map a typed failure to `ERROR` with
   recovery copy?
2. Should live recording replace the synchronous
   `ShellTranscriptionBackend.transcribe()` seam with separate capture and
   transcription ports, or can an adapter preserve it without leaking audio
   lifecycle into `ShellController`?
3. Which application or Windows adapter owns microphone permission prompts and
   device selection?
4. What shutdown deadline, drain policy, and recovery/diagnostic contract
   applies once capture workers and an ASR sidecar are composition-owned?
5. Is `UNAVAILABLE` a build capability state only, or will a later product
   configuration expose an explicit capability/readiness model distinct from
   runtime failure?

## Exit Criteria

This RFC may move from Draft to an implementation-complete status only when:

- it is registered in `spec/catalog.yaml` and the catalog validates it;
- the affected frontend specification records the unavailable-shell contract;
- the production path is free of fake construction and native-runtime startup;
- unavailable and injected-enabled behavior are covered by focused tests;
- lifecycle ownership, idempotent disposal, and cleanup failure semantics are
  tested;
- the offscreen frontend and real packaging smoke paths pass; and
- `make check` passes without any claim of microphone or ASR capability that
  this slice does not implement.

## Decision Needed

Approve the unavailable-shell migration as the prerequisite for real
microphone and ASR work. Approval must explicitly reject `FakeShellBackend` as
a production fallback and preserve the open questions above for the subsequent
recording/ASR RFC.
