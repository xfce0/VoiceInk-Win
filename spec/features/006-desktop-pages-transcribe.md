# Feature: Desktop Pages and Imported Transcription

## Status and Scope

Status: proposed.

Current implementation scope: this slice enables only the Transcribe route and
its imported-media workflow. Modes, History, Dictionary, AI Models, and Audio
remain specified future work; their sidebar descriptors stay disabled and this
slice does not claim route-complete delivery.

This feature is the next desktop presentation slice after the existing shell
and microphone planning. It adds the Modes, Transcribe, History, Dictionary,
AI Models, and Audio pages, and connects the Transcribe page to the existing
imported-media transcription application service. It defines the page state,
commands, persistence contracts, queue behavior, output actions, and the
Windows parity target documented in `rfcs/desktop-pages-transcribe.md`.

The feature includes:

- desktop navigation and page-level view models for the six destinations;
- real local audio/video file selection and drag-and-drop on Transcribe;
- bounded imported-media queue presentation with progress, cancellation,
  automatic retry state, manual retry, and visible errors;
- FFmpeg-backed content validation and normalization before ASR;
- original/enhanced transcript presentation when enhanced text exists;
- Markdown, TXT, and clipboard output actions;
- history query, detail, deletion, and transcript output actions;
- mode, dictionary, model-catalog, and deferred-audio-settings page contracts.

The feature explicitly excludes microphone capture, WASAPI, native audio input
or playback, global hotkeys, text injection/paste into another application,
streaming transcription, persistent queue recovery, and production model or
cloud-provider implementation. The Audio page is a presentational settings
surface whose native-audio-dependent controls remain unavailable until the
later microphone/native-audio feature is approved.

## User Scenarios

- A user opens any of the six destinations from the desktop rail and sees the
  expected page title, empty state, controls, and accessibility labels.
- A user drops one or more supported local media files on Transcribe, or uses
  Choose Files, and sees each admitted file in one FIFO queue.
- A user starts the queue and sees each item move through queued, normalizing,
  transcribing, optional enhancing, cleanup, and a terminal state.
- A user cancels a queued or running import and sees cancellation after the
  application cleanup fence; no partial transcript is shown or persisted.
- A user sees an actionable error for invalid, corrupt, audio-less,
  unsupported, resource-limited, or runtime-failed media and can retry when
  retry is offered.
- A user opens a completed item, switches between Original and Enhanced when
  both exist, copies the effective text, or saves it as TXT or Markdown.
- A user searches and pages through History, opens a transcript detail, copies
  or saves a selected variant, and deletes entries with confirmation.
- A user edits modes, dictionary entries, or persisted non-native audio
  preferences without causing microphone capture or native audio calls.

## Functional Requirements

1. The desktop route registry must expose `modes`, `transcribe`, `history`,
   `dictionary`, `ai_models`, and `audio` routes. Each route must have a stable
   identifier, display title, icon name, tooltip, and accessibility label.
2. Route selection must be application state, not widget-local state. A page
   must render from an immutable snapshot and emit typed commands to its page
   controller.
3. The Presentation layer may import only page snapshots, commands, and
   application ports. It must not import Qt subprocess APIs, FFmpeg types,
   SQLite drivers, Windows APIs, or concrete ASR/runtime adapters.
4. Transcribe must accept multiple local regular files through a file picker
   and drag-and-drop. The picker/filter is only a user-experience hint; it is
   never an acceptance decision.
5. The required v1 input matrix is exactly the following. Every listed format
   must be sent through the FFmpeg normalization boundary, and the source
   content and decodability must be validated by FFmpeg rather than inferred
   from the extension.

   | Input label | Required content family | Audio selection |
   |---|---|---|
   | WAV | RIFF/WAVE audio | first audio stream |
   | MP3 | MPEG audio | first audio stream |
   | M4A | ISO Base Media audio | first audio stream |
   | AIFF | AIFF/AIFF-C audio | first audio stream |
   | MP4 | ISO Base Media audio/video | first audio stream |
   | MOV | QuickTime audio/video | first audio stream |
   | AAC | AAC elementary audio | first audio stream |
   | FLAC | FLAC audio | first audio stream |
   | CAF | Core Audio Format | first audio stream |
   | AMR | AMR-NB/AMR-WB audio | first audio stream |
   | OGG | Ogg container audio | first audio stream |
   | OPUS | Opus audio | first audio stream |
   | 3GP | 3GPP audio/video | first audio stream |
   | WEBM | WebM/Matroska audio/video | first audio stream |

   The v1 picker may match the case-insensitive extensions in this table, but
   the extension is only a display/filter hint. The application must accept or
   reject the bytes according to the matrix after FFmpeg opens, selects, and
   decodes an audio stream; a filename/content extension mismatch alone is not
   a rejection reason. Unsupported or undecodable content must be rejected.
   `OGA` is not part of this required matrix.
