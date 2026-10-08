# RFC: Native Windows Runtime Startup

## Status

Draft. This RFC defines the investigation and startup design for the native
Windows NeMo-Speech.cpp runtime. It does not implement production code and does
not change the completion status of the Parakeet runtime integration.

## Problem and Evidence

The latest native smoke report at
`/tmp/opencode/native-smoke-37762930921/VoiceInk-Win/VoiceInk-Win/native-smoke-report.json`
is failed:

```json
{
  "status": "failed",
  "failure_stage": "application_start",
  "failure": {
    "error_code": "native_smoke_failure"
  },
  "cause_types": [
    "ExceptionGroup",
    "ExecutionError",
    "RuntimeUnavailableError",
    "ExecutionError",
    "PermissionError"
  ],
  "application_close_error": "ExecutionError"
}
```

The report proves neither sidecar readiness nor transcription. No native
success claim is permitted from this evidence.

The current startup path is:

```text
native_smoke.py
  -> BackendApplication.start()
  -> LoopbackProxy.start()
  -> NeMoSidecarRuntime.start()
  -> SubprocessSupervisor.start()
  -> suspended process + Windows Job Object
  -> resume
  -> /ready probe
  -> authenticated model attestation
```

The current code deliberately uses a long-lived loopback sidecar, a safe argv,
trusted artifact hashes, Windows artifact read leases, a suspended launch, a
Job Object, and bounded cleanup. The failure is therefore at the native startup
boundary, not evidence that the ASR protocol or transcription result is valid.

Relevant repository evidence:

- `scripts/native_smoke.py` records `runtime.verified` and `model.verified`
  before application startup, then requires `sidecar.ready`, non-empty cold and
  warm transcripts, and `runtime.cleaned`.
- `SubprocessSupervisor.start()` opens executable/model handles, verifies
  identity, creates the suspended process, assigns the Job Object, revalidates
  artifacts, and resumes the process.
- `NeMoSidecarRuntime.start()` currently converts unexpected startup failures to
  typed errors and runs rollback. A rollback failure can produce an
  `ExceptionGroup`, obscuring which startup operation failed.
- The current rollback path attempts the supervisor kill operation even after a
  terminate operation has already stopped the process. This is a possible
  source of a secondary Windows permission/handle error.
- `notes.md` records a successful manual direct CLI transcription on Windows,
  but that is backend evidence only. It is not evidence that the application
  composition root reaches `sidecar.ready` or produces a non-empty transcript.
- Existing tests cover artifact hashes, safe argv, readiness validation,
  protocol decoding, cancellation, restart, cleanup, and report redaction.
  They do not reproduce the reported native `PermissionError` through the real
  Windows process launch path.

The configured repository quality gate is `make check`. The current worktree
does not have the configured `.venv`, so local test execution is not treated as
a passing baseline. Python-level tests and native Windows smoke are separate
evidence classes.

## Goals

- Identify the exact native startup operation that raises `PermissionError`.
- Preserve the primary startup failure when cleanup also fails.
- Make startup phases and cleanup ownership observable through sanitized,
  stable diagnostics.
- Keep the existing Clean Architecture boundary and long-lived sidecar design.
- Ensure a failed startup cannot publish readiness or transcription success.
- Ensure every process, Job Object, artifact lease, transport, and proxy is
  either owned by a completed lifecycle or explicitly reported as pending
  recovery.
- Add deterministic cross-platform regression tests and focused Windows probes.
- Establish an authoritative native verification sequence for CPU startup.

## Non-Goals

- Implementing the fix in this RFC.
- Replacing NeMo-Speech.cpp with an embedded Python/C++ runtime.
- Adding a fake runtime fallback to mask a native startup failure.
- Adding CUDA validation or making CUDA claims.
- Changing the ASR domain contract, audio format, protocol, or imported-media
  queue.
- Adding UI, microphone capture, installer behavior, automatic downloads, or
  model updates.
