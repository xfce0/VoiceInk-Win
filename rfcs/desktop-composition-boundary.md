# RFC: Minimal Desktop Composition Boundary

## Status

Draft. This document remains an architecture reference and does not authorize
microphone, WASAPI, hotkey, or native-runtime implementation. The current
working tree contains the explicitly scoped persistence and lifecycle slice
described below; this RFC is not an approval record.

This RFC defines the production composition boundary, configured local
persistence, the imported-media bootstrap path, and the unavailable fallback
when runtime resources are not configured. It does not define microphone
capture, WASAPI, hotkeys, packaging inspection, or multi-resource shutdown
policy.

## Summary

The current desktop entrypoint constructs `ShellController(FakeShellBackend())`.
That makes the shell display a successful transcript even though this build has
no microphone and no live ASR path. The result is a false-success production
flow.

The smallest safe correction is:

1. add an explicit `UNAVAILABLE` controller state;
2. make the default desktop composition own persistence and an unavailable
   imported-media controller until its configured backend is ready;
3. make the entrypoint own that composition for the event-loop lifetime; and
4. keep `FakeShellBackend` out of production composition while retaining it for
   focused tests; an optional developer demo remains outside this RFC.

The boundary must be useful without pretending that recording exists. It
creates the configured SQLite database and audio-artifact root, and starts the
imported-media bootstrap only when its environment is complete. It never
creates a microphone, WASAPI path, or global hotkey.

## Problem and Evidence

The packaged shell currently reaches this path:

```text
frontend entrypoint -> presentation.app.main
                    -> ShellController(FakeShellBackend())
                    -> fixed transcript
```

`FakeShellBackend` is therefore reachable from the user-facing startup path,
not only from tests. The existing controller has no capability-absent state and
the presentation maps its initial state to recording-oriented copy. The feature
specification also describes the fake-backed flow as a demo without separating
that flow from production startup.

This violates the project requirements that platform boundaries be explicit and
that unavailable capabilities be visible rather than represented as synthetic
success.

## Goals

- Define one small, argument-free production composition API.
- Start the production shell in a stable, user-visible unavailable state.
- Define exact no-resource lifecycle and exception semantics.
- Keep the controller independent of Qt, Windows APIs, subprocesses, HTTP, and
  native runtime details.
- Preserve injected enabled-controller behavior for focused tests and future
  adapters.
- State a strict production-versus-demo policy for `FakeShellBackend`.
- Provide a core test plan that can run without PySide6, Windows, a display
  server, model files, or native runtime artifacts.

## Non-Goals

- GUI archive or frozen-bundle scanning, wheel member inspection, or packaging
  policy. These belong to a future desktop artifact-verification RFC.
- Full cleanup aggregation, transactional rollback of several owned resources,
  cleanup exit codes, shutdown deadlines, or failure precedence across resource
  owners. These belong to a future desktop shutdown-reliability RFC.
- Microphone, WASAPI, permissions, audio devices, recording workers, global
  hotkeys, or audio buffering. These belong to a future microphone-capture RFC.
- `AsrApplicationService`, `BackendApplication`, Parakeet, sidecars, model
  loading, runtime discovery, runtime configuration, or network transport.
  Runtime integration remains specified by
  `rfcs/parakeet-runtime-integration.md`.
- Detailed imported-media behavior, history-page behavior, tray policy, text
  delivery, installer behavior, or native smoke changes. The composition still
  owns the configured persistence boundary and history-deletion reconciliation
  needed by the desktop lifecycle.
- A production fake fallback, environment-controlled demo mode, or a promise
  that recording will become available in this build.

## Proposed Architecture

### Boundary and dependency direction

The dependency direction remains:

```text
Presentation -> Application -> Domain <- Infrastructure
```

The production desktop composition root is an outer adapter. It creates the
application controller but does not select a fake backend, runtime adapter, or
Windows resource. Presentation receives the controller through the composition
boundary and does not import infrastructure adapters.

The new module is `voiceink_win.desktop_composition`. Its complete public API
for this RFC is:

```python
class DesktopComposition(Protocol):
    @property
    def controller(self) -> ShellController: ...

    @property
    def transcribe_controller(self) -> TranscribePageController: ...

    @property
    def persistence(self) -> PersistenceService: ...

    @property
    def artifact_cleanup(self) -> Callable[[str], None]: ...

    @property
    def history_deletion(self) -> HistoryDeletionService: ...

    def close(self) -> None: ...


def build_desktop_composition() -> DesktopComposition: ...
```

The API is intentionally exact and minimal:

- `build_desktop_composition()` takes no arguments and has no environment,
  command-line, factory, backend, or runtime override.
- `controller` returns the composition-owned unavailable microphone controller.
- `transcribe_controller` starts unavailable/loading and is attached to the
  verified imported-media backend only after asynchronous bootstrap succeeds.
- `persistence` is the composition-owned SQLite boundary backed by the
  configured VoiceInk application-data path.
- `artifact_cleanup` is the composition-owned validated audio artifact delete
  operation; it never accepts an external path outside the artifact root.