6. The imported workflow must call the existing
   `ImportedMediaTranscriptionService`, not a second queue or a page-specific
   ASR implementation. FFmpeg must produce the existing canonical contract:
   mono, 16 kHz, signed PCM S16LE, non-empty samples in a bounded RIFF/WAV
   result.
7. Each admitted queue item must preserve the backend `job_id`, current
   attempt, source display name, source identity metadata, selected mode, and
   typed terminal result. Full paths and raw transcript text must not be
   written to ordinary logs.
8. The UI state machine must represent these states:

   ```text
   pending -> validating -> queued -> normalizing -> transcribing
                    |          |          |              |
                    v          v          v              v
                 rejected   cancelled  failed      retry_waiting
                                                       |
                                                       v
                                                      queued
    transcribing -> cleaning_up -> enhancing -> succeeded
          |             |            |             |
          +-------------+------------+-----------> failed/cancelled
   ```

    `cleaning_up` is visible as a finishing state when it lasts long enough to
    render, but publication of a backend terminal result is owned by the
    application cleanup fence. `enhancing` is application post-processing
    after a backend `Success`; enhancement failure or cancellation preserves
    the original successful transcript and completes the page item as
    original-only. The backend states `accepted`, `queued`, `normalizing`,
   `transcribing`, `retry_waiting`, `cleaning_up`, `succeeded`, `failed`, and
   `cancelled` must not be silently collapsed into success or removed from the
   diagnostic mapping.
9. Progress must be a typed phase plus an optional fraction. Queued,
   normalizing, transcribing, enhancing, and cleanup may report an indeterminate
   fraction. The UI must not invent a percentage from elapsed time, file size,
   or extension. If a backend reports a fraction, it must be clamped to
    `[0, 1]` and identified by stage. Progress and terminal updates must be
    observable without blocking the UI; an admitted job must expose a closeable
    observation subscription until its terminal result is published.
10. A user may remove only a `pending` or `rejected` item without invoking the
    backend. Cancel on an admitted item must call the existing job cancellation
    API and keep the item visible until a typed `Cancelled` result is published.
    Cancellation must discard late ASR output and never publish partial text.
    Once backend `Success` has been received and the page is enhancing, cancel
    applies only to the enhancement task and preserves the original success.
11. Automatic retry is limited to the existing transient runtime policy: at
    most two total attempts, capped backoff, a new attempt workspace, and no
    retry for invalid media, no audio stream, malformed normalized output,
    source changes, cancellation, missing configuration, or protocol/schema
    failures. Manual Retry must revalidate the source and create a new admitted
    job; it must not bypass FFmpeg validation or queue capacity.
12. Every failure shown by the page must contain a stable code, safe user
    message, stage, retryability, and a diagnostic action or explanation. At a
    minimum the mapping must cover `InvalidSource`, `QueueFull`,
    `ResourceLimitExceeded`, `SourceChanged`, `NoAudioStream`,
    `UnsupportedMedia`, `NormalizationFailed`, `RuntimeUnavailable`,
    `RuntimeTimeout`, `DeadlineExceeded`, `RuntimeProtocolFailure`,
    `TranscriptionFailed`, `Cancelled`, and `CleanupWarning`.
13. A successful imported transcript must contain original text and may contain
    enhanced text. The page must show Original and Enhanced tabs only when
    enhanced text is non-empty. Selecting an unavailable Enhanced tab must
    fall back to Original and must not expose an enhancement error as transcript
    content.
14. Output actions must be available for a successful transcript and operate on
    a `TranscriptDocument` with an explicit variant. The actions are Copy to
    Clipboard, Save as TXT, and Save as Markdown. Save dialogs must suggest a
    source-derived basename and the correct extension.
15. Clipboard precedence must be deterministic:
    - an explicit variant requested by the action wins;
    - otherwise the currently selected visible tab wins;
    - without a tab context, non-empty Enhanced text wins over Original text;
    - missing or empty Enhanced text always falls back to Original text.

    Clipboard output is plain UTF-8 text. This feature copies text only; it
    does not paste into the active application, synthesize keyboard input, or
    restore the previous clipboard.