- Relaxing artifact verification or process-tree cleanup without a falsifiable
  probe and an RFC amendment.
- Claiming native success before both `sidecar.ready` and non-empty
  transcription are observed.

## Ranked Hypotheses

### H1: Artifact sharing or launch-handle incompatibility

The supervisor holds Windows handles for the executable and model while the
process is created and starts. The selected share modes may conflict with
`CreateProcess`, image mapping, the native runtime's model open, or Windows
security policy. This is the leading hypothesis because the safe failure chain
contains `PermissionError`, while cross-platform artifact tests cannot exercise
these Windows sharing semantics.

### H2: Rollback masks the primary startup failure

The original launch/readiness failure may be followed by cleanup that performs
an invalid second operation, such as killing an already stopped process or
closing a resource whose ownership was never acquired. The resulting
`ExceptionGroup` can make the report look like a generic execution failure.
The report's nested `ExceptionGroup`, `ExecutionError`, and `PermissionError`
sequence is consistent with this hypothesis.

### H3: The native process is created but exits before readiness

The runtime may start and then fail to load a DLL, model, CPU backend, or
required runtime setting. Current stdout/stderr suppression prevents the
startup boundary from distinguishing process creation failure from early child
exit. The direct CLI success note lowers, but does not eliminate, this
hypothesis because the application supplies a different argv, endpoint,
environment, and lifecycle.

### H4: Loopback proxy or endpoint startup conflict

The proxy or ephemeral port allocation may fail, or the sidecar may bind a
different endpoint than the readiness probe expects. The current
select-a-port-then-close-the-socket sequence does not reserve ownership of the
port; a competing local process can win the bind race. Authenticated readiness
should prevent that process from receiving audio, but it can still cause a
startup denial of service. This is lower-ranked because the report reaches
`application_start` and the reported cause is a permission error rather than a
typed endpoint configuration error, but it must be falsified explicitly.

### H5: Artifact, architecture, or environment mismatch

The externally provisioned executable, model, architecture, runtime DLL set,
or inherited environment may differ from the manual CLI environment. Hashes
and lock checks reduce this probability, but they do not prove loader
compatibility, process architecture, or executable policy acceptance.

## Falsifiable Probes

All probes must run with the same Windows account, runner image, Python
version, artifact lock, runtime executable, model, and fixture as native smoke.
Probe output must contain operation names, stable error codes, exception types,
and Windows error numbers where available; it must not contain paths, command
line secrets, model data, audio, or transcript text.

| ID | Probe | Falsifies / confirms | Required observation |
|---|---|---|---|
| P1 | Add a diagnostic-only startup phase ledger around proxy start, artifact open/hash, `Popen`, Job Object creation/assignment, resume, readiness, and attestation. | H1-H5 | One phase and operation owns the first failure; cleanup failures are listed separately. |
| P2 | Run a minimal `CreateProcess` probe for the pinned executable with the same working directory, environment allowlist, and safe endpoint arguments, first normally and then suspended. | H3-H5 | Distinguishes Windows process creation/ACL failure from runtime early exit. |
| P3 | Run the pinned sidecar directly with bounded stderr capture and the same model, backend, endpoint, and API-key environment. | H3/H5 | Captures sanitized exit code and loader/runtime error class without using application lifecycle code. |
| P4 | Compare a diagnostic-only launch with artifact leases held and with leases released after hash/identity verification. Never use the released variant as production behavior. | H1 | A difference confirms sharing/lease interaction; no difference rejects the leading hypothesis. |
| P5 | In the suspended-launch probe, record the order `artifact revalidate -> CreateProcess -> Job assign -> resume` and revalidate the process image/model identity before resume. | H1/H5 | Detects ordering or handle invalidation errors without weakening the security gate. |
| P6 | Probe the proxy and sidecar port independently, including an occupied-port case, a competing loopback listener, and the allocation-to-bind race. | H4 | Shows whether startup fails before process ownership or during readiness; a wrong listener must receive no audio. |
| P7 | Execute each failed-start rollback path twice: process never created, process created but stopped, process running, and cleanup pending. | H2 | Rollback is idempotent, does not kill a stopped/non-owned process, and retains the primary error. |
| P8 | Run the full smoke with the startup ledger enabled and inspect event order. | All | No `sidecar.ready` or `asr.completed` event exists unless their gates actually pass. |

