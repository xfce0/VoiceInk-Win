# RFC: Desktop Pages and Imported Transcription

## Status

Status: Proposed. Documentation only; this RFC authorizes no production code.
Implementation remains blocked until the feature specification, imported-media
dependency, persistence decisions, and the relevant runtime/file-system
adapters are approved. Microphone capture and native audio are explicitly a
later dependency and are not part of this RFC.

## Summary

This RFC defines the next Windows desktop UI slice after the current shell and
microphone planning. It ports the Mac reference's six product destinations into
the Windows presentation boundary:

```text
Modes | Transcribe | History | Dictionary | AI Models | Audio
```

The Transcribe page is the only page with a real processing flow in this slice.
It submits local files to the already implemented
`ImportedMediaTranscriptionService`, which owns bounded admission, immutable
source snapshots, FFmpeg normalization, ASR invocation, cancellation, retry,
cleanup, and typed terminal results. The page owns no media decoding or ASR
logic. The output boundary adds explicit Original/Enhanced variant selection,
copy to clipboard, TXT serialization, and Markdown serialization.

The normative product contract is
`spec/features/006-desktop-pages-transcribe.md`. This RFC records architecture,
parity evidence, implementation seams, and review gates.

## Goals

- Provide a route-complete desktop shell for Modes, Transcribe, History,
  Dictionary, AI Models, and Audio.
- Make imported transcription real through the existing imported-media backend,
  not a demo or page-local worker.
- Preserve the architecture direction
  `Presentation -> Application -> Domain <- Infrastructure`.
- Define an implementation-ready, typed contract for page snapshots, commands,
  queue states, progress, cancellation, retry, terminal errors, history, modes,
  dictionary, model catalog, and deferred audio settings.
- Match observable Mac behavior where it is product behavior rather than a
  platform implementation detail.
- Require FFmpeg content/decode validation for the complete 14-format input
  matrix instead of trusting file extensions.
- Make output behavior deterministic, testable, privacy-safe, and independent
  of active-application text injection.

## Non-Goals

- Microphone capture, WASAPI, native audio input, native audio playback, device
  enumeration, permission prompts, mute/pause execution, or recording sounds.
- Global hotkeys, text injection, SendInput, active-application paste, or
  clipboard restoration.
- A second ASR queue, a second media normalizer, a streaming runtime, partial
  transcript publication, or persistent queue recovery.
- Implementing the Parakeet runtime/model artifacts, cloud providers, or model
  download distribution in this documentation change.
- Reproducing SwiftUI/AppKit implementation details, Mac-only controls, or Mac
  file-security APIs.
- CSV export, audio playback in History, transcript analysis, or automatic
  cleanup policy beyond the existing imported-media cleanup contract.

## Proposed Architecture

### Layer ownership

```text
Presentation
  page widgets, view models, route registry, accessibility, Qt dialogs
       |
       v
Application
  page controllers, import coordinator, output coordinator, history queries,
  mode/dictionary/model/settings use cases
       |
       v
Domain <- ports and immutable values
  route/page snapshots, queue state, transcript document, modes, dictionary,
  history records, model capabilities, typed errors
       ^
       |
Infrastructure
  imported-media adapter, FFmpeg artifact, SQLite, file dialogs, file writer,
  clipboard, model catalog, settings storage
```

Presentation depends on Application interfaces and Domain values only.
Application depends on Domain ports and values only. Domain must not import Qt,
subprocess, SQLite, HTTP, FFmpeg, Windows APIs, clipboard types, or runtime
schemas. Infrastructure implements ports and may depend on platform libraries.
The composition root is the only place that wires concrete adapters.

### Mac parity evidence

The following repository files are the parity evidence for observable behavior.
The Windows contract intentionally changes only platform boundaries and the
explicit exclusions in this RFC.

