# RFC: Dictionary Visual Redesign

## Status

Proposed. This workstream changes only the Dictionary presentation and its GUI
coverage; persistence, CRUD semantics, and the global theme contract remain
unchanged.

## Summary

Replace the current split list/editor presentation with a clear page header, a
compact list of dictionary rows, and an inline editor. Each row exposes Edit
and Delete actions without a separate dialog. Loading, empty, persistence
error, and validation states remain visible and actionable.

## Goals

- Make dictionary rules scannable through phrase and replacement hierarchy.
- Keep add, edit, enable/disable, and delete operations on the same page.
- Preserve asynchronous `DictionaryPort` calls and SQLite persistence exactly.
- Provide English and Russian copy, accessible names, keyboard focus, and
  deterministic offscreen GUI tests.

## Non-Goals

- Applying rules during transcription.
- Changing `DictionaryEntry`, persistence schema, ordering, or uniqueness.
- Adding theme tokens or changing the global theme API.

## Proposed Architecture

`DictionaryPage` keeps its existing persistence boundary and async generation
guards. A page-local row widget renders each entry and emits typed Edit/Delete
requests back to the page. Existing `ThemeTokens` are reused by adding
Dictionary-specific selectors to the generated stylesheet. State copy is kept
in the localization catalog with matching English/Russian keys.

## Exit Criteria

- Existing Dictionary CRUD and persistence tests pass unchanged in behavior.
- GUI tests cover loading, empty, error, validation, inline actions, locale,
  and keyboard focus.
- Targeted Ruff and pytest checks pass.
- No global theme token, schema, or transcription behavior changes are needed.

## Open Questions

Rule application in the transcription pipeline remains a separate workstream.
