# VoiceInk-Win Project Context

This file is the tracked, public project passport. It is the companion to the local ignored `AGENTS.md`. New sessions and contributors should read this file first, then read the local `AGENTS.md` when it exists.

## What This Project Is

VoiceInk-Win is an open-source Windows desktop voice-to-text application inspired by VoiceInk for macOS. It is a new Windows implementation, not a literal Swift port.

The intended product includes local Parakeet TDT 0.6B V3 transcription, CPU fallback, NVIDIA CUDA acceleration, microphone recording, global hotkeys, audio/video file transcription, a queue with cancellation and retry, history, modes, prompts, word replacements, optional AI enhancement, text delivery to the active application, system tray, and a floating overlay.

The product is privacy-first. Local audio stays local unless a cloud provider is explicitly selected.

## References

- macOS product reference: `https://github.com/xfce0/VoiceInk`
- Existing Python Windows prototype: `https://github.com/abhijitchirde/voiceink-windows`
- This project: `https://github.com/xfce0/VoiceInk-Win`

The macOS project is the reference for behavior and visual language. The existing Windows prototype is a reference for Windows integration ideas. Neither is a drop-in implementation.

## Current Status

The foundation and imported-media backend are implemented. The following are complete:

- specification-driven project layout;
- architecture baseline;
- Python package baseline;
- Makefile quality gates;
- local Git hooks;
- public project context;
- foundation/runtime boundary and fake adapter tests;
- imported-media queue, normalization, cancellation, retry, and cleanup contracts;
- Windows diagnostic build and manual ARM64 smoke validation.

The following are not implemented:

- PySide6 UI, tray, and overlay;
- production Parakeet runtime/model management;
- microphone/WASAPI recording;
- Windows hotkeys, clipboard, and text input;
- SQLite history;
- modes, enhancement, cloud providers, and packaging;
- Windows CI runtime smoke and NVIDIA validation.

## Current Milestone

Work 3 is `Parakeet Runtime Integration`, specified in
`rfcs/parakeet-runtime-integration.md` and
`spec/features/003-parakeet-runtime-integration.md`. Its status is `Proposed`.

After RFC approval, adapter/protocol/configuration and fake-sidecar tests may
be implemented with synthetic artifacts. Native smoke, release validation, and
the `Complete` status remain blocked until the RFC's runtime/model artifact,
fixture, and quality-gate decisions are resolved.

## Planned Architecture

```text
Presentation -> Application -> Domain <- Infrastructure
```

- Presentation: PySide6/Qt windows, tray, overlay, and view models.
- Application: use cases, queues, cancellation, lifecycle, and orchestration.
- Domain: platform-independent models, state machines, ports, and rules.
- Infrastructure: Windows APIs, audio, FFmpeg, SQLite, credentials, hotkeys, clipboard, and ASR runtime.

The first ASR direction is a long-lived local `NeMo-Speech.cpp` sidecar with a pinned Parakeet V3 GGUF model. The application talks to it through a narrow ASR port. Microphone and imported media input converge to mono 16 kHz PCM.

Parakeet V3 is treated as offline/batch transcription in the first milestone. Real-time streaming requires a separate future decision and possibly a different model.

## Development Workflow

1. Read this file, local `AGENTS.md`, the relevant RFC, and specifications.
2. Work on a branch prefixed with `feature/`, `fix/`, `refactor/`, `docs/`, `test/`, or `chore/`.
3. Update or create the specification before non-trivial code.
4. Implement and test one logical slice.
5. Run `make check`.
6. Commit the complete logical change.
7. Run `make push`; it verifies the branch and quality gates and publishes only the feature branch.
8. Open a pull request into `main`.
9. Wait for all GitHub checks to pass.
10. A human maintainer selects `Squash and merge`.
11. Agents do not merge into `main`.

Direct pushes to `main` are prohibited by project policy. The local hook and Makefile are developer safeguards; GitHub branch protection is the authoritative remote safeguard.

## Quality Gates

```text
make spec-check
make format-check
make lint
make test
make build
make check
make push
```

Python development targets 3.12-3.14 until the full dependency set supports a newer interpreter. Mac tests can cover domain/application code, fake adapters, FFmpeg, SQLite, and UI components. They cannot prove Windows APIs, WASAPI, CUDA, Windows packaging, or Windows clipboard behavior.