| Mac reference | Evidence used for this RFC |
|---|---|
| `VoiceInk/App/Navigation/ContentView.swift` | Route names and page mapping: Modes, AI Models, Transcribe Audio, History, Audio, Dictionary. |
| `VoiceInk/Features/AudioImport/Views/AudioTranscribeView.swift` | Empty drop zone, Choose Files, multiple-file queue, Add/Start/Cancel/Clear controls, mode selection, drag overlay. |
| `VoiceInk/Features/AudioImport/State/AudioFileQueueItem.swift` | Pending, processing phase, completed, and failed item presentation model. |
| `VoiceInk/Features/AudioImport/Views/AudioFileRow.swift` | Waiting/progress/error rows, retry, Original/Enhanced tabs, copy/save actions, duration. |
| `VoiceInk/Features/AudioImport/Workflows/AudioFileTranscriptionManager.swift` | Sequential queue processing, loading/audio/transcription/enhancement phases, persistence after success. |
| `VoiceInk/Features/History/Views/HistoryView.swift` | Search over both variants, page size 20, cursor pagination, selection, delete confirmation, detail panel. |
| `VoiceInk/Features/History/Views/TranscriptionDetailView.swift` | Original and optional Enhanced detail presentation. |
| `VoiceInk/VoiceInk/Infrastructure/Persistence/Models/Transcription.swift` | Original/enhanced text, duration, timestamp, source audio, model, mode, prompt, and status metadata. |
| `VoiceInk/Features/Modes/Views/ModeView.swift` and `Features/Modes/State/ModeConfig.swift` | Mode list, add/edit/settings panels, enable/default state, model/language/enhancement/output fields. |
| `VoiceInk/Features/Dictionary/Views/DictionarySettingsView.swift`, `VocabularyView.swift`, `WordReplacementView.swift` | Two dictionary sections, add/edit/delete, sorting, validation, and settings panel. |
| `VoiceInk/Features/ModelLibrary/Views/ModelManagementView.swift` | Local/Cloud/Custom filters, model cards, settings/provider panels, download/import/delete actions. |
| `VoiceInk/Features/AudioSettings/Views/AudioSetupView.swift` | Audio input, priority order, recording behavior, and recording sound sections; all native-audio actions are deferred here. |
| `VoiceInk/Infrastructure/Audio/SupportedMedia.swift` | Mac's user-facing supported media intent. Windows uses the exact required matrix below and excludes extra `OGA`. |
| `VoiceInk/Features/History/Components/SaveIconButton.swift` and `Infrastructure/SystemIntegration/Paste/ClipboardManager.swift` | TXT/Markdown save actions and enhanced-first copy fallback. Windows makes encoding, variant, and error behavior explicit. |

### Page contracts

The following are conceptual contracts. They define names and semantics, not a
mandate to create one class per item.

#### Navigation

```text
DesktopRoute = modes | transcribe | history | dictionary | ai_models | audio

RouteDescriptor:
  route: DesktopRoute
  title: non-empty display text
  icon: repository-owned icon identifier
  tooltip: non-empty safe text
  accessibility_label: non-empty safe text

NavigationSnapshot:
  selected_route: DesktopRoute
  routes: tuple[RouteDescriptor, ...]
```

Route selection is a typed application command. Unknown route IDs are ignored
with a diagnostic counter and cannot select an arbitrary widget.

#### Modes

```text
ModeSummary:
  id: opaque ModeId
  name: non-empty display name
  icon: IconDescriptor
  is_enabled: bool
  is_default: bool
  selected_language: LanguageCode | auto
  transcription_model_id: ModelId | none
  enhancement: EnhancementConfiguration | none
  text_formatting_enabled: bool
  output_mode: paste | respond | custom_command
  auto_send: none | enter | shift_enter | command_enter

ModesPageSnapshot:
  modes: tuple[ModeSummary, ...]
  selected_mode_id: ModeId | none
  panel: none | create | edit(ModeId) | settings
  error: PageError | none
```

Commands are `create`, `update`, `delete`, `set_enabled`, `set_default`,
`reorder`, `open_editor`, `close_panel`, and `select`. Delete of a default mode
must be rejected by the application or must first select a replacement through
an explicit command. A mode is configuration data only in this slice; it does
not install shortcuts or deliver text.