The first implementation step must be P1 and P7. Changing file sharing,
environment inheritance, or retry policy before those probes would conflate
root cause and remediation.

## Chosen Design

Retain the existing sidecar architecture and introduce a transactional,
phase-aware startup boundary in infrastructure. The design has four parts.

### 1. Explicit startup transaction

`SubprocessSupervisor` owns a startup transaction with monotonic phases:

```text
created
  -> endpoint_validated
  -> artifacts_verified
  -> artifact_leases_acquired
  -> process_created_suspended
  -> job_assigned
  -> artifacts_revalidated
  -> process_resumed
  -> ready
  -> attested
```

Every phase records whether its resource was actually acquired. Rollback walks
only the acquired resources in reverse order. A process is terminated only if
it was created; a kill is attempted only if the process is still running or a
known process-tree recovery owner remains. A stopped process is reaped and is
not killed again.

### 2. Primary error plus cleanup outcome

Startup returns one primary typed error. Cleanup is a separate typed outcome
with one of `complete`, `pending`, or `failed`. If cleanup fails, the report
retains the primary operation and error code and adds sanitized cleanup
failures; cleanup must not replace the root cause with an opaque
`ExceptionGroup`. An `ExceptionGroup` may remain an internal implementation
detail, but report serialization must expose stable fields rather than
exception messages.

The minimum diagnostic record is:

```text
startup_phase
operation
error_code
error_type
win32_error_code (optional)
cleanup_status
resource_ownership
```

No field may contain an absolute path, raw exception text, command line,
authorization value, model bytes, audio, or transcript text.

### 3. Security-preserving launch order

The production launch order remains:

1. Validate the loopback endpoint and explicit backend.
2. Load the trusted, read-only artifact lock and runtime manifest.
3. Verify executable/model path, regular-file status, identity, and SHA-256.
4. Acquire the Windows artifact lease using the approved path.
5. Create the sidecar suspended with `shell=False` and the allowlisted
   environment.
6. Assign the process to the kill-on-close Job Object.
7. Revalidate the locked artifact identities and hashes.
8. Resume the process.
9. Require `/ready` with `ready=true`, transcription capability, and the
   configured device.
10. Complete authenticated model attestation before publishing readiness.

The RFC does not approve releasing artifact leases or broadening sharing flags
as a workaround. If P4 confirms a sharing conflict, the remediation must be a
separate amendment that preserves path, identity, hash, and process-binding
guarantees. The default candidate is a private, verified staging root with
restrictive ACLs, not an unverified path-based fallback.

The internal sidecar endpoint is not considered owned merely because an unused
port number was observed. The implementation must either reserve the endpoint
through a supported listener handoff or treat any bind/readiness collision as a
fail-closed startup error. A competing listener must not receive PCM data. If
the sidecar cannot accept a reserved-listener handoff, a new endpoint may be
selected only before any request is sent, followed by the same authenticated
readiness and model-attestation checks.

### 4. Evidence-gated publication

The native smoke event contract remains:

```text
runtime.verified
model.verified
sidecar.ready
asr.completed (transcript_length > 0)
runtime.cleaned
```

`sidecar.ready` is emitted only after the complete application start returns.
`asr.completed` is emitted only after a real request returns a non-empty
transcript. A failed startup emits neither event. `runtime.cleaned` is emitted
only after the sidecar, process reaper, Job Object, artifact leases, proxy, and
transport have completed cleanup. The event writer is then closed and its close
result is included in final cleanup validation; otherwise the report is a
cleanup failure or pending recovery.

