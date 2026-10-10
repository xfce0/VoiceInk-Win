# RFC: History Media Actions

## Status

Proposed. RFC only; no implementation is included in this workstream.

## Decision Summary

History media actions must operate on the normalized local artifact retained by
VoiceInk, not on an imported source path and not on an unvalidated string passed
from the row widget.

- **Audio** resolves the stored artifact and starts playback of that exact file.
- **Folder** resolves the stored artifact and reveals/selects that exact file in
  the platform file viewer. It does not attempt to reveal the original source
  folder, because the imported-media history record intentionally does not store
  that path.
- The filesystem and platform process APIs remain behind application ports and
  infrastructure adapters. The Qt presentation layer receives neither
  `Path` objects nor filesystem callbacks.
- Availability is computed through the application action service and is
  rechecked immediately before every action. Missing or invalid artifacts are
  reported as unavailable; platform operation failures are reported as errors.
- Tests inject artifact and platform ports. They must not depend on the local
  machine's media player, Explorer, Finder, current user profile, or timing.

## Current State And Findings

### Windows storage and artifact contract

`VoiceInkPaths.default()` uses `%LOCALAPPDATA%/VoiceInk` and defines:

```text
%LOCALAPPDATA%/VoiceInk/voiceink.sqlite3
%LOCALAPPDATA%/VoiceInk/audio/history/<sha256(job-id)>.wav
```

SQLite stores only a validated portable relative reference such as
`history/<digest>.wav`. `AudioArtifactStore` owns the `audio` root, rejects
traversal, absolute paths, wrong-platform separators, symlink components, and
non-regular files, and writes normalized mono 16 kHz signed PCM16 WAV data by
atomic replacement. `reveal_path()` already revalidates the reference and
returns the existing regular file; `folder_path` is the app-owned storage
directory.

The import application saves the normalized artifact before publishing a
successful history record. If artifact saving fails, the transcript remains
valid but has no audio reference. If durable history persistence fails, the
unlinked artifact is cleaned up. History deletion tombstones the row, deletes
the artifact through the store, and finalizes the row, with restart
reconciliation for pending deletions.

### Current History action seams

- `HistoryAudioArtifactPort` is a domain persistence port with
  `save_normalized_audio`, `delete`, and `reveal_path`.
- `HistoryPage` currently declares presentation protocols whose methods accept
  strings: `HistoryAudioPort.play(artifact_reference)` and
  `HistoryFolderPort.reveal(folder_reference)`.
- `HistoryPage` receives `artifact_reveal` and `artifact_folder`, but the
  current action handlers do not use them. Production composition creates the
  artifact store and passes those values through the window without wiring
  actual audio or folder adapters.
- Folder availability currently depends on
  `source_metadata["folder_reference"]`. The imported-media application does
  not produce that field; its metadata contains only a safe source name,
  content hash, size, and job reference. The existing GUI test supplies a
  synthetic `folder-token`, so it verifies only string forwarding, not real
  file reveal.
- Audio availability currently means “a non-empty database reference and an
  injected presentation port exist”. It does not prove that the referenced
  file exists or that it is the saved local artifact.

### macOS reference behavior

The VoiceInk macOS repository is a behavior reference, not a source of Windows
paths or Swift persistence types:

- recordings are stored in
  `~/Library/Application Support/com.prakashjoshipax.VoiceInk/Recordings`;
- SwiftData stores an absolute `audioFileURL` string;
- History checks file existence before rendering the audio player;
- `AudioPlayerManager` loads that URL with `AVAudioPlayer`, prepares it, and
  controls play/pause, seek, rate, and cleanup;
- the Folder button calls `NSWorkspace.selectFile(file, inFileViewerRootedAtPath:
  parent)` and therefore selects the actual audio file, rather than opening an
  unrelated directory.

The Windows equivalent is the same user-visible behavior with a Windows
platform adapter and the Windows artifact store's validated local path.

## Goals

- Make every enabled History media action truthful about the artifact it can
  operate on.