#### Transcribe

```text
TranscribePageSnapshot:
  items: tuple[TranscriptionQueueItemSnapshot, ...]
  selected_mode_id: ModeId | none
  is_processing: bool
  can_start: bool
  accepting_files: bool
  aggregate: QueueAggregate
  page_error: PageError | none

TranscriptionQueueItemSnapshot:
  item_id: opaque QueueItemId
  job_id: JobId | none
  attempt: positive integer
  source_name: safe basename
  source_format_hint: MatrixLabel | unknown
  state: QueueState
  progress: ProgressSnapshot
  result: TranscriptDocument | none
  failure: ImportFailure | none
  can_remove: bool
  can_cancel: bool
  can_retry: bool
  selected_variant: original | enhanced
```

Commands are `add_paths`, `drop_paths`, `remove_pending`, `start_queue`,
`cancel_item`, `cancel_all`, `retry_item`, `clear_terminal_items`,
`select_mode`, `select_variant`, `copy`, `save_txt`, and `save_markdown`.
The application returns typed command outcomes; a UI event is not permission
to bypass validation or mutate a terminal result.

Each path in a multi-file add/drop is validated independently in input order.
Valid paths remain page-level `pending` items until `start_queue`; an invalid
path produces an item-level `RejectedRequest` without creating a backend job.
`start_queue` submits pending paths in order to the existing service, which
performs admission and queue-capacity reservation atomically per path. A
`QueueFull` result leaves that path unadmitted and visible for a later retry;
the page never creates a second backend queue and the backend never admits more
than its existing capacity of eight jobs.

#### History

```text
HistoryEntry:
  id: opaque HistoryId
  created_at: RFC 3339 timestamp
  source_name: safe basename
  duration_seconds: non-negative finite number | none
  original_text: string | none
  enhanced_text: non-empty string | none
  mode: ModeMetadata | none
  transcription_model: ModelMetadata | none
  enhancement_model: ModelMetadata | none
  status: succeeded | failed | cancelled
  failure: ImportFailure | none
  source_reference: opaque SourceReference | none

HistoryPageSnapshot:
  query: string
  entries: tuple[HistoryEntry, ...]
  next_cursor: HistoryCursor | none
  is_loading: bool
  selected_ids: frozenset[HistoryId]
  panel: none | detail(HistoryId) | settings
  error: PageError | none
```

The repository query is `search(query, cursor, limit=20)` and matches original
and enhanced text. The cursor is `(created_at, id)` in descending order. Detail
uses the same `TranscriptDocument` and output coordinator as Transcribe.

#### Dictionary

```text
VocabularyEntry:
  id: opaque DictionaryEntryId
  word: normalized non-empty string
  created_at: RFC 3339 timestamp

ReplacementEntry:
  id: opaque DictionaryEntryId
  originals: tuple[normalized non-empty string, ...]
  replacement: normalized non-empty string
  created_at: RFC 3339 timestamp
  updated_at: RFC 3339 timestamp

DictionaryPageSnapshot:
  section: replacements | vocabulary
  vocabulary: tuple[VocabularyEntry, ...]
  replacements: tuple[ReplacementEntry, ...]
  sort: typed sort value
  settings_panel: none | open
  validation_error: PageError | none
```

Application commands validate Unicode-normalized, trimmed values, reject
duplicates, and persist atomically. Multiple original variants are comma
separated at the presentation boundary and a tuple in the application/domain
contract. Optional JSON transfer must use a versioned archive and preview
conflicts before writing.

#### AI Models

```text
ModelSummary:
  id: opaque ModelId
  provider: local | cloud | custom
  display_name: non-empty string
  description: safe string
  languages: tuple[LanguageCode, ...]
  supports_streaming: bool
  availability: available | unavailable | misconfigured | unsupported
  install_state: not_installed | downloading | installed | failed
  download_fraction: float | none
  safe_resource_metadata: ModelResourceMetadata | none

ModelsPageSnapshot:
  filter: local | cloud | custom
  models: tuple[ModelSummary, ...]
  panel: none | settings | provider(ModelId) | custom_model
  error: PageError | none
```

