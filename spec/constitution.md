# VoiceInk-Win Constitution

## Purpose

VoiceInk-Win is a privacy-first Windows voice-to-text application. The project prioritizes dependable local transcription, fast user feedback, and a polished interface over broad but fragile feature coverage.

## Principles

### Specifications Are the Source of Truth

Product behavior, system boundaries, acceptance criteria, and operational limits are specified before implementation. Code and tests must implement the current specifications. Any discovered mismatch is resolved by updating the specification and then the implementation.

### Local First

Audio remains on the machine for local transcription. Cloud providers are explicit opt-in integrations and must expose their data and failure behavior to the user.

### Explicit Platform Boundaries

Windows APIs, Qt, FFmpeg, model runtimes, and credential stores stay behind infrastructure ports. Domain and application code must remain testable without Windows, CUDA, or a GUI.

### Failure Is Visible

Audio-device failures, model download failures, GPU incompatibilities, conversion failures, cancellation, and paste failures must produce actionable user-visible state and diagnostic logs without leaking sensitive data.

### Clean Code Over Clever Code

Prefer cohesive modules, explicit types, small functions, deterministic state transitions, and the smallest correct change. Avoid hidden global state, broad exception swallowing, unbounded queues, and magic timing delays.

### Evidence Before Claims

Performance and compatibility claims must be backed by reproducible measurements that record hardware, operating system, Python, model, runtime, and dependency versions.

## Quality Bar

A change is complete only when the affected specification, implementation, behavior-focused tests, and validation commands agree. `make check` must pass before handoff.
