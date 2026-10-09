# Microphone Gate Decision and Evidence Register

## Status

This register is evidence and decision bookkeeping for
`rfcs/microphone-recording.md` and `spec/features/005-microphone-recording.md`.
It does not approve production implementation. At repository HEAD
`751f88ca130dc10fcc424a352ad169ee8f1b5bdb`, microphone implementation and
enablement remain blocked:

```text
implementation_allowed: false
enablement_allowed: false
G3b: Windows native evidence required after implementation
G4b: Windows helper/package validation required after implementation
```

`APPROVAL_REQUIRED` means repository evidence supports a candidate, but no
human approval is present. `WINDOWS_REQUIRED` means this macOS workspace
cannot produce the required evidence. `OPEN` means neither a decision nor
enough repository evidence exists. No value in this document is an approval,
and no SHA-256 value is estimated.

## Evidence Baseline

The baseline is the immutable repository commit `751f88ca130dc10fcc424a352ad169ee8f1b5bdb`.
The earlier documentation branches reviewed were:

| Ref | Use in this register |
|---|---|
| `docs/microphone-gate-evidence` at `7d644f19ec8c880e4ce244888daad1b7f24ffb29` | Earlier gate-evidence branch; it contains no microphone evidence artifact. |
| `docs/rfc-microphone-recording` at `1ee23ed20e571a37729822cc414814366748ce32` | Earlier RFC/catalog synchronization. |
| `docs/rfc-microphone-capture` at `c245aae30a333985f38cc3c98ca8aef8a6e14eb5` | Earlier capture-slice specification; it does not provide Windows-native evidence. |

The macOS VoiceInk reference is context only, not Windows evidence:
`https://github.com/xfce0/VoiceInk` at tree `2cc2089f7327a605dfce9819d62ab4ca1575f517`.
The reviewed reference paths include `VoiceInk/Infrastructure/Audio/CoreAudioRecorder.swift`,
`VoiceInk/Features/Recording/Capture/Recorder.swift`,
`VoiceInk/Infrastructure/Audio/Devices/AudioDeviceManager.swift`, and
`VoiceInk/Features/AudioSettings/Views/AudioSetupView.swift`. They support
product vocabulary such as explicit device selection and asynchronous setup;
CoreAudio implementation details cannot approve WASAPI decisions.

The existing `.github/workflows/native-smoke.yml` is an imported-media/runtime
smoke lane. Its `windows-latest` run, pinned FFmpeg/NeMo artifacts, and
`native-smoke-report.json` do not prove microphone, WASAPI, ACL, or helper
evidence. A macOS run, fake adapter, or direct CLI transcription is not a
microphone gate result.

## Decision Register

The hashes below are SHA-256 hashes of immutable files at the baseline commit,
unless the path is explicitly prefixed with `HEAD:`. `NONE` is intentional.