The model catalog is a port. A missing runtime/model is an explicit status,
never a fake successful model. Secrets are supplied by a credential port and
never included in snapshots, logs, SQLite records, or model-card diagnostics.

#### Audio

```text
AudioSettingsSnapshot:
  input_route: system_default | selected_device(opaque DeviceId) | priority_order
  mute_while_recording: bool
  pause_media_while_recording: bool
  resume_delay_seconds: finite non-negative number
  start_sound: none | built_in(SoundId) | custom(SoundId)
  stop_sound: none | built_in(SoundId) | custom(SoundId)
  capabilities:
    native_input: unavailable
    native_playback: unavailable
    device_enumeration: unavailable

AudioPageSnapshot:
  settings: AudioSettingsSnapshot
  disabled_reason: NativeAudioDeferred
  error: PageError | none
```

The page may read/write non-native preferences through a settings repository
only if that repository is already available. It must not enumerate devices,
open a capture session, play a sound, request permission, or call WASAPI. All
native-dependent controls are disabled and explain the later dependency.

### Input and FFmpeg contract

The required matrix is authoritative:

| Label | Container/codec family accepted by FFmpeg | First stream rule |
|---|---|---|
| WAV | RIFF/WAVE | `0:a:0` |
| MP3 | MPEG audio | `0:a:0` |
| M4A | ISO Base Media audio | `0:a:0` |
| AIFF | AIFF/AIFF-C | `0:a:0` |
| MP4 | ISO Base Media audio/video | `0:a:0` |
| MOV | QuickTime audio/video | `0:a:0` |
| AAC | AAC elementary stream | `0:a:0` |
| FLAC | FLAC | `0:a:0` |
| CAF | Core Audio Format | `0:a:0` |
| AMR | AMR-NB/AMR-WB | `0:a:0` |
| OGG | Ogg audio | `0:a:0` |
| OPUS | Opus audio | `0:a:0` |
| 3GP | 3GPP audio/video | `0:a:0` |
| WEBM | WebM/Matroska audio/video | `0:a:0` |

An extension is used only to filter the picker and produce a display hint. The
application must validate a canonical local regular file, make the existing
immutable source snapshot, and invoke the existing FFmpeg normalizer. FFmpeg
must open the bytes, select `0:a:0`, decode them, and produce mono 16 kHz
S16LE RIFF/WAV. Strict output validation remains the existing bounded parser;
the application must reject empty, malformed, truncated, wrong-format, or
quota-exceeding output before ASR. `OGA` is not a v1 required input label.

The service must keep the existing security contract: no shell interpolation,
no URLs/network protocols, no symlinks/reparse points/UNC paths by default,
checksum-verified FFmpeg, bounded stderr, bounded source/workspace/duration/
PCM output, Windows Job Object process containment, immutable snapshot identity
and hash verification, attempt fencing, and cleanup on every terminal path.

### Queue, progress, cancellation, retry, and errors

The UI state maps to the existing backend as follows:

| UI state | Existing backend state/stage | Visible behavior |
|---|---|---|
| `pending` | not admitted | Waiting; removable |
| `validating` | admission/normalization preflight | Checking media; no invented percentage |
| `queued` | `queued` | Waiting; cancellable |
| `normalizing` | `normalizing` | Converting audio; indeterminate unless measured |
| `transcribing` | `transcribing` | Transcribing; indeterminate unless measured |
| `enhancing` | application post-processing | Enhancing; optional and cancellable |
| `retry_waiting` | `retry_waiting` | Retrying with safe countdown/indeterminate status |
| `cleaning_up` | `cleaning_up` | Finishing; no result until cleanup fence |
| `succeeded` | `succeeded` | Transcript and output actions |
| `failed` | `failed` | Stable code/message; manual retry when permitted |
| `cancelled` | `cancelled` | No transcript; optional manual retry |
| `rejected` | `RejectedRequest` | No job ID/workspace; fix input or queue state |