- `history_deletion` owns pending tombstone reconciliation and serialized
  history/artifact deletion work.
- `close()` is synchronous and idempotent; it closes imported-media workers,
  artifact work, and SQLite after queued operations have drained.
- There is no `CleanupResult`, async close, context-manager API, resource list,
  rollback callback, or public test factory in this slice.
- The composition remains the owner of the controller even though the
  controller currently owns only memory.

### Unavailable controller contract

The application adds an explicit construction path, preferably
`ShellController.unavailable()`. The existing backend-requiring constructor
remains explicit and is not made optional.

`UNAVAILABLE` is a build capability state, not a failed transcription request.
An unavailable controller contains no backend. Its snapshot has an empty
transcript and empty error; only `TRANSCRIPT_READY` may contain transcript text
and only `ERROR` may contain an error message.

| Action | Return value | Snapshot | Side effect |
|---|---:|---|---|
| Construct unavailable controller | succeeds | `UNAVAILABLE` | none |
| `start_recording()` | `False` | unchanged | none |
| `stop_recording()` | `False` | unchanged | none |
| `complete_processing()` | `False` | unchanged | none |
| `reset()` | no return value | unchanged | no publication |

Unavailable actions do not invoke a backend, publish another state, create a
timer, or produce text. Existing subscription and unsubscription contracts
remain valid; unavailable no-op actions do not notify listeners. Each action is
`O(1)` time and `O(1)` space, excluding any listener work for enabled states.

The injected backend constructor continues to cover enabled transitions,
typed failures, reset, and listener behavior in tests. It is not a production
composition seam in this RFC.

### Production and demo fake policy

`FakeShellBackend` has one permitted use in this RFC:

1. focused controller/application tests.

It has no permitted use in `build_desktop_composition()`, the installed
user-facing shell, any production module, or any production fallback path. The
implementation must keep the fixture outside the production infrastructure
package and must not export it from the infrastructure barrel. An environment
variable, CLI switch, missing-runtime handler, or broad exception handler must
not select it.

If a later development-only demo is desired, a separately named harness may
import the test fixture directly from development support and must be clearly
marked as a demo. It must not call the production builder with an override, be
an installed user-facing entrypoint, or be used as production evidence. This
RFC does not require that harness.

### Unavailable presentation contract

The production dashboard and recorder render capability absence, not an idle
recording promise:

- state: **Recording unavailable**;
- headline: **Recording is unavailable in this build.**;
- detail: **Microphone capture and ASR are not included.**;
- page subtext: **Recording cannot start because microphone capture and ASR are
  not included.**;
- transcript metadata: **Capability unavailable**;
- transcript body: **Transcripts are unavailable because recording and ASR are
  not included.**;
- recorder status: **Unavailable**;
- recorder action: disabled, with an accessible name and description explaining
  that microphone capture is not connected;
- waveform: inactive and not driven by an animation or processing timer.

The dashboard may keep an `Open recorder` action only to show this explanation.
The unavailable state must not render `Ready`, `Listening`, `Transcribing`,
`Start recording`, `Start again`, `Try again`, `Working...`, a success
timestamp, or synthetic transcript text.

### Configured persistence and imported-media lifecycle

`build_desktop_composition()` resolves `VoiceInkPaths.default()`, opens
`voiceink.sqlite3` through `SQLitePersistence`, and uses the sibling `audio/`
directory for optional relative artifact references. Imported-media terminal
results use the same `HistoryPort` and are recorded once after the cleanup
fence, including source metadata and failure status.

If the runtime manifest, artifact lock, FFmpeg metadata, import roots, or
verified binaries are unavailable, persistence remains configured while the
Transcribe page is explicitly unavailable. This is an unavailable
no-resource fallback for imported media, not a fake transcript or fake
production backend.

### Exact lifecycle and exit semantics

The entrypoint owns the composition from successful construction until the Qt
event loop has returned. The lifecycle is equivalent to:

```python
composition = build_desktop_composition()
try:
    window = MainWindow(composition.controller, theme=theme)
    window.show()
    return application.exec()
finally:
    composition.close()
```

The `try` begins immediately after `build_desktop_composition()` succeeds.
Therefore:

- a composition-build exception has no composition to close and propagates;
- a window-construction, `show()`, or event-loop exception calls the
  composition's `close()` and re-raises the original exception unchanged;
- a normal Qt return preserves its integer exit code after `close()` runs;
- the no-resource `close()` has no failure result and does not translate an
  exit code, aggregate exceptions, or log raw exception text; and
- there is no exception handler that substitutes a fake backend.

This guarantee covers composition-owned resources. The window's presentation
cleanup remains a presentation concern; shutdown failures are logged at the
composition boundary and never substitute a fake backend. A future
shutdown-reliability RFC may define aggregate exit-code policy.

### Resource prohibition

The default builder and unavailable controller must not:

- read runtime manifests, model files, or credentials unless the imported-media
  bootstrap is explicitly configured;