| ID | Evidence-backed candidate or current decision | Rejected alternatives | Owner | Approver | Evidence path | Evidence SHA-256 | Approval record | Decision date | RFC/spec/catalog commit | Status | Gate |
|---|---|---|---|---|---|---|---|---|---|---|---|
| D1 | Reuse the existing 32 min / 30,720,000-sample and 64 MiB canonical limits as the microphone candidate; the relationship still needs product approval. | Not evaluated. | Product owner | Product owner + maintainer | `rfcs/imported-media-transcription.md`; `src/voiceink_win/domain/models.py` | `9389b32b4644cf670512c57d1b56cba4a66b9bdb730591648f4db2459320dea9`; `8770188aff26443c59981e010343ea9227acab8d937bd19f4a73cbaa6e32dc70` | PENDING: `docs/microphone-gate-evidence.md#d1` | PENDING | PENDING | APPROVAL_REQUIRED | G1 |
| D2 | Keep the normative operation state `cancelling`; the current shell has no cancelling projection, so the shell projection remains an approval choice. | Not evaluated. | Application owner | Maintainer + UI owner | `src/voiceink_win/domain/shell.py`; `src/voiceink_win/application/shell_controller.py` | `258ddd1b75f2a74a973b52a1636eba28d6dc52f47414a26bfef8bf91dec583f2`; `f12c5771b6e5ec3e4672f86b2e8265383ea8c7ff4fef2a14cbfebf187ca9b7d8` | PENDING: `docs/microphone-gate-evidence.md#d2` | PENDING | PENDING | APPROVAL_REQUIRED | G1 |
| D3 | Use a separate capture-error family at the microphone boundary; reuse existing `AsrError` for shared ASR/runtime failures. This is a candidate, not a new production type. | Not evaluated. | Application owner | Maintainer | `src/voiceink_win/domain/errors.py`; `src/voiceink_win/domain/imported_errors.py` | `50968080bf40c5e42bf666a4dc50ce8ed494979e5be1b500bb561f2b8049c75d`; `a0ff5bf6a2a0ba84d5d5ae801590005196d5cc3c836d63105a4b94d9d8c778c8` | PENDING: `docs/microphone-gate-evidence.md#d3` | PENDING | PENDING | APPROVAL_REQUIRED | G1 |
| D4 | Every operation must carry an absolute monotonic deadline; exact recording, finalization, ASR, and cleanup values are not present in repository evidence. | None. | Application owner | Product owner + maintainer | `rfcs/imported-media-transcription.md`; `rfcs/microphone-recording.md` at baseline | `9389b32b4644cf670512c57d1b56cba4a66b9bdb730591648f4db2459320dea9`; `64d3e8ae0bcdc08f74e770b23c42cd0b9f827e65626ca075be8d5f097aead5ce` | PENDING: `docs/microphone-gate-evidence.md#d4` | PENDING | PENDING | OPEN | G1/G2 |
| D5 | Extend the existing lock/condition FIFO reservation protocol: one transaction protects capacity and `held -> committed -> released` ownership. The existing shared ASR service still needs request-scoped admission/quiescence work. | Actor and unbounded blocking queue not evaluated. | ASR owner | Maintainer | `src/voiceink_win/application/import_queue.py`; `src/voiceink_win/domain/imported_job.py` | `17ead2b8b57b6ebb5a236d62ee6be2e3033964aa1cd41449b0aacc51a71b09bf`; `55ad2284a86280d25a0942e17a95e70a1a4afb08ae7adf290ab46d3168b7fc8b` | PENDING: `docs/microphone-gate-evidence.md#d5` | PENDING | PENDING | APPROVAL_REQUIRED | G2 |
| D6 | Preserve `recovery_owned`/recovery-pending semantics: no new generation or release until ownership release is proven. The helper-specific recovery budget remains open. | No fallback or silent release. | ASR owner | Maintainer + security owner | `src/voiceink_win/application/import_service.py`; `rfcs/imported-media-transcription.md` | `2af166385acf6c52f4b20fb266b218f447929dcee5c94dc697b00c566e7dfe39`; `9389b32b4644cf670512c57d1b56cba4a66b9bdb730591648f4db2459320dea9` | PENDING: `docs/microphone-gate-evidence.md#d6` | PENDING | PENDING | OPEN | G2 |
| D7 | NONE. The default input role requires a Windows endpoint-role probe and product approval. | Console, communications, product-defined: not rejected. | Windows owner | Product owner + maintainer | `docs/evidence/microphone/<run-id>/g3a/endpoint-role.json` | NONE | PENDING: `docs/microphone-gate-evidence.md#d7` | PENDING | PENDING | WINDOWS_REQUIRED | G3a |
| D8 | NONE. Shared mode is the candidate under the contract below, not an approved value. | Exclusive mode not rejected. | Windows owner | Product owner + maintainer | `docs/evidence/microphone/<run-id>/g3a/wasapi-mode.json` | NONE | PENDING: `docs/microphone-gate-evidence.md#d8` | PENDING | PENDING | WINDOWS_REQUIRED | G3a |
| D9 | NONE. No Windows capability inventory exists for the claimed input tuples. | No format tuple rejected. | Windows owner | Maintainer | `docs/evidence/microphone/<run-id>/g3a/format-inventory.json` | NONE | PENDING: `docs/microphone-gate-evidence.md#d9` | PENDING | PENDING | WINDOWS_REQUIRED | G3a |
| D10 | NONE. The canonical PCM contract is fixed, but coefficients, resampler, rounding, output length, and tolerance are not selected. | No converter alternative rejected. | Audio owner | Maintainer + Windows owner | `docs/evidence/microphone/<run-id>/g3a/golden-vectors.json` | NONE | PENDING: `docs/microphone-gate-evidence.md#d10` | PENDING | PENDING | WINDOWS_REQUIRED | G3a |
| D11 | NONE. Period, queue capacity, callback/event model, and overflow action require Windows stress evidence. | No buffer alternative rejected. | Windows owner | Maintainer | `docs/evidence/microphone/<run-id>/g3a/buffer-stress.json` | NONE | PENDING: `docs/microphone-gate-evidence.md#d11` | PENDING | PENDING | WINDOWS_REQUIRED | G3a |
| D12 | Candidate amendment: imported media uses FFmpeg; live device frames are converted inside the platform adapter and never cross the Domain/Application boundary. | Current architecture wording is not rejected until amendment approval. | Architecture owner | Maintainer | `spec/architecture/overview.md`; `HEAD:rfcs/microphone-recording.md` | `3bb15b0064a01af77b4f7e0f3520941e65d7852c90c27cda15d597a2af1e9fc2`; `64d3e8ae0bcdc08f74e770b23c42cd0b9f827e65626ca075be8d5f097aead5ce` | PENDING: `docs/microphone-gate-evidence.md#d12` | PENDING | PENDING | APPROVAL_REQUIRED | G3a |
| D13 | NONE. C++20/MSVC is the helper candidate defined below; no helper exists in this repository. | Named Python binding, in-process COM, and other helper technologies not rejected. | Windows owner | Maintainer | `docs/evidence/microphone/<run-id>/g4a/helper-feasibility.json` | NONE | PENDING: `docs/microphone-gate-evidence.md#d13` | PENDING | PENDING | WINDOWS_REQUIRED | G4a |
| D14 | NONE. A versioned named pipe with current-user ACL is the candidate defined below; framing, authentication, and bounds require Windows evidence. | Loopback socket, shared memory, and no-go not rejected. | Security owner | Maintainer + Windows owner | `docs/evidence/microphone/<run-id>/g4a/ipc-feasibility.json`; `.../named-pipe-acl.json` | NONE | PENDING | PENDING | PENDING | WINDOWS_REQUIRED | G4a |
| D15 | NONE. Existing runtime artifact locks prove only the imported-media lane; helper signing, provenance, x64/ARM64 package paths, and rollback remain open. | No package lane rejected. | Release owner | Maintainer + security owner | `.github/workflows/native-smoke.yml`; `.github/native-smoke/artifact-lock.template.json` | `e56fc7e2f005eb5ac6a372b11c761da5d5bf9857ec532f0005462b974231a0cd`; `38835b09e7b35173d2b6f05e0713dcfb7b343493d5a8812d497669ed8507fc0e` | PENDING: `docs/microphone-gate-evidence.md#d15` | PENDING | PENDING | WINDOWS_REQUIRED | G4a |
| D16 | NONE. Existing Job Object code is evidence of a repository pattern only; microphone helper limits, restart, and recovery are unproved. | No restart policy rejected. | Windows owner | Maintainer + security owner | `src/voiceink_win/infrastructure/process.py`; `rfcs/imported-media-transcription.md` | `4e10f55953a845f3caed03cc67876cb3e03e980b4a970c7e62eeb8e29ce4b871`; `9389b32b4644cf670512c57d1b56cba4a66b9bdb730591648f4db2459320dea9` | PENDING: `docs/microphone-gate-evidence.md#d16` | PENDING | PENDING | WINDOWS_REQUIRED | G4a |
| D17 | NONE. The 64 MiB canonical limit and existing bounded diagnostic limits are evidence; the complete simultaneous audio-path/RSS accounting is not. | No memory budget rejected. | Performance owner | Maintainer | `src/voiceink_win/domain/models.py`; `rfcs/microphone-recording.md` at baseline; `rfcs/native-windows-runtime-startup.md` | `8770188aff26443c59981e010343ea9227acab8d937bd19f4a73cbaa6e32dc70`; `64d3e8ae0bcdc08f74e770b23c42cd0b9f827e65626ca075be8d5f097aead5ce`; `2fc51b6683e716adff85e29c7f0016c5599fa8ac6087e6d35fadba54cb1770c2` | PENDING: `docs/microphone-gate-evidence.md#d17` | PENDING | PENDING | APPROVAL_REQUIRED | G3a/G4a |
| D18 | NONE. The existing workflow defines only an x64 imported-media `windows-latest` lane; microphone owner, runner matrix, fixtures, thresholds, and retention are absent. | No microphone lane rejected. | Release owner | Maintainer | `.github/workflows/native-smoke.yml`; `.github/native-smoke/artifact-lock.template.json` | `e56fc7e2f005eb5ac6a372b11c761da5d5bf9857ec532f0005462b974231a0cd`; `38835b09e7b35173d2b6f05e0713dcfb7b343493d5a8812d497669ed8507fc0e` | PENDING: `docs/microphone-gate-evidence.md#d18` | PENDING | PENDING | WINDOWS_REQUIRED | G4a |
| D19 | NONE. Existing report sanitization is reusable evidence; dump mode, locations, retention/consent, scanner, canaries, and negative controls are not approved. | Dumps disabled versus filtered not rejected. | Security/privacy owner | Maintainer + security owner | `src/voiceink_win/infrastructure/reporting.py`; `rfcs/native-windows-runtime-startup.md` | `b5fa0246529064e4191b87e945de26515a51e35bbd2425fb14a523574d90d2f4`; `2fc51b6683e716adff85e29c7f0016c5599fa8ac6087e6d35fadba54cb1770c2` | PENDING: `docs/microphone-gate-evidence.md#d19` | PENDING | PENDING | APPROVAL_REQUIRED | G4a |

