# Persistence Foundation

## Status and Scope

Status: implemented.

This slice provides the durable local storage boundary without wiring
recording, WASAPI, global hotkeys, or presentation pages.

## User Scenarios

An application caller can save and reload transcription history, dictionary
rules, and settings without owning a database connection. An optional audio
artifact can be written to the VoiceInk application-data directory while the
history row retains only its relative reference.

## Functional Requirements

1. SQLite access is serialized on a dedicated background worker and public
   operations return futures.
2. Versioned SQL migrations create history, dictionary, settings, and migration
   metadata tables at startup.
3. History supports atomic upsert, newest-first pagination, and deletion.
4. Dictionary rules support atomic upsert, deterministic ordering, and deletion.
5. Settings round-trip language, mode, hotkeys, auto-copy, and model/audio
   preferences.
6. Audio references are relative and traversal-safe; audio bytes are not stored
   in SQLite.

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
  operation raises a typed path error and leaves the root untouched.

## Test Plan

Behavior tests use temporary SQLite files and cover migrations, settings,
dictionary CRUD/order, history pagination/deletion, concurrent submissions,
rollback on reconciliation failure, and path traversal rejection.

## Open Questions and Deferred Work

Microphone capture, WASAPI, hotkey registration, recording orchestration,
history UI, and presentation integration remain outside this feature.