Progress is `stage`, optional `fraction`, optional `queue_position`, and
optional safe `message`. Fractions are absent unless provided by a trusted
adapter; elapsed time is not a progress source. The existing backend's typed
terminal union is authoritative and the page must retain its `job_id` and
attempt for every admitted item. The application exposes progress and terminal
updates through a closeable observation subscription; closing that subscription
does not cancel the admitted job.

```text
ObservationSubscription:
  next(timeout) -> ImportObservation | EndOfStream
  close() -> None

ImportObservation:
  job_id: JobId
  attempt: positive integer
  stage: backend stage
  progress: Progress
  terminal: TerminalResult | none
```

The existing imported-media service must add this non-blocking observation
surface alongside `submit`, `wait`, and `cancel`; it must publish snapshots from
the same job state machine and must not introduce a second worker queue.

User cancellation is request-scoped. Queued cancellation must avoid worker
execution; running cancellation signals the active adapter/runtime, discards
late output, cleans the workspace, and publishes `Cancelled` only after the
cleanup fence. A page close is not cancellation. Automatic retry is only for
classified transient runtime failures and is capped at two total attempts.
Manual retry starts a new admission flow and cannot skip source or FFmpeg
validation. Permanent input failures are not automatically retried.

Enhancement starts only after the imported-media service has published backend
`Success` and cleanup has completed. It is an application task over the
successful original transcript, not a second backend stage. Cancelling or
failing that task preserves the original success and records a safe warning;
it never calls backend `cancel` and never changes the backend terminal result.

### Transcript variants and output

```text
TranscriptDocument:
  source_name: safe basename
  created_at: RFC 3339 timestamp
  duration_seconds: non-negative finite number
  original_text: non-empty string
  enhanced_text: non-empty string | none
  selected_variant: original | enhanced
  mode: ModeMetadata | none
  model: ModelMetadata | none
```

The variant resolver applies this precedence: explicit requested variant,
currently selected visible tab, then Enhanced when non-empty, then Original.
An unavailable Enhanced selection resolves to Original. This same resolver is
used by Transcribe, History, collapsed-row Copy, and detail output actions.

TXT is selected text only, UTF-8 without BOM, LF line endings, and one final
LF. Markdown is UTF-8 without BOM, LF line endings, and one final LF:

```markdown
# Transcription

**Source:** <display name>
**Date:** <RFC 3339 timestamp>
**Duration:** <seconds with three fractional digits>
**Variant:** <Original|Enhanced>

<selected text>
```

The save port performs an atomic write after the user chooses a destination.
The clipboard port writes plain text and verifies the write. Clipboard and file
errors are action failures only; the transcript remains successful and can be
retried. No active-app paste or clipboard restoration is included.

Markdown serialization escapes the source display name as inline content and
serializes transcript lines so Markdown control characters, line-leading
constructs, raw HTML, and code fences remain literal transcript content.

### Application ports

The composition root must be able to provide these seams without concrete
platform types in Domain/Application:

```text
ImportedMediaPort
  submit(source_path, ImportOptions) -> JobId | RejectedRequest
  wait(job_id, deadline) -> TerminalResult
  observe(job_id) -> closeable ObservationSubscription
  cancel(job_id) -> bool

EnhancementPort
  enhance(document, EnhancementConfiguration) -> EnhancementResult

HistoryRepository
  search(query, cursor, limit) -> HistoryPage
  save_terminal(record) -> None | PersistenceError
  delete(ids) -> None | PersistenceError

ModeRepository, DictionaryRepository, ModelCatalogPort, AudioSettingsRepository
  load/save/query operations over immutable domain values

ClipboardPort
  copy(text) -> CopyResult

TextFilePort
  choose_save_target(suggested_name, format) -> SaveTarget | UserCancelled
  write_atomic(target, utf8_bytes) -> SaveResult
```