The hashes above are evidence references, not approval claims. They must be
regenerated by the verification command below before any approval record is
signed. A value must never be copied from an earlier run without checking the
referenced commit.

## Gate Register

| Gate | Current result | What repository evidence resolves | Blocking remainder |
|---|---|---|---|
| G1 | `EVIDENCE_PARTIAL / APPROVAL_REQUIRED` | Normative state machine, ownership rules, generation fence, canonical value shape, and existing shell limitation are recorded. | D1-D4 approval; race evidence for the future microphone implementation. |
| G2 | `EVIDENCE_PARTIAL / APPROVAL_REQUIRED` | Existing FIFO reservation and lock-linearized job transitions provide a reusable pattern. | D5-D6 approval and a request-scoped ASR quiescence contract; current `wait_idle`/`transcribe` is not sufficient. |
| G3a | `BLOCKED / WINDOWS_REQUIRED` | Canonical output shape and imported-media conversion boundary are documented. | D7-D12 exact policy, Windows format/contention/golden-vector/buffer evidence, and D12 architecture approval. |
| G4a | `CANDIDATE_ONLY / WINDOWS_REQUIRED` | Process isolation and safe diagnostic/Job Object patterns exist for other native work. | C++20/MSVC helper feasibility, named-pipe ACL, Job Object policy, package matrix, privacy dump policy, and D13-D19 approval. |