- inspect audio devices or initialize microphone/WASAPI/recording timers;
- import the ASR composition root or native runtime modules when the runtime is
  unavailable;
- start a process, sidecar, HTTP server/client, or network endpoint when the
  imported-media configuration is incomplete; or
- create global hotkeys or recording workers.

SQLite persistence and the audio artifact root are intentionally initialized
independently of imported-media availability.

Qt objects used only to render the unavailable view are allowed. No recording,
processing, or waveform animation timer is allowed.

## Test Plan

Tests are behavior and boundary tests. They are part of the later
implementation change; this RFC adds no tests or production code.

### Core tests without GUI or runtime dependencies

- Construct `ShellController.unavailable()` and assert the initial snapshot,
  snapshot invariants, all four unavailable actions, and unchanged listener
  state.
- Assert unavailable actions cannot invoke a spy backend, publish synthetic
  text, or transition to an enabled state.
- Preserve injected-backend tests for recording, processing, transcript-ready,
  empty, typed error, unexpected error, reset, invalid actions, and
  unsubscription.
- Build `build_desktop_composition()` without arguments and assert an
  unavailable controller; call `close()` twice and assert idempotence.
- In a subprocess, import the production composition and assert that fake-shell
  and native-runtime modules are not loaded and that the builder performs no
  process or network startup.

### Entrypoint and offscreen presentation tests

- Use a test-only session runner to assert that normal event-loop return
  preserves its code, construction failure and event-loop failure re-raise the
  original exception, and `close()` is called once in each case.
- With Qt offscreen, assert the exact unavailable copy, disabled recorder
  action, inactive waveform, no active timer, empty snapshot fields, and
  absence of synthetic transcript/success copy.
- Assert that the production entrypoint contains no fake construction and that
  any optional demo fake is only reached from a separately named,
  development-only harness.

### Explicitly excluded tests

This slice does not add archive scanning, frozen-artifact inspection, full
cleanup aggregation, microphone/device tests, sidecar/runtime tests, or native
Windows smoke coverage. Those tests belong to the follow-up RFCs listed below.

## Acceptance Criteria

The no-resource implementation is acceptable only when:

1. the production desktop builder has exactly the API defined above and takes
   no backend or runtime arguments;
2. the default production shell starts in `UNAVAILABLE` and renders the exact
   unavailable contract;
3. no production path constructs or imports `FakeShellBackend`;
4. unavailable actions are no-ops with the specified return values and no
   listener, timer, backend, or transcript side effects;
5. the composition lifecycle preserves normal exit codes, closes on exceptional
   paths, and never substitutes a fake backend;
6. core tests run without PySide6, a display server, Windows APIs, model files,
   or native runtime artifacts; and
7. `make spec-check` passes for the catalog and all registered documents.

Archive inspection, aggregate cleanup semantics, microphone capability, and
real runtime capability must not be used as acceptance evidence for this RFC.

## Follow-up RFCs

The following work is intentionally moved out of this boundary:

| Follow-up | Responsibility |
|---|---|
| Desktop artifact-verification RFC | GUI archive scanning, wheel/frozen-member inspection, and release artifact policy |
| Desktop shutdown-reliability RFC | Full cleanup aggregation, rollback, deadlines, and cleanup exit-code policy |
| Microphone-capture RFC | WASAPI, permissions, devices, capture workers, and audio lifecycle |
| `rfcs/parakeet-runtime-integration.md` | ASR runtime, model, sidecar, transport, and runtime failure policy |

Imported-media transcription remains governed by its existing RFC, but its
configured application service and shared `HistoryPort` are part of the
production desktop composition. Microphone capture remains excluded.

## Migration and Rollback

After approval, implementation should be one small code change: add the
unavailable controller path, add the no-resource composition module, route the
entrypoint through it, and update the unavailable presentation mapping. Move
the fake fixture out of the production package only as required to enforce the
policy; do not add runtime or microphone code.

There is no data migration. If validation fails, revert the unavailable-shell
implementation as one unit rather than restoring a fake-success production
fallback.

## Open Questions

These questions are deliberately deferred and do not block this no-resource
slice:

1. Which microphone capture port and Windows permission policy should the
   microphone-capture RFC define?
2. Should a future runtime-not-ready condition be a distinct state or a typed
   `ERROR` state once capture exists?
3. Which exact artifact inspection and release policy should the desktop
   artifact-verification RFC require?
4. Which shutdown deadline, drain policy, and cleanup failure exit contract is
   appropriate once the composition owns workers, transports, or a sidecar?

## Exit Criteria

This RFC can move from Draft to Approved when:

- this RFC and the affected feature specification are registered and pass
  `make spec-check`;
- the no-resource API, unavailable behavior, fake policy, and exception/exit
  semantics are unambiguous;
- the core and offscreen tests above are sufficient to falsify the contract;
  and
- archive scanning, aggregate cleanup, microphone integration, and runtime
  integration are explicitly owned by follow-up RFCs rather than this slice.

Implementation completion is a separate change. That change must satisfy the
acceptance criteria above and may not expand this RFC's scope without an
amendment.
