# RFC: History Newest-First Ordering

## Status

Status: Implemented. This workstream hardens the existing history query and
presentation path without changing the visual design, storage format, or audio
artifact behavior.

## Summary

History records are returned in one deterministic order for the initial load,
cursor pagination, and search: `created_at DESC, id DESC`. The timestamp is the
newness signal; the opaque record ID is the stable tie-breaker for equal
timestamps. The cursor encodes the same two values, so a page boundary cannot
duplicate or skip records.

## Goals

- Show newest transcription records first on initial load.
- Preserve the same order across cursor pagination and search results.
- Make equal-timestamp ordering deterministic with the record ID.
- Cover the persistence-to-Qt path with regression tests.
- Keep the change limited to the existing SQL, domain contract, and history UI.

## Non-Goals

- No visual redesign or navigation changes.
- No changes to audio files, artifact retention, or storage paths.
- No new history schema, migration, or ordering preference.

## Proposed Architecture

The existing boundary remains unchanged:

```text
HistoryPage UI -> PersistenceService -> SQLitePersistence
                         ^
                         |
                  HistoryPage domain value
```

SQLite applies the descending `(created_at, id)` order and uses the same tuple
for the cursor predicate. The domain page documents this ordering contract. The
Qt page resets cursor state when starting an initial or search load and renders
records in the order supplied by persistence.

## Exit Criteria

- Initial load, pagination, search, and equal-timestamp ordering are covered by
  SQLite regression tests.
- An offscreen Qt test verifies the same order through initial load, next/prev
  pagination, and search over both transcript variants.
- Targeted tests and lint pass.
- The change does not modify visual theme/redesign or audio/storage behavior.

## Open Questions

None for this workstream. The existing opaque record ID is the agreed stable
tie-breaker.