No gate has outcome `GO`. The catalog must continue to say
`implementation_allowed: false`, `enablement_allowed: false`, and
`g4a_outcome: OPEN (GO required)`.

## Exact Evidence Commands and Paths

### Commands that are runnable at this HEAD

Run from repository root:

```text
python scripts/spec_check.py
make spec-check
python -m pytest tests/test_asr_application.py tests/test_asr_domain.py tests/test_imported_media.py tests/test_process_supervisor.py tests/test_reporting.py tests/test_shell.py
```

These commands produce repository/design evidence only. They do not close a
microphone gate. The existing native smoke command is:

```text
make native-smoke
```

Its declared artifacts are `native-smoke-report.json` and
`native-smoke-events.jsonl`; the workflow uploads them as the
`native-smoke-report` artifact. They remain imported-media/runtime evidence.

### Required Windows evidence command

The implementation slice must provide this stable command interface; the
command is deliberately not present at this documentation-only HEAD:

```powershell
$ErrorActionPreference = "Stop"
$run = $env:GITHUB_RUN_ID
$root = Join-Path $env:GITHUB_WORKSPACE "docs/evidence/microphone/$run"
New-Item -ItemType Directory -Force -Path $root | Out-Null
git rev-parse HEAD | Set-Content (Join-Path $root "repository-head.txt")
Get-ComputerInfo -Property WindowsProductName,WindowsVersion,OsBuildNumber,OsArchitecture |
  ConvertTo-Json -Depth 3 | Set-Content (Join-Path $root "runner-metadata.json")
python -m pytest tests/windows/test_microphone_native.py -m microphone_native --evidence-dir $root
python scripts/microphone_native_smoke.py --evidence-dir $root
```

