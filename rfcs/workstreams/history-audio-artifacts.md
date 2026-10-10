# RFC: History Audio Artifacts

## Status

Implemented in the backend/data slice. Presentation receives seams for future
audio playback and folder actions; this RFC does not add UI controls.

## Decision

Successful normalized transcriptions may retain a local audio copy under the
VoiceInk application-data directory:

```text
%LOCALAPPDATA%/VoiceInk/audio/history/<sha256(job-id)>.wav
```

The artifact is a deterministic mono, 16 kHz, signed PCM16 RIFF/WAV file. The
source filename, source path, transcript, and user metadata are not written to
the artifact. SQLite stores only the validated relative `audio_artifact_path`
reference on the history row. Existing rows with `NULL` keep working and do
not acquire an artifact retroactively.

## Safety And Quotas

- The default per-artifact limit is 64 MiB of PCM plus the WAV header.
- The default aggregate artifact quota is 512 MiB.
- Writes use a temporary file, `fsync`, and atomic replacement.
- Names are derived from a job identifier, never from user-controlled source
  names. Relative paths reject traversal, absolute paths, separators from the
  wrong platform, symlinks, and non-regular files.
- A quota or filesystem failure does not discard an otherwise successful
  transcript; it produces a history record without audio and is logged without
  raw audio or full paths.

## Lifecycle And API

The import application saves the normalized audio before publishing the
successful history record. History deletion tombstones the row, removes its
artifact through the validated store, then finalizes the row. Pending
deletions remain retryable across restart. `reveal_path(relative_path)` returns
an existing regular path only after revalidation; `folder_path` is the
validated app-owned directory for a future presentation action.

The existing persistence migration already supplies the nullable history
reference (`002_persistence_hardening.sql`), so this slice requires no schema
change. No audio bytes are stored in SQLite.