The application retains bounded retry policy: access denied, invalid
configuration, artifact mismatch, missing model, and protocol failures are not
silently retried. A process crash or transient readiness/transport timeout may
use the existing bounded recovery path only after the previous process and all
owned handles are fully fenced and cleaned up. No retry changes CPU/CUDA
backend implicitly.

## Security Constraints

- Process the runtime locally; only the owned loopback endpoint is allowed.
- Use an argument array, `shell=False`, `stdin=DEVNULL`, and an allowlisted
  environment. Do not inherit tokens or unrelated secrets.
- Keep the per-start API key in the allowlisted environment and use
  `Authorization: Bearer` for `/v1` requests. The nonce is correlation data,
  not server-side authentication for the official runtime.
- Require trusted artifact-lock hashes, HTTPS provenance, licenses, and exact
  approved paths. Reject UNC/device paths, symlinks, reparse points, aliases,
  and case-insensitive path mismatches.
- Verify artifacts through handles and revalidate immediately before resume.
- Do not treat a port obtained from a closed probe socket as an ownership
  guarantee. Bind collisions and unauthenticated listeners fail closed before
  any audio request is sent.
- Assign the Windows Job Object before resume and terminate the complete owned
  process tree on crash, timeout, failed readiness, or shutdown.
- Never treat an exception, empty response, or missing readiness as a
  successful transcript.
- Bound readiness and response reads, startup/cleanup deadlines, and all
  captured diagnostics.
- Redact paths, secrets, raw audio, model data, transcript text, and raw
  exception messages from reports and ordinary logs.
- Do not use a fake adapter, direct CLI result, or manual model output as proof
  of application-level native success.

The startup bookkeeping is `O(1)` per lifecycle phase and uses `O(p)` memory
for `p` owned resources. Artifact hashing is `O(n)` in artifact bytes with
`O(1)` working memory beyond the bounded I/O buffer. Native model loading and
inference remain runtime-specific; they are measured, not estimated here.

## Regression Tests

Preserve the existing tests in `test_sidecar_runtime.py`,
`test_process_supervisor.py`, `test_native_smoke.py`, `test_runtime_manifest.py`,
`test_transport.py`, and `test_reporting.py`. Add behavior-focused tests for:

- startup failure before process creation retains the original typed error and
  performs no kill;
- startup failure after process creation terminates exactly the owned process;
- rollback does not kill an already stopped process;
- cleanup failure is reported separately from the primary startup operation;
- repeated cleanup is idempotent and prevents restart while recovery is
  pending;
- each startup phase emits only sanitized operation/error metadata;
- `CreateProcess`, Job Object assignment, resume, readiness, and attestation
  failures map to distinct stable diagnostic phases;
- artifact revalidation occurs before resume and still rejects changed files;
- no `sidecar.ready` or `asr.completed` event is emitted on any startup or
  cleanup failure;
- a successful fake/in-process sidecar still produces a non-empty transcript
  through the real adapter boundary;
- a Windows integration test proves the pinned executable reaches readiness
  with the production artifact lease policy;
- an occupied or competing loopback listener cannot receive audio or satisfy
  readiness.
- an endpoint allocation-to-bind race fails closed or uses a supported listener
  handoff without sending audio to the competing listener.

Tests must use AAA structure and mock only process, Windows API, filesystem,
and transport boundaries. No test may use model weights or private audio in
the repository.

## Native Windows Verification

Native verification is performed on a real Windows runner or machine with the
pinned CPU artifacts from `.github/native-smoke/artifact-lock.template.json`.
The setup must use Python 3.12-3.14, the tracked runtime lock, the exact
NeMo-Speech.cpp executable, the exact Parakeet model, the exact FFmpeg build,
and the pinned licensed fixture.

The authoritative command is:

```text
python scripts/native_smoke.py
```

The run is accepted as a technical startup/transcription result only when all
of the following are true:

