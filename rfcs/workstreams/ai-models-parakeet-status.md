# RFC: AI Models Parakeet Status

## Status

Approved for implementation on `feature/ai-models-parakeet-status`.

## Decision

Add an `AI Models` page that presents the existing Parakeet runtime metadata:
name, model ID, artifact version/revision, backend, trust result, availability,
and a redacted model path. The page reads the existing packaged-runtime
descriptor or validated runtime manifest/artifact lock. It does not start the
sidecar, download model files, or expose absolute paths, credentials, or other
secrets.

When the approved package or runtime metadata is absent or invalid, the page
keeps the Parakeet identity visible and reports an unavailable state with a
safe placeholder path. English and Russian translations cover every new label
and state. Unit tests cover metadata discovery and redaction; GUI tests cover
navigation, rendering, and locale changes.
