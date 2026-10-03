# Foundation and Runtime Spike

## Status and Scope

Status: draft.

This feature establishes the repository quality gates and validates the proposed Windows ASR runtime before product implementation. It does not provide the user-facing application yet.

## User Scenarios

- A developer can validate that the project structure and specifications are consistent.
- A developer can run the repository checks on macOS or Windows.
- A developer can later record a reproducible Parakeet CPU/CUDA benchmark without changing the application contract.

## Functional Requirements

- The repository must contain the tracked public project context; a local `AGENTS.md` contract may be supplied by the maintainer and is intentionally ignored.
- Product and architecture requirements must live under `spec/` and be catalogued.
- `make spec-check` must fail when a catalogued specification is missing or empty.
- `make check` must run specification validation, formatting, linting, tests, and source compilation.
- The future runtime spike must record CPU, GPU, RAM, VRAM, Windows version, Python version, model artifact, runtime version, and elapsed time.

## Non-Functional Requirements

- The repository must not require Python 3.15 before its dependencies are verified.
- The initial application architecture must support CPU fallback and NVIDIA acceleration without coupling domain code to either backend.
- No model weights, recordings, secrets, or generated binaries may be committed.

## Error and Cancellation Behavior

- A failed quality gate must exit non-zero and identify the failing artifact.
- A missing runtime or model must be reported as an environment/setup failure, not silently treated as a successful benchmark.

## Acceptance Criteria

- `PROJECT_CONTEXT.md`, `README.md`, `Makefile`, `pyproject.toml`, and the catalogued specifications exist.
- `make spec-check` passes.
- `make check` passes on the development machine.
- No existing macOS VoiceInk working-tree files are changed.
- The initial repository state is committed with a conventional commit message.

## Test Plan

- Run `make spec-check`.
- Run `make format-check`.
- Run `make lint`.
- Run `make test`.
- Run `make build`.
- Run the aggregate `make check`.

## Open Questions and Deferred Work

- Add the Parakeet benchmark specification after the Windows runtime spike.
- Add the file-import specification before implementing FFmpeg integration.
- Add the recording, paste, history, modes, and enhancement specifications before their implementations.