1. `runtime.verified` and `model.verified` are present.
2. `sidecar.ready` is present after authenticated readiness and attestation.
3. `asr.completed` is present with `transcript_length > 0` for cold and warm
   requests.
4. The report records `transcript_non_empty=true` and
   `warm_transcript_non_empty=true`.
5. The process-tree cancellation probes pass.
6. `runtime.cleaned` is present and no owned process or recovery resource
   remains.
7. The report contains no raw audio, transcript text, secret, authorization
   header, model path, full local path, or unredacted exception text.

Until conditions 2 and 3 are observed in the same native smoke run, the
runtime is **not** successful. In particular, a direct CLI transcript, a
`technical_passed` status with `quality_gate.status=not_evaluated`, or a
successful cross-platform test suite is insufficient.

After the startup gate passes, release completion still requires the existing
quality, latency, RSS, benchmark, and third-party-notice gates from
`rfcs/parakeet-runtime-integration.md`.

## Acceptance Criteria

- The root cause of the reported native `PermissionError` is identified by a
  phase and operation, not inferred from a generic `ExceptionGroup`.
- The primary startup error and cleanup outcome are both preserved in safe
  diagnostics.
- Failed startup leaves no unowned sidecar, child process, Job Object, artifact
  lease, proxy, transport, or reaper; pending recovery is explicit and blocks
  restart.
- Cross-platform regression tests pass without Windows binaries or model
  weights.
- Windows probes prove the chosen launch order, artifact revalidation,
  process-tree ownership, and rollback semantics.
- A native smoke run reaches `sidecar.ready` and returns a non-empty cold and
  warm transcript before this RFC can be marked technically successful.
- The native smoke run emits the required sanitized event order and exits zero
  only when cleanup also succeeds.
- No acceptance criterion is satisfied by a fake adapter or a direct CLI-only
  result.
- The release quality gate remains open until the approved corpus, WER/RTFx/RSS
  thresholds, benchmark evidence, and notices are present.

## Rollout and Rollback

### Rollout

1. Land the startup transaction and diagnostics behind the existing native
   smoke entrypoint; do not change the public ASR port.
2. Run cross-platform regression tests and the focused Windows probes.
3. Run native smoke manually, then in the scheduled Windows workflow, using
   the same pinned artifacts and report retention.
4. Promote the startup design only after the readiness and non-empty
   transcription gates pass.

### Rollback

If native startup regresses or cleanup becomes pending, disable the native
   runtime composition for that build and return an explicit setup/runtime
   failure. Do not fall back to fake transcription or silently switch backend.
Revert only the startup transaction change after preserving the failed report
   and probe evidence. Existing cross-platform fake-adapter and media-only
   diagnostic paths remain available for development; they are not native
   success evidence.

## Open Questions

- Which exact Windows operation produces the reported `PermissionError`: file
  handle acquisition, `CreateProcess`, Job Object assignment, resume, child
  model loading, proxy bind, or cleanup?
- Does the failure reproduce only while executable/model artifact leases are
  held?
- Does the pinned sidecar emit a stable bounded diagnostic when it exits before
  `/ready`, and which DLL/runtime dependencies must be recorded?
- Is a private ACL-protected staging root required, or can the current verified
  artifact lease policy launch successfully without weakening security?
- Which Windows runner architecture and emulation mode produced the latest
  report, and are the manual CLI and smoke environments identical?
- Should the sanitized report expose a numeric Win32 error code for operational
  triage, subject to the existing privacy contract?
- After technical startup passes, which named Windows baseline owns the CPU
  RTFx and RSS quality measurements?

## Decision Log

- Keep the long-lived NeMo-Speech.cpp sidecar and existing domain/application
  ports.
- Diagnose startup by phase before changing artifact-lock semantics.
- Make startup rollback transactional and cleanup outcome explicit.
- Treat `sidecar.ready` plus non-empty transcription as mandatory evidence;
  no earlier status is success.
