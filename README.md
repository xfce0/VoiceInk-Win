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

## Windows Frontend Shell Artifact

After a clean clone, `make build` is the reproducible Windows packaging command.
It creates or updates only the frontend outputs in `dist/`, installs the exact
`.[gui,build]` extras into `.venv`, validates both x64 PE subsystems, and runs
the console executable with Qt's offscreen platform. It requires 64-bit Windows,
GNU Make, and Python 3.12, 3.13, or 3.14. macOS and Linux are intentionally
rejected for this target; use `make check` there.

Build prerequisites are GNU Make (`make --version` must work) and Git for
Windows with `sh.exe` available on `PATH`; the Makefile uses `/bin/sh`. Git
Bash supplies the required shell, but GNU Make is still required. PowerShell
is supported when both `make.exe` and `sh.exe` are available on `PATH`.
Direct frontend build pins, including the Qt split packages, are committed in
`packaging/windows-build-constraints.txt`.

PowerShell:

```powershell
git clone https://github.com/xfce0/VoiceInk-Win.git
Set-Location .\VoiceInk-Win
python --version  # 3.12.x, 3.13.x, or 3.14.x
make build
```

If `python` is not the desired supported interpreter, use the Python launcher:

```powershell
make build 'PYTHON=py -3.12'
```

Git Bash:

```bash
git clone https://github.com/xfce0/VoiceInk-Win.git
cd VoiceInk-Win
python --version  # 3.12.x, 3.13.x, or 3.14.x
make build
```

The command produces exactly these package files:

```text
dist/
  README.txt
  voiceink-shell.exe          # user-facing GUI executable
  voiceink-shell-smoke.exe    # console/offscreen smoke executable
```

If `dist/` contains unrelated files, the build refuses to overwrite them. Move
those files or explicitly run `make clean` before retrying. `make clean` removes
all generated `dist/` and build directories.

The `Windows Frontend Shell Build` workflow uses the same `make build` command
and publishes the artifact `voiceink-shell-windows-x64`.
GitHub CLI downloads and extracts it into the requested directory:

```powershell
gh run download RUN_ID --repo xfce0/VoiceInk-Win --name voiceink-shell-windows-x64 --dir .\voiceink-shell-windows-x64
& .\voiceink-shell-windows-x64\voiceink-shell.exe
```

Run only `voiceink-shell.exe` as the desktop application. The companion
`voiceink-shell-smoke.exe` is a CI-only smoke-test binary, not
`voiceink-diagnostic.exe` and not a separate user-facing CLI.

Replace `RUN_ID` with the workflow run ID, or download the artifact ZIP from
the Actions page and extract it with `Expand-Archive`. The artifact includes
the same command in its `README.txt`.

To stage a complete relocatable CPU package from already downloaded, pinned
artifacts, run this on Windows after `make build`:

```powershell
make portable-package `
  VOICEINK_FFMPEG_PATH='C:\path\to\ffmpeg.exe' `
  VOICEINK_SIDECAR_PATH='C:\path\to\nemo-speech.exe' `
  VOICEINK_MODEL_PATH='C:\path\to\parakeet.gguf'
```

The result is `release/voiceink-shell-windows-x64`. It contains the shell,
FFmpeg, sidecar, model, package-relative runtime metadata, and a launcher.
The builder verifies every supplied file against
`.github/native-smoke/artifact-lock.template.json`; it does not download
artifacts.

The packaged shell supports two explicit runtime modes:

- **No-resource mode:** when imported-media configuration is absent, the shell
  starts promptly and shows recording and Transcribe as unavailable. It never
  constructs a fake backend or shows synthetic text. The fake backend is
  reserved for focused tests and is never a production fallback.
- **Configured imported-media mode:** when the trusted ASR and FFmpeg settings
  below are complete, the shell starts promptly with Transcribe loading, then
  enables the page only after backend readiness succeeds. Microphone capture,
  WASAPI, and native audio remain unavailable in both modes.

Configured imported-media mode requires these runtime environment variables:

```text
VOICEINK_RUNTIME_MANIFEST=C:\path\to\runtime.manifest.json
VOICEINK_ARTIFACT_LOCK=C:\path\to\artifacts.lock.json
VOICEINK_ARTIFACT_LOCK_SHA256=<sha256-of-artifacts.lock.json>
VOICEINK_FFMPEG_PATH=C:\path\to\ffmpeg.exe
VOICEINK_FFMPEG_VERSION=<approved-version>
VOICEINK_FFMPEG_PROVENANCE_URL=https://<approved-source>
VOICEINK_FFMPEG_SHA256=<sha256-of-ffmpeg.exe>
VOICEINK_FFMPEG_LICENSE=<license-name>
VOICEINK_IMPORT_WORKSPACE_ROOT=C:\path\to\workspace
VOICEINK_IMPORT_ROOTS=C:\path\to\allowed\media;D:\another\allowed\root
```

The runtime manifest and artifact lock must describe the approved model and
sidecar executable. FFmpeg must be an approved absolute path whose metadata and
SHA-256 pass verification. `VOICEINK_IMPORT_ROOTS` is a semicolon-separated
list on Windows. Global hotkeys, system tray, active-application text injection,
and native audio behavior are not part of this imported-media slice.

## Windows Diagnostic Build

The first native validation artifact is a portable CLI executable. It writes
`diagnostic.jsonl`, `diagnostic.log`, and `python.log` under
`%LOCALAPPDATA%\\VoiceInk-Win\\diagnostics\\<run-id>`. Logs contain stage,
size, checksum, timing, and sanitized exception data, but not raw audio or
transcript text.

The local packaging command must run on Windows. The two FFmpeg variables below
are build inputs only; `VOICEINK_FFMPEG_MANIFEST` is copied into the frozen
bundle and is not read from the process environment at runtime:

```text
VOICEINK_FFMPEG_PATH=C:\\path\\to\\ffmpeg.exe \\
VOICEINK_FFMPEG_MANIFEST=C:\\path\\to\\ffmpeg.manifest.json \\
make diagnostic-build
voiceink-diagnostic.exe C:\\path\\to\\audio-or-video-file
```

The production imported-media composition contract is typed
`ImportedMediaConfiguration`, not a mutable manifest path. It requires an
absolute FFmpeg path, absolute non-empty import roots, an absolute workspace
root, and a complete `FfmpegArtifactManifest` containing version, HTTPS
provenance, license, and SHA-256. The environment adapter enables this feature
only when the complete metadata set is present and rejects partial values.

At runtime, `VerifiedFfmpegArtifact` reopens the executable, validates its
identity and approved path, and recomputes its SHA-256 before launch. The
diagnostic CLI accepts an external manifest only with an explicit
`--ffmpeg-sha256`; only the manifest embedded in the frozen bundle may provide
the checksum by itself.

The repository workflow `Windows Diagnostic Build` produces a Windows x64
portable bundle with a verified FFmpeg binary. Windows ARM64 can normally run
this x64 diagnostic through Windows x64 emulation. Native ARM64 packaging is a
separate build target.

Direct pushes to `main` are forbidden by local hooks, `make push`, and the repository branch protection policy. `AGENTS.md` contains local agent instructions and is intentionally not published; `PROJECT_CONTEXT.md` is the public project passport for new clones.
