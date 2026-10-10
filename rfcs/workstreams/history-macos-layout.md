# RFC: History macOS Layout

## Status

Status: Approved for the presentation-only workstream.

## Goal

Make the Windows History page read like the VoiceInk macOS history list: a quiet
vertical list of transcript rows, each showing source metadata and a two-line
preview. Longer text remains collapsed with an ellipsis and is revealed by
selecting the row. Search, newest-first pagination, localization, copy, export,
and delete behavior remain available.

## Boundaries

- Change only the presentation layer, presentation localization, and shared Qt
  stylesheet tokens needed by History.
- Reuse `PersistenceService`, `HistoryDeletionService`, `QtClipboardPort`, and
  `TextFilePort`; do not add or change a storage backend, schema, or migration.
- Audio playback and folder reveal are optional injected ports. They receive only
  existing opaque references. If either data or port is absent, its visible
  action is disabled and labelled unavailable.
- The composition root does not enable new native behavior in this workstream.

## Acceptance

- History rows show a two-line preview and reveal the complete selected text on
  click without a second detail panel.
- Copy uses the existing clipboard port. Audio and folder actions never access
  SQLite or the filesystem directly.
- Search and cursor/offset pagination keep their current asynchronous behavior.
- English and Russian labels update when the shared locale changes.
- Offscreen GUI tests cover preview/collapse, expansion, copy, unavailable
  actions, and injected action ports.