- Make Folder reveal the exact retained artifact, including for imported media.
- Make Audio play the retained normalized WAV artifact.
- Preserve the existing relative-reference storage format and deletion
  lifecycle.
- Keep filesystem, process, and Qt multimedia details out of the presentation
  layer.
- Make success, capability absence, missing artifact, and operation failure
  distinguishable in the UI and deterministic in tests.

## Non-Goals

- No SQLite schema or migration change.
- No retention-policy or quota change.
- No recovery of the original imported source path.
- No copying of source paths, transcript text, or user metadata into audio
  artifacts.
- No direct use of `Path`, `os.startfile`, Explorer, a media process, or Qt
  multimedia from `HistoryPage` or `HistoryRow`.
- No implementation of a macOS application adapter in this Windows repository.
- No replacement of the macOS VoiceInk persistence model with the Windows
  relative-reference model.
- No automatic default-application launch as a substitute for guaranteed local
  artifact playback.

## Proposed Architecture

### Domain boundary

The domain owns durable history semantics and opaque artifact references:

1. `HistoryRecord.audio_artifact_path` remains a nullable, validated,
   portable relative reference. It is a storage token, not a user-visible path
   and not an imported source reference.
2. `HistoryAudioArtifactPort` remains the artifact lifecycle/lookup port used by
   the application and import/deletion workflows. Its lookup operation must
   retain the current `reveal_path(relative_path) -> Path` semantics: validate
   the relative token, reject unsafe identity, and return only an existing
   regular file within the artifact root.
3. `source_metadata` remains display/search metadata. A
   `folder_reference` value is not a valid target for the new Folder action.
4. Domain code does not know Explorer, Finder, `AVAudioPlayer`, Qt multimedia,
   shell commands, or default-application behavior.

The existing `Path` result is allowed at the domain-to-outer-layer lookup seam
because it is consumed by the application service and infrastructure adapters.
It must never be exposed by a presentation constructor, row model, signal, or
localized message.

### Application boundary

Add one application-facing `HistoryMediaActionService` (name is normative for
the RFC; implementation may split internal helpers without widening the UI
contract). It owns orchestration and safe outcome mapping:

```text
inspect(record) -> HistoryMediaAvailability
play(record) -> HistoryMediaActionResult
reveal(record) -> HistoryMediaActionResult
```

The service receives the existing `HistoryRecord`, reads only its opaque
`audio_artifact_path`, and performs the following sequence for both actions:

1. Reject a missing or blank artifact reference as unavailable.
2. Ask `HistoryAudioArtifactPort.reveal_path()` to validate and resolve the
   current artifact. Do not use `source_metadata` as a fallback.
3. Ask the selected platform port whether the capability is available, if the
   port exposes capability reporting.
4. Pass the resolved path to the platform adapter.
5. Map adapter exceptions to a safe typed error result; never pass exception
   text or an absolute path to the UI.

The service must repeat resolution at action time even when `inspect()` had
reported availability, because the file may be deleted or replaced between
render and click.

The application ports are:

```text
HistoryAudioPlaybackPort
    is_available() -> bool
    play(resolved_artifact: Path) -> None

HistoryArtifactRevealPort
    is_available() -> bool
    reveal(resolved_artifact: Path) -> None
```

The adapters receive only a path already validated by the artifact store. They
do not resolve database references, inspect `HistoryRecord`, or decide whether
an action is available.

`HistoryMediaAvailability` must expose separate audio and reveal states. At
minimum, each state distinguishes `available`, `unavailable`, and `error` (or
an equivalent typed representation) with a stable safe reason code. The
action-result code set must cover:

- `NO_ARTIFACT` — the record has no retained artifact;
- `ARTIFACT_MISSING_OR_INVALID` — lookup failed or the artifact is no longer a
  valid regular file;
- `PLATFORM_UNAVAILABLE` — the adapter is not configured or cannot provide its
  capability;
