# VoiceInk-Win

Windows desktop VoiceInk implementation with local transcription, Parakeet TDT V3, audio/video file import, global shortcuts, history, and a Windows-adapted VoiceInk interface.

The project is in the specification and architecture stage. The macOS VoiceInk repository and the existing Python Windows port are references, not drop-in dependencies.

## Repository Layout

```text
AGENTS.md                  Agent and engineering contract
Makefile                   Development and quality commands
spec/                      Living product and architecture specifications
src/voiceink_win/          Application source
tests/                     Behavior-focused tests
scripts/                   Repository checks and Git hooks
```

## Development Baseline

- Windows 10/11, 64-bit
- Python 3.12-3.14 during the compatibility phase
- CPU fallback and NVIDIA CUDA backend
- Parakeet TDT 0.6B V3 through an isolated ASR runtime

Python 3.15 is not a requirement until PySide6, packaging, audio, and ASR dependencies are verified together.

## Workflow

1. Read `AGENTS.md` and the affected specifications.
2. Create or update a feature specification under `spec/features/`.
3. Implement the smallest behavior covered by the specification.
4. Add or update behavior-focused tests.
5. Run `make check`.
6. Commit the complete logical change with a conventional commit message.

## Commands

Run `make help` for the current command list. The important gates are:

```text
make spec-check
make test
make check
```