`ImportedMediaPort` is an application-facing adapter over the existing service;
it must not duplicate `MediaNormalizer`, `ImportQueue`, or `AsrApplicationService`.
`EnhancementPort` is optional application post-processing and must preserve the
successful original transcript when enhancement fails. `SourceReference` is an
opaque domain value; only the infrastructure adapter knows how to resolve it.
`ModelCatalogPort` may return unavailable capability values while the Parakeet
or cloud dependency is unresolved. `AudioSettingsRepository` must be a no-op or
safe preference adapter for native-dependent values until the later audio RFC.

### Composition boundary and lifecycle

This RFC does not silently expand the exact no-resource
`build_desktop_composition()` contract defined by
`rfcs/desktop-composition-boundary.md`. The page contracts and injected fake-port
tests may be implemented independently, but production wiring for the imported
media service, history repository, model catalog, settings repositories,
clipboard, and save-file ports is blocked until that composition RFC is revised
to define ownership, close order, shutdown cancellation, and rollback for
those resources. The implementation must not add a fake backend or resource
override to the current builder as a shortcut.

### Implementation sequence

1. Approve this RFC and the feature specification without changing production
   code.
2. Add domain page contracts and output serialization tests without Qt.
3. Add application controllers and injected fake ports; connect Transcribe to
   the existing imported-media service.
4. Add SQLite history migrations and repository integration.
5. Add the six PySide6 pages, route registry, dialogs, and offscreen tests.
6. Add Windows FFmpeg fixture matrix validation and native file-picker/
   clipboard/save evidence.
7. Review model/runtime and enhancement dependencies separately before enabling
   those actions. Do not enable Audio native actions under this RFC.

## Exit Criteria

This RFC may move to Approved when:

- `spec/features/006-desktop-pages-transcribe.md` and this RFC are catalogued
  with matching scope and dependency statements;
- the architecture review accepts the Presentation/Application/Domain/
  Infrastructure seams and confirms no page-local backend logic;
- the 14-format matrix, FFmpeg content/decode validation, first-audio-stream
  rule, quotas, error mapping, and source security contract are accepted;
- queue state, progress semantics, cancellation, retry, cleanup, and attempt
  fencing are accepted against the existing imported-media backend;
- transcript variant precedence and TXT/Markdown/clipboard contracts have
  deterministic test vectors;
- history schema/retention and file-dialog/import-root decisions are recorded;
- the Parakeet runtime is available for an implementation/test lane, or the
  model page explicitly remains unavailable without synthetic success;
- implementation is separately planned and the catalog still records
  `implementation_allowed: false` until all blocking decisions are closed.

The feature is Complete only after the feature acceptance criteria pass, the
Windows FFmpeg matrix evidence is recorded, SQLite migrations pass, PySide6
offscreen tests pass, and no native-audio behavior is claimed by this slice.

## Open Questions

These are explicit decisions for implementation review, not permission to hide
behavior in code:

1. Which SQLite retention policy applies to imported source references and
   retained normalized audio? The page contract uses an opaque reference and
   does not expose a full path.
2. Is enhancement enabled by default for a mode, and what exact application
   enhancement port should wrap a successful imported-media result? The UI
   remains correct when enhancement is unavailable.
3. Which Windows picker/trusted-root policy should the file adapter use for
   reparse points, UNC paths, removable media, and access-denied sources?
4. Which approved Parakeet artifact/model configuration supplies real model
   availability for the Local filter? No fake model may report installed or
   transcribable status.
5. What cloud-provider credential and network-consent policy is required before
   enabling Cloud model actions? Until resolved, Cloud may be read-only or
   unavailable.
6. Should failed/cancelled terminal outcomes appear in History by default, or
   only successful transcript records? The schema supports all three statuses;
   the default filter is a product decision.
7. The later microphone/native-audio RFC must define when Audio controls become
   enabled. This RFC must not be amended to add capture as an implicit
   dependency.
