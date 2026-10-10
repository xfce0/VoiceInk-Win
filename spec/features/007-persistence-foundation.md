# Persistence Foundation

## Status and Scope

Status: implemented.

This slice provides the durable local storage boundary used by the desktop
composition without wiring recording, WASAPI, or global hotkeys. The desktop
composition configures SQLite and the audio-artifact root even when the
imported-media runtime is unavailable.

## User Scenarios

An application caller can save and reload transcription history, dictionary
rules, and settings without owning a database connection. An optional audio
artifact can be written to the VoiceInk application-data directory while the
history row retains only its relative reference.

## Functional Requirements

1. SQLite access is serialized on a dedicated background worker and public
   operations return futures.
2. Versioned SQL migrations create history, dictionary, settings, and migration
   metadata tables at startup; packaged migrations are required and checksummed
   for drift detection.
3. History supports atomic upsert, stable cursor/newest-first pagination,
   search, selected variants, and durable tombstone deletion.
4. Dictionary rules support atomic upsert, deterministic ordering, and deletion.
5. Settings round-trip language, mode, hotkeys, auto-copy, and model/audio
   preferences, with field-level atomic updates so pages cannot overwrite one
   another's changes.
6. Audio references are relative and traversal-safe; audio bytes are not stored
   in SQLite.
7. Successful normalized imports may save a private RIFF/WAV artifact under
   app storage with a generated safe name and an aggregate quota.
8. History deletion removes the linked artifact before finalizing the durable
   row deletion; old rows without an artifact remain valid.
9. The storage adapter exposes a revalidated artifact path and app-owned folder
   seam for future presentation audio/folder actions.

## Non-Functional Requirements

Startup configures WAL, foreign keys, and a bounded busy timeout. SQL is
parameterized, writes have explicit transaction boundaries, and the adapter is
portable across non-Windows test hosts.

## Error and Cancellation Behavior

Migration, reconciliation, SQL, and path-validation failures complete their
future with an exception and do not silently succeed. Failed transactions are
rolled back. Closing rejects new submissions and completes after queued work
and the SQLite connection have been released. Cancellation is not exposed for
individual SQLite operations; callers can ignore their future.

## Acceptance Criteria

- Given a new database, when the worker becomes ready, then all schema tables
  exist and WAL is enabled.
- Given multiple concurrent callers, when they submit writes, then every
  completed future reflects a serialized durable operation.
- Given history records, when a page is requested, then records are ordered by
  newest timestamp and deterministic ID tie-breaker.
- Given an unsafe audio path, when it is resolved or persisted, then the
  operation completes its Future with a typed path error and leaves the root
  untouched.
- Given a successful normalized import, when history is persisted, then its
  relative artifact reference points to a normalized private WAV copy, unless
  the configured artifact quota is exhausted.
- Given an artifact path, when a reveal path is requested, then traversal,
  symlink, missing, and non-regular paths are rejected.

## Test Plan

Behavior tests use temporary SQLite files and cover the frozen/package migration
contract, checksum drift, migration rollback, settings field updates,
dictionary CRUD/order and Unicode canonical uniqueness, cursor history
pagination/search, tombstone deletion, concurrent submissions, and path
traversal rejection. Audio artifact behavior covers normalized WAV encoding,
safe generated names, quota enforcement, reveal validation, and history-link
publication.

## Open Questions and Deferred Work

Microphone capture, WASAPI, hotkey registration, and recording orchestration
remain outside this feature. The desktop composition now consumes this
boundary, and its history page uses the application persistence and deletion
services; detailed page behavior remains specified by the desktop-pages RFC.