The command must write only sanitized metadata and measurements to:

```text
docs/evidence/microphone/<run-id>/runner-metadata.json
docs/evidence/microphone/<run-id>/repository-head.txt
docs/evidence/microphone/<run-id>/g1/state-race.json
docs/evidence/microphone/<run-id>/g2/scheduler-quiescence.json
docs/evidence/microphone/<run-id>/g3a/endpoint-role.json
docs/evidence/microphone/<run-id>/g3a/wasapi-mode.json
docs/evidence/microphone/<run-id>/g3a/format-inventory.json
docs/evidence/microphone/<run-id>/g3a/golden-vectors.json
docs/evidence/microphone/<run-id>/g3a/buffer-stress.json
docs/evidence/microphone/<run-id>/g4a/helper-feasibility.json
docs/evidence/microphone/<run-id>/g4a/named-pipe-acl.json
docs/evidence/microphone/<run-id>/g4a/job-object.json
docs/evidence/microphone/<run-id>/g4a/package-matrix.json
docs/evidence/microphone/<run-id>/g4a/privacy-scan.json
docs/evidence/microphone/<run-id>/g4a/deadline-trace.json
docs/evidence/microphone/<run-id>/gate-report.json
docs/evidence/microphone/<run-id>/SHA256SUMS.txt

The runner must upload the directory as `microphone-gate-<run-id>`. The
artifact may contain event names, stable codes, counts, durations, byte
lengths, peak RSS, handle/thread counts, and hashes. It must not contain PCM,
transcript text, endpoint names or IDs, full paths, secrets, raw exception
messages, or unfiltered dumps.

### Immutable-file hash command

Before an approver signs a record, regenerate hashes from the exact reviewed
commit and compare them with the register:

```powershell
python -c "import hashlib, subprocess, sys; print(hashlib.sha256(subprocess.check_output(['git', 'show', f'{sys.argv[1]}:{sys.argv[2]}'])).hexdigest())" <reviewed-commit> <repository-path>
Get-ChildItem -Recurse docs/evidence/microphone/<run-id> -File |
  Get-FileHash -Algorithm SHA256 |
  Sort-Object Path | ConvertTo-Csv -NoTypeInformation |
  Set-Content docs/evidence/microphone/<run-id>/SHA256SUMS.txt
