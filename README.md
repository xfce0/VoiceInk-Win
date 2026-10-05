# VoiceInk-Win

Windows desktop VoiceInk implementation with local transcription, Parakeet TDT V3, audio/video file import, global shortcuts, history, and a Windows-adapted VoiceInk interface.

The project is in the specification and architecture stage. The macOS VoiceInk repository and the existing Python Windows port are references, not drop-in dependencies.

## Repository Layout

```text
PROJECT_CONTEXT.md         Public project description and current status
AGENTS.md                  Local agent and engineering contract (ignored)
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

1. Read `PROJECT_CONTEXT.md`, local `AGENTS.md`, and the affected specifications.
2. Create or update a feature specification under `spec/features/`.
3. Implement the smallest behavior covered by the specification.
4. Add or update behavior-focused tests.
5. Run `make check`.
6. Commit the complete logical change with a conventional commit message.
7. Run `make push` from the feature branch to publish it for a pull request.
8. Open a pull request into `main` and wait for GitHub checks and review.
9. A human maintainer merges the pull request; agents never merge into `main`.

## Commands

Run `make help` for the current command list. The important gates are:

```text
make spec-check
make test
make check
make push
```

## Windows Diagnostic Build

The first native validation artifact is a portable CLI executable. It writes
`diagnostic.jsonl`, `diagnostic.log`, and `python.log` under
`%LOCALAPPDATA%\\VoiceInk-Win\\diagnostics\\<run-id>`. Logs contain stage,
size, checksum, timing, and sanitized exception data, but not raw audio or
transcript text.

The local build command must run on Windows:

```text
VOICEINK_FFMPEG_PATH=C:\\path\\to\\ffmpeg.exe \\
VOICEINK_FFMPEG_MANIFEST=C:\\path\\to\\ffmpeg.manifest.json \\
make diagnostic-build
voiceink-diagnostic.exe C:\\path\\to\\audio-or-video-file
```

`ffmpeg.manifest.json` must contain the pinned executable SHA-256 and provenance
metadata. The Make target refuses to build without both external paths and
embeds them into the frozen executable.

The repository workflow `Windows Diagnostic Build` produces a Windows x64
portable bundle with a verified FFmpeg binary. Windows ARM64 can normally run
this x64 diagnostic through Windows x64 emulation. Native ARM64 packaging is a
separate build target.

Direct pushes to `main` are forbidden by local hooks, `make push`, and the repository branch protection policy. `AGENTS.md` contains local agent instructions and is intentionally not published; `PROJECT_CONTEXT.md` is the public project passport for new clones.
