# Feature: History Media Actions

## Status and Scope

Status: implemented. This specification covers the retained-artifact Audio and
Folder actions in History. It mirrors `rfcs/workstreams/history-media-actions.md`.
Windows-native Explorer and Qt Multimedia behavior remains host-dependent and
is not proven by the local macOS run.

The feature includes application-level artifact resolution, platform ports,
Windows adapters, History UI wiring, localized availability/error states, and
safe lifecycle handling. It excludes source-folder reveal, macOS adapters, and
audible playback assertions in headless tests.

## User Scenarios

- A completed History record with a retained artifact can play that exact file.
- A completed History record can reveal/select that exact retained file in Explorer.
- A missing, invalid, unsafe, or unavailable artifact disables both media actions
  with neutral localized copy.
- A platform failure shows an error without claiming that playback or reveal
  succeeded.
- A playback failure delivered asynchronously updates only the current History
  action and is ignored after refresh, row change, a newer media action, or
  disposal.

## Functional Requirements

1. Presentation passes a record to `HistoryMediaActionService`; it never receives
   a `Path`, artifact root, source-folder reference, or filesystem callback.
2. The application service resolves and validates `audio_artifact_path` through
   `HistoryAudioArtifactPort` immediately before every action.
3. Audio and reveal ports receive the same resolved regular artifact path.
4. `HistoryAudioPlaybackPort.play(Path)` remains synchronous at the port boundary;
   asynchronous playback failures use the separate optional failure-event port.
5. Missing or invalid artifacts map to unavailable safe reason codes. Platform
   capability failures map to `PLATFORM_UNAVAILABLE`; operation failures map to
   `OPERATION_FAILED` and safe structured logs.
6. Windows reveal uses the absolute system Explorer executable and passes the
   `/select,` argument and target path as separate process arguments.
7. Qt playback owns its player/output lifetime, reports asynchronous errors once,
   ignores stale playback requests, resets the media source before release, and
   releases resources even when cleanup steps fail.
8. History media callbacks are queued onto the Qt thread and invalidated by
   record changes, refreshes, newer media commands, and page disposal.
9. Tests inject artifact and platform ports. They do not depend on Explorer,
   Qt Multimedia backends, current user profiles, or audible output.

## Acceptance Criteria

- `make check` passes.
- The focused history media and composition tests cover artifact identity,
  validation, capability/operation failures, safe logging, callback cleanup,
  Explorer argument construction, and playback lifecycle.
- GUI history tests cover application-service delegation, localization, and
  neutral disabled actions; the GUI suite runs in the offscreen CI job.
- Windows-native Explorer/audio smoke validation remains a separate Windows-only
  responsibility.