```

The approver must record the reviewed commit, evidence root, exact SHA-256
manifest, and review URL. A missing artifact, changed hash, or unpopulated
approver field leaves the decision open.

## Candidate Implementation Acceptance Contract

The following criteria are implementation-ready requirements for the candidate
design. They do not select D7-D19 or authorize code. `T_open`, `T_start`,
`T_stop_finalize`, `T_asr`, and `T_cleanup` are exact absolute-monotonic
deadlines that D4 must approve before implementation.

### C++20/MSVC WASAPI helper

- **MIC-AC-001**: The helper builds with MSVC and C++20 in Release for every
  claimed architecture using the documented toolchain, with warnings treated
  as errors; the evidence records compiler, Windows SDK, architecture, and
  helper SHA-256.
- **MIC-AC-002**: The helper initializes COM on each owning thread, opens only
  the D7-selected input role in D8-selected shared mode, uses an event-driven
  `IAudioClient`/`IAudioCaptureClient` loop, and keeps all COM/native types
  inside the helper process.
- **MIC-AC-003**: Device loss, access denial, unsupported format, AUDCLNT
  failure, capture overrun, and deadline expiry map to stable codes; no device
  fallback or retargeting occurs.
- **MIC-AC-004**: The helper never blocks the audio callback on a pipe write,
  allocator, logger, or Python call; stop first prevents new frame ownership,
  then drains only bounded helper-owned data before finalization.

### Named-pipe ACL and protocol

- **MIC-AC-005**: The pipe name is unguessable and generation-scoped; the
  security descriptor grants access only to the launching user's SID and the
  helper process identity. A competing user, forged client, replayed hello,
  wrong generation, or wrong protocol version is rejected before PCM acceptance.
- **MIC-AC-006**: The protocol has versioned hello/auth, open, start, bounded
  audio chunk, stop, cancel, status, terminal result, and close messages. Every
  message carries generation/session correlation and an absolute deadline.
- **MIC-AC-007**: Length prefixes and all limits are validated before payload
  allocation. Oversized, malformed, out-of-order, or late messages produce a
  stable bounded error and cannot grow the pipe reader or queue.

### Job Object and recovery

- **MIC-AC-008**: The parent creates a Windows Job Object, applies
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and the approved limits, assigns the
  suspended helper before resume, and records the order in `job-object.json`.
- **MIC-AC-009**: Cancellation timeout, protocol failure, crash, and shutdown
  close the Job Object and reap the helper within `T_cleanup`; repeated cleanup
  emits at most one termination request and one reap/close sequence.
- **MIC-AC-010**: If release cannot be proven by `T_cleanup`, the operation is
  `recovery_owned`, no PCM reference is released, no terminal result is
  published, and no new generation is admitted until `resource_release_proven`.

### Bounded buffers and canonical PCM

- **MIC-AC-011**: The capture ring, pipe chunk, pipe message, conversion
  scratch, canonical payload, ASR framing copy, and diagnostics each have an
  approved byte ceiling. The report includes simultaneous occupancy and peak
  process-tree RSS; allocation crossing a ceiling is rejected before growth.
- **MIC-AC-012**: Any overrun or full queue fails the recording. The helper
  never drops, pads, duplicates, truncates, or silently replaces frames while
  claiming a complete recording; no disk spill is used.
- **MIC-AC-013**: The only successful audio value is non-empty immutable mono
  16,000 Hz signed PCM16 little-endian with an even byte length and contiguous
  samples. Malformed, non-finite, discontinuous, unsupported, or zero-frame
  input maps to the approved typed result without ASR invocation.
- **MIC-AC-014**: Downmix, resampling, rounding, clamping to `[-32768, 32767]`,
  and output length follow the approved D10 policy. Repeating the same golden
  vector twice produces identical canonical bytes and identical output hash.

### Privacy diagnostics and deadlines

- **MIC-AC-015**: Diagnostics use an allowlist of stable codes, enum values,
  bounded timings/counts, byte lengths, versions, and an ephemeral operation
  token. They exclude PCM, transcript, endpoint identity, full paths, secrets,
  raw exception text, and raw dumps. The D19 scanner passes canary and
  negative-control tests and records its version.
- **MIC-AC-016**: Every helper, pipe, capture, stop, conversion, ASR, cancel,
  quiescence, cleanup, and terminal-publication operation receives an absolute
  monotonic deadline. The trace proves `stop_requested` precedes
  `audio_callback_quiesced`, `finalize_begin/end` precedes ASR admission, and
  terminal publication occurs only after cleanup.
- **MIC-AC-017**: A deadline miss never becomes success or empty success. It
  produces the approved timeout/recovery code, invalidates the generation, and
  follows the `recovery_owned` rule. Cancellation is terminal only after the
  capture/ASR fences and cleanup satisfy their approved deadlines.

## Approver Record Template

Each approval must be a separate immutable or versioned record at
`docs/evidence/microphone/approvals/<decision-id>-<version>.md` and must
contain all fields below. Empty fields are not approval:

```text
decision_id:
version:
reviewed_rfc_spec_catalog_commit:
selected_alternative:
rejected_alternatives:
evidence_root:
evidence_sha256_manifest:
repository_file_sha256_values:
approver_name:
approver_role:
approver_handle_or_review_url:
decision_date_utc:
decision: APPROVED | REJECTED
conditions_or_follow_up:
```

Until all D1-D19 records contain these fields, G1/G2/G3a/G4a cannot become
approved. G3b and G4b remain separate Windows enablement gates even after the
pre-implementation decisions are approved.