16. TXT serialization must be the selected text normalized to LF line endings,
    encoded as UTF-8 without a BOM, and terminated by one LF. Markdown
    serialization must be UTF-8 without a BOM, use this exact structure, and
    terminate by one LF:

    ```markdown
    # Transcription

    **Source:** <display name>
    **Date:** <RFC 3339 timestamp>
    **Duration:** <seconds with three fractional digits>
    **Variant:** <Original|Enhanced>

    <selected text>
    ```

    The display name must be Markdown-escaped as inline content. Selected text
    must be serialized as literal Markdown content: escape Markdown control
    characters and line-leading constructs so headings, links, raw HTML, and
    code fences in the transcript cannot become document metadata or markup.
    Save failures are visible action errors and do not change the transcription
    result.
17. History must persist successful, failed, and cancelled terminal records
    through a repository port backed by the planned SQLite infrastructure. A
    record must include an opaque ID, timestamp, source display name, duration
    when known,
    original text when available, enhanced text when available, mode metadata,
    model metadata, terminal status, stable failure code when applicable, and
    an opaque source reference for a later retry, resolved by the infrastructure
    adapter. The UI must never display or log the full source path by default.
18. History search must match Original and Enhanced text, sort by timestamp
    descending with ID as a stable tie-breaker, and page using a cursor. The
    initial page size is 20, matching the Mac reference. Deletion requires
    confirmation and must remove the history record and any feature-owned
    retained imported audio according to the repository contract.
19. Modes must support list, create, edit, delete, enable/disable, reorder, and
    set-default commands. A mode contract must include ID, name, icon, enabled
    and default flags, language, transcription model ID, optional enhancement
    configuration, formatting flag, output mode, and auto-send preference.
    Auto-send and output delivery are stored configuration only in this feature;
    no global hotkey or text injection is implemented.
20. Dictionary must expose Word Replacements and Vocabulary sections. A
    vocabulary entry has ID, normalized word, created timestamp, and sort mode.
    A replacement has ID, one or more normalized original variants, replacement
    text, created/updated timestamps, and sort mode. Empty values, duplicate
    entries, and invalid variant lists must produce typed validation errors.
    Add, edit, delete, sort, and optional JSON import/export commands must be
    application-owned.
21. AI Models must expose Local, Cloud, and Custom filters and a model card
    contract containing ID, provider, display name, description, language
    capability, streaming capability, availability, download state/progress,
    and safe resource metadata. Download, cancel, delete, import, credential,
    and configuration actions must be represented as typed application commands;
    unavailable providers must show an explicit unavailable state rather than a
    fake model.
22. Audio must expose the page route and a typed snapshot for persisted audio
    preferences, including input-route preference, mute/pause behavior,
    resume delay, and start/stop sound selection. Until the later native-audio
    feature is approved, device enumeration, permission requests, microphone
    capture, recording, playback, mute/pause execution, and sound testing must
    be disabled and must not call Windows APIs.
23. All page controllers must be closeable and must cancel subscriptions,
    in-flight page tasks, and file-import observation without cancelling an
    admitted backend job unless the user explicitly issued Cancel or the
    application is shutting down.

## Non-Functional Requirements

- The dependency direction remains `Presentation -> Application -> Domain <-
  Infrastructure`.
- Domain and Application tests must run without PySide6, Windows APIs, a
  display server, FFmpeg binaries, model weights, SQLite files, or a clipboard.
- FFmpeg must be pinned, checksum-verified, launched without a shell, limited
  to local immutable snapshots, and run with the existing workspace quotas and
  cleanup policy.
- The existing imported-media limits remain authoritative: source 2 GiB,
  derived workspace 768 MiB, decoded duration 32 minutes/30,720,000 samples,
  canonical PCM 64 MiB, stderr 64 KiB, queue capacity 8, stage timeout 30
  minutes, cleanup timeout 5 minutes, and processing deadline 45 minutes.
- Queue operations are O(1) amortized and queue metadata is O(q) for q pending
  entries. Normalization is O(n) in decoded samples with bounded peak memory.
- UI rendering must not block on file I/O, FFmpeg, ASR, SQLite, model downloads,
  save dialogs, or clipboard operations.
- Raw audio, full paths, API keys, complete FFmpeg stderr, and transcript text
  must not appear in ordinary logs, telemetry, or diagnostic artifacts.
- Controls must have keyboard focus, accessible names, disabled-state
  explanations, visible error text, and light/dark semantic theme tokens.

## Error and Cancellation Behavior

Pre-admission source and queue failures are `RejectedRequest` values and do not
create a backend job, workspace, history record, or terminal item. An admitted
job has exactly one terminal result: `Success`, `Failed`, or `Cancelled`.
Cleanup completes before normal terminal publication; cleanup timeout or
recovery is visible as `CleanupWarning` and never becomes a successful result.

Closing the page does not cancel work. Closing the application requests
cancellation for every admitted item and waits for the existing service close
contract. A late completion from an older attempt or closed page must be
discarded by the backend attempt fence and cannot overwrite a newer item.