- `STARTED` — playback/reveal was accepted by the adapter;
- `OPERATION_FAILED` — the adapter was available but the operation failed.

The service logs structured diagnostics using the history ID and safe reason
code only. It must not log the absolute artifact path, original source path,
audio bytes, or transcript content.

### Presentation boundary

`HistoryPage` and `HistoryRow` use only the application action service:

- remove `artifact_reveal` and `artifact_folder` from presentation wiring;
- remove the current string-based `HistoryAudioPort` and
  `HistoryFolderPort` protocols from the row/page once the application service
  is available;
- pass a service (or a narrow application facade exposing the three methods
  above) into `HistoryPage`;
- derive button enabled/disabled state from `inspect(record)`, not from
  `bool(record.audio_artifact_path)` or a synthetic `folder_reference`;
- on click, pass the record/action identity to the service, never a path;
- render `NO_ARTIFACT`, `ARTIFACT_MISSING_OR_INVALID`, and
  `PLATFORM_UNAVAILABLE` as localized neutral unavailable states;
- render `OPERATION_FAILED` as a localized actionable error state;
- render `STARTED` as the existing localized success/status state.

The row may retain the existing labels (“Audio” and “Folder”), but “Folder”
means “show the stored artifact in the file viewer”. No user-facing UI text
may display the artifact root, relative token, absolute path, or exception
message.

The page remains responsible for selection, row-local controls, localization,
and status/error rendering. It does not perform filesystem checks, invoke
platform APIs, or construct `AudioArtifactStore`.

### Infrastructure and composition boundary

`AudioArtifactStore` remains the Windows storage adapter and is injected into
the application service as the artifact lookup/lifecycle port. The composition
root creates and injects platform adapters separately:

- `WindowsHistoryAudioPlaybackAdapter` plays the resolved WAV through the
  chosen supported Windows/Qt multimedia mechanism and owns player lifetime;
- `WindowsHistoryArtifactRevealAdapter` selects the exact resolved file in
  Explorer and owns Windows shell invocation/error translation.

The concrete playback technology and Explorer invocation are infrastructure
details. Their public contract is only the application port above. The
composition root must make an absent or initialization-failed adapter visible
as `PLATFORM_UNAVAILABLE`, rather than silently creating an enabled-looking
button.

The macOS repository may later provide analogous adapters using
`AVAudioPlayer` and `NSWorkspace.selectFile`, but that is outside this branch.
The shared behavior is the target; the OS APIs and stored path formats are not
shared implementations.

## Acceptance Criteria

### Domain and application

- **Given** a completed record with `audio_artifact_path = "history/item.wav"`
  and a real artifact under the configured store,
  **when** `play(record)` is called, **then** the playback port receives the
  resolved absolute local file and returns `STARTED`.
- **Given** the same record, **when** `reveal(record)` is called, **then** the
  reveal port receives that same exact file, not the artifact directory and not
  a source-folder token.
- **Given** a record with no artifact reference, a missing artifact, a
  traversal reference, a symlink, or a non-regular file, **when** availability
  is inspected or an action is invoked, **then** the result is unavailable with
  the matching safe reason code and no platform port is called.
- **Given** an absent platform adapter, **when** availability is inspected,
  **then** the action is unavailable and the UI cannot enable the button.
- **Given** an adapter that raises after receiving a validated file, **when** an
  action is invoked, **then** the result is `OPERATION_FAILED`, the exception
  is logged structurally, and no raw path/exception is returned to presentation.
- **Given** an artifact that disappears after `inspect()` and before the click,
  **when** the action is invoked, **then** the second lookup wins and the result
  is unavailable rather than falsely reporting success.

### Presentation

- History action controls never receive or store `Path` objects, absolute paths,
  artifact roots, or source-folder references.
- A record without a retained artifact or with an unavailable platform has a
  disabled action with localized neutral copy; it is not shown as a generic
  red operation error.
- A platform failure shows the localized History action error and does not
  claim that playback started or that a folder was opened.
- A successful Folder action selects the actual retained artifact; a successful
  Audio action reports playback started for that retained artifact.
- Existing row ownership, fixed-width layout, newest-first ordering,
  localization, copy, export, and deletion behavior remain unchanged.
- Production composition no longer passes the currently unused
  `artifact_reveal`/`artifact_folder` filesystem seams into the UI.

### Persistence and lifecycle

- The existing `audio_artifact_path` column, relative path validation, atomic
  writes, quotas, tombstone deletion, and restart reconciliation remain
  compatible.
- Records whose artifact save failed continue to render transcript content and
  truthfully show media actions as unavailable.
- Deleting a history record remains the only lifecycle owner for deleting its
  retained artifact; playback/reveal adapters never delete or mutate it.

## Deterministic Test Plan

### Domain/storage tests

Extend the existing storage tests in `tests/test_sqlite_persistence.py` to keep
coverage for traversal rejection, regular-file validation, atomic writes, WAV
format, and quota behavior. Add assertions that the action lookup returns the
exact `audio/history/<digest>.wav` file and rejects missing, symlinked, and
non-regular targets.

### Application tests

Add focused tests with injected fakes:

- a fake artifact port maps a relative reference to a `tmp_path` artifact and
  records every lookup;
- fake playback and reveal ports record the received `Path`, availability, and
  calls without launching a process;
- success proves both actions receive the same resolved artifact path;
- each unavailable reason proves the platform port is not called;
- adapter exceptions map to `OPERATION_FAILED`;
- a fake that removes the artifact after inspection proves action-time
  revalidation;
- logs and results contain no absolute path, source path, transcript, or audio
  payload.

These tests should live with application/media action tests rather than rely on
Qt or a real Windows installation.

### Presentation tests

Update the existing History GUI tests in
`tests/test_settings_history_dictionary_gui.py`:

- inject a fake application action service, not a fake filesystem callback;
- assert button state follows the service availability result;
- assert Folder invokes the service for the record and does not forward the
  synthetic `source_metadata["folder_reference"]`;
- assert Audio invokes the service for the record and does not forward the
  database-relative artifact token;
- cover neutral unavailable states, localized status/error states, success,
  and action-time failure;
- retain fixed-width row ownership and English/Russian localization coverage.

The GUI tests remain offscreen and deterministic. They must not use a system
media player, Explorer, Finder, or real user-profile artifact directory.

### Windows and macOS limits

- The repository's regular quality job runs Python tests on Windows and the
  offscreen GUI contract on Ubuntu. The new port/application tests are valid in
  both environments because all external behavior is injected.
- Actual Explorer selection and actual audio playback are Windows integration
  concerns. They may be covered by a Windows-only smoke/manual check using a
  temporary valid WAV, but a headless CI test must not assert foreground shell
  focus or audible output. The native check must assert adapter acceptance,
  process/API error mapping, and that the target file is the retained artifact.
- The current macOS host and this Windows repository cannot establish Windows
  native behavior. No local macOS run is evidence that Explorer or Windows
  playback works.
- The VoiceInk macOS repository is used only to verify the behavioral reference:
  existing-file gating, `AVAudioPlayer` playback, and selecting the exact file
  with `NSWorkspace`. macOS adapter tests, if later added, belong in that
  repository and must not be claimed by this branch's Windows test suite.

## Implementation Order After Approval

1. Introduce application action result/capability types and the two platform
   ports without changing storage.
2. Implement the application service around `HistoryAudioArtifactPort` and
   cover its result matrix with injected fakes.
3. Implement Windows playback and exact-file reveal adapters and wire them only
   in the composition root.
4. Replace presentation string ports and stale artifact plumbing with the
   application service; update localized neutral/error states.
5. Update focused GUI tests, then run the repository quality and Windows-native
   checks appropriate to the environment.

## Open Questions

None for the requested behavior. The concrete Windows playback backend can be
selected during implementation, provided it satisfies the application port,
plays the retained artifact, owns its lifetime, and preserves the result/error
contract above.