An enhancement failure preserves the successful original transcript, records a
safe enhancement warning, and leaves Enhanced unavailable. A clipboard or file
save failure preserves the transcript and exposes an action-level error that
can be retried independently.

## Acceptance Criteria

- **AC-001**: Given the desktop shell is running, when each of the six routes is
  selected, then the correct page snapshot and accessible title are rendered
  without importing concrete infrastructure.
- **AC-002**: Given one file for every required matrix entry, when the real
  Windows FFmpeg lane processes valid fixtures, then each file is accepted only
  after FFmpeg opens and decodes its first audio stream into canonical audio.
- **AC-003**: Given corrupt, audio-less, unsupported, or undecodable content,
  when import is attempted, then FFmpeg/content validation rejects it with a
  stable error and ASR invocation count is zero. Valid content remains accepted
  under a misleading filename extension.
- **AC-004**: Given more than queue capacity is admitted, when the next file is
  submitted, then the item is rejected as `QueueFull` without a workspace or
  leaked reservation.
- **AC-005**: Given queued, normalizing, transcribing, or retry-waiting work,
  when the user cancels, then no partial transcript is shown, cleanup runs, and
  exactly one `Cancelled` backend terminal result is visible. Given enhancing
  work after backend `Success`, cancellation stops only enhancement and leaves
  the original successful transcript visible.
- **AC-006**: Given a transient runtime failure, when attempts remain, then the
  item enters `retry_waiting`, uses a new workspace, and returns to the FIFO tail;
  permanent input failures are not automatically retried.
- **AC-007**: Given a successful result with and without enhanced text, when the
  user opens the item, then tabs and clipboard precedence follow requirements
  13-15 and never select empty enhanced text.
- **AC-008**: Given a selected Original or Enhanced variant, when the user saves
  TXT or Markdown, then the exact encoding, line endings, final newline, and
  Markdown structure in requirement 16 are produced atomically.
- **AC-009**: Given history entries with equal timestamps or matching text in
  either variant, when the user searches and loads more, then results are
  cursor-paginated, stable, and match both text fields.
- **AC-010**: Given a microphone/native-audio action is requested from Audio,
  when the later dependency is absent, then the control is disabled with an
  explanation and no native API is called.
- **AC-011**: Given macOS fake adapters, when application/domain tests run, then
  all queue, serialization, routing, and page-state tests pass without PySide6
  or native dependencies.
- **AC-012**: `make spec-check`, `make format-check`, `make lint`, `make test`,
  `make build`, and `make check` pass for the documentation-only change.

## Test Plan

- Unit tests cover route registry completeness, immutable page snapshots,
  command validation, format-matrix mapping, progress invariants, clipboard
  precedence, TXT/Markdown serialization, mode validation, dictionary
  duplicate/variant validation, history cursors, and Audio unavailable guards.
- Application integration tests use fake imported-media, enhancement, history,
  model-catalog, settings, clipboard, and save-file ports. They cover queue
  admission, cancellation races, retry fencing, save/copy failures, and close
  behavior.
- Presentation tests use Qt offscreen fixtures for all route pages, keyboard
  navigation, disabled Audio controls, queue rows, original/enhanced tabs,
  error states, and accessible labels.
- Windows native tests use pinned FFmpeg and licensed fixtures for all 14 input
  labels, valid content under misleading extensions, invalid content with
  supported extensions, audio-less media, process cancellation, quota limits,
  source mutation, cleanup, and diagnostics redaction.
- SQLite integration tests cover migration, insertion/update of terminal
  records, cursor pagination, search over both text variants, deletion, and
  source-reference unavailability.
- No test may claim microphone, WASAPI, native audio, clipboard-to-active-app,
  cloud-provider, or production model-runtime evidence from a macOS run.

## Open Questions and Deferred Work

- Approve the exact SQLite schema and retention policy for imported audio and
  history before implementation of persistence.
- Approve the enhancement provider contract and whether imported transcription
  enhancement is enabled by default for the selected mode. The page contract
  remains optional-enhancement-safe without this decision.
- Approve the Windows file-picker and drag-and-drop adapter details, including
  trusted import roots and user-facing path permission errors.
- Complete the Parakeet runtime/model artifact decision from
  `spec/features/003-parakeet-runtime-integration.md` before claiming real
  transcription on Windows.
- Define cloud-provider credential UX and distribution policy before enabling
  the Cloud model filter actions.
- Define the later native-audio/microphone contract before enabling any Audio
  input, recording, playback, or recording-behavior action.
- Persistent queue recovery, streaming progress, global hotkeys, active-app
  paste/text injection, audio playback, CSV history export, and auto-downloads
  remain deferred.
