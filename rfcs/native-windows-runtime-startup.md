# RFC: Native Windows Runtime Startup Diagnostics and Safe Rollback

## Status

Proposed. This RFC is deliberately limited to one executable slice: preserve
the primary startup failure, prevent a second kill request for a stopped
process, add bounded phase/operation/Win32 diagnostics, and define the Windows
probe/test plan. It does not implement production code. It does not claim that
the native runtime is working or that the broader security design is complete.

## Summary

The latest native smoke report failed at `application_start` with a nested
`PermissionError` and `ExceptionGroup`. The current rollback can attempt a
supervisor kill after a terminate operation has already stopped the process.
That secondary failure can hide the operation that first failed.

This RFC makes the startup boundary observable and transactional without
changing the runtime, endpoint, proxy, artifact-lock, or attestation security
model. The implementation slice will:

1. capture the first typed startup failure as the immutable primary failure;
2. track process ownership and state so a stopped process is never killed a
   second time;
3. emit a bounded, sanitized ledger of startup and cleanup phases, operations,
   exception types/codes, and optional numeric Win32 error codes; and
4. validate the behavior with deterministic tests and focused Windows probes.

The slice is diagnostic and lifecycle work. A passing cross-platform test suite
or a direct CLI transcription is not native application success.

## Evidence and Scope Boundary

The native smoke report at
`/tmp/opencode/native-smoke-37762930921/VoiceInk-Win/VoiceInk-Win/native-smoke-report.json`
records `failure_stage=application_start`, `PermissionError` in its cause
chain, and an application-close error. It does not prove sidecar readiness or
transcription.

The existing architecture remains the reference boundary:

```text
native_smoke.py
  -> BackendApplication.start()
  -> LoopbackProxy.start()
  -> NeMoSidecarRuntime.start()
  -> SubprocessSupervisor.start()
  -> suspended process + Windows Job Object
  -> resume + readiness/attestation paths
```

This RFC changes only the startup error/rollback observation at the
`NeMoSidecarRuntime` and `SubprocessSupervisor` infrastructure boundary. The
following items are explicit follow-up RFCs and open blockers, not solutions in
this document:

| Blocker | Follow-up RFC | Boundary of this RFC |
|---|---|---|
| Endpoint allocation-to-bind race and listener ownership | `rfcs/native-endpoint-ownership.md` | Validate the configured endpoint shape only; do not claim reservation or handoff. |
| Proxy authentication and authorization contract | `rfcs/native-proxy-authentication.md` | Preserve the existing transport boundary; do not strengthen or re-specify authentication. |
| Full model/capability attestation strengthening | `rfcs/native-attestation-strengthening.md` | Observe the existing attestation operation as a phase; do not claim stronger attestation. |
| Independent trust root and provenance for the artifact lock | `rfcs/native-artifact-trust-root.md` | Consume the existing trusted lock and report verification outcomes; do not redesign its trust root. |

These files are proposed follow-up RFCs and are not catalogued by this change
because they have no approved content yet. Each is an open blocker for the
corresponding security claim. No acceptance criterion below passes by treating
one of these deferred items as solved.

## Goals

- Preserve the first startup failure with a stable phase, operation, error
  code/type, and optional Win32 error number.
- Keep cleanup failures separate from the primary startup failure.
- Make process ownership and stopped/running state explicit enough to prevent a
  second kill request for a stopped process.
- Bound the number and size of diagnostic records and redact sensitive data.
- Define one unambiguous launch order for the lifecycle slice.
- Add behavior-focused cross-platform tests and real Windows probes for the
  process/Job Object boundary.
- Keep the existing Clean Architecture boundary and long-lived sidecar design.

## Non-Goals

- Implementing production code in this RFC change.
- Fixing endpoint allocation, listener reservation, listener handoff, or the
  allocation-to-bind race.
- Changing proxy authentication, authorization, API-key handling, or readiness
  authentication.
- Strengthening model/capability attestation or making a new attestation claim.
- Creating an independent artifact-lock trust root, provenance verifier, or
  release-signing design.
- Changing artifact hashes, allowed paths, sharing flags, retry policy, ASR
  protocol, audio format, model, backend, or public application ports.
- Adding a fake runtime fallback or treating direct CLI output as application
  success.
- Claiming native success before the existing readiness and transcription gates
  pass in one native smoke run.

## Proposed Architecture

### 1. Startup transaction and ownership state

`SubprocessSupervisor` owns a per-start transaction. Every acquired resource
has an ownership flag, and cleanup operates only on resources acquired by that
transaction. The process state machine is:

```text
not_created
  -> created_suspended
  -> running
  -> stop_requested
  -> exited
  -> reaped
```

`stop_owned_process()` is idempotent and has one termination gate:

1. If no process was created, record `process_not_owned` and do not call a
   termination API.
2. If termination was already requested, record `termination_already_requested`
   and do not call a termination API again.
3. Query the owned process handle with a zero-timeout wait. If it is already
   signaled, record `process_already_stopped`, reap/close it once, and do not
   call a termination API.
4. Only an owned, unsignaled process may transition to `stop_requested` and
   receive one termination request. The bounded wait/reap result is recorded.

If the process exits between the state query and the termination call, the
Windows probe must define the observed Win32 result as an already-stopped
outcome and prevent any subsequent termination attempt. Cleanup callers share
the same per-process gate; repeated rollback cannot issue a second kill.

If any owned resource cannot be proven cleaned by the deadline, the transaction
enters `recovery_pending`. A new start is rejected with a stable
`recovery_pending` error until the recovery owner resolves the resource; a
second start must not create another process or Job Object.

### 2. Primary failure and cleanup outcome

The first failure in the startup transaction becomes immutable `primary_failure`.
It contains only stable fields:

```text
startup_phase
operation
error_code
error_type
win32_error_code (optional)
```

Cleanup produces a separate outcome: `complete`, `failed`, or `pending`. A
cleanup failure may add bounded `cleanup_failures`, but it must not replace the
primary failure with an `ExceptionGroup`, raw exception, or generic execution
error. An exception group may remain an internal Python detail; report
serialization exposes the stable fields above.

The first failure wins even when rollback encounters several errors. If no
startup failure occurred, a cleanup failure remains a cleanup failure and is
not rewritten as a startup failure.

### 3. Bounded diagnostic ledger

The ledger uses a fixed vocabulary for phases and operations. At minimum it
supports these phase values:

```text
configuration
artifact_verification
artifact_lease
process_create_suspended
job_assignment
artifact_revalidation
resume
readiness_probe
attestation_passthrough
cleanup
```

At minimum it supports these operation values:

```text
validate_endpoint
open_artifact
verify_artifact
create_process
assign_job
revalidate_artifact
resume_process
probe_readiness
observe_attestation
get_process_state
terminate_process
wait_for_exit
reap_process
close_resource
```

Each record contains `startup_phase`, `operation`, a stable `error_code` or
success code, `error_type` when failed, and `win32_error_code` when a Windows
API supplies one. It never contains paths, command lines, secrets, raw
exception messages, audio, model bytes, or transcript text.

The bounds are part of the contract: at most 32 startup records, at most 16
cleanup-failure records, at most 128 characters for each enum/code field, and
at most 64 KiB for the serialized diagnostic section. Overflow is represented
by one `diagnostics_truncated` marker and cannot append unbounded text. Win32
codes are numeric unsigned 32-bit values; their presence does not authorize
raw Windows messages.

### 4. Canonical launch order

The following order is authoritative for this RFC and resolves the readiness
wording conflict:

1. Validate configuration and endpoint syntax only.
2. Verify executable/model identity and hashes with the existing artifact
   verifier and trusted lock.
3. Acquire the existing approved artifact lease.
4. Create the sidecar suspended with the existing safe process settings.
5. Assign the process to the existing kill-on-close Job Object.
6. Revalidate artifact identity and hashes before resume.
7. Resume the process.
8. Run the bounded existing `/ready` observation and record its phase/result.
9. Run the existing attestation operation unchanged and record its phase/result.
10. Publish the existing `sidecar.ready` event only after the application start
    contract completes; this event is not emitted by the diagnostic ledger.

In this document, `/ready` is a process/readiness observation, while
`sidecar.ready` is an application success event. The former precedes the
existing attestation operation; the latter follows the complete existing start
contract. This RFC does not change either authentication or attestation. A
failure at any step records the first failing operation and rolls back acquired
resources in reverse order.

The endpoint race, proxy authentication, attestation strengthening, and lock
trust-root follow-ups remain outside this order. In particular, this order is
not evidence that a closed-socket port is owned, that the proxy is newly
authenticated, that attestation is stronger, or that the lock has an
independent trust root.

### 5. Complexity and safety

Ledger append and ownership transitions are `O(1)` per lifecycle operation and
use `O(p)` space for `p` owned resources plus the fixed diagnostic bounds.
Artifact hashing remains `O(n)` in artifact bytes with `O(1)` working memory
beyond the bounded I/O buffer. Native loading and inference are outside the
algorithmic scope and are measured by the Windows probes.

## Windows Probe and Test Plan

All Windows probes use the same account, Python version, runner image, pinned
artifacts, runtime manifest, and fixture as native smoke. Probe output is
sanitized and contains only stable phase/operation/error data. No probe changes
production files or weakens artifact verification.

### Cross-platform behavior tests

Preserve the existing tests in `test_sidecar_runtime.py`,
`test_process_supervisor.py`, `test_native_smoke.py`, `test_runtime_manifest.py`,
`test_transport.py`, and `test_reporting.py`. Add AAA tests with these exact
behaviors:

| ID | Behavior | Required assertion |
|---|---|---|
| T-01 | Failure before process creation | The original typed error is `primary_failure`; no termination API is called. |
| T-02 | Process created, then already stopped | Cleanup reaps/closes once and calls no termination API. |
| T-03 | Running owned process | Exactly one termination request is issued and the process is awaited/reaped. |
| T-04 | Repeated/concurrent cleanup | The termination gate is idempotent; no second kill request occurs. |
| T-05 | Cleanup error after startup error | The startup phase/operation/error remains unchanged; cleanup is a separate outcome. |
| T-06 | Diagnostic overflow/redaction | Bounds are enforced and no path, secret, raw exception, audio, model data, or transcript is serialized. |
| T-07 | Launch-order ledger | Events prove `revalidate_artifact` precedes `resume_process`; readiness and attestation are after resume. |
| T-08 | Stable Win32 mapping | A supplied numeric Win32 error is retained without a raw Windows message. |
| T-09 | Pending recovery | A failed cleanup marks recovery pending and a second start is rejected without creating a process. |

Tests mock only process, Windows API, filesystem, and transport boundaries. No
model weights or private audio are checked into the repository.

### Focused Windows probes

| ID | Probe | Pass condition |
|---|---|---|
| W-01 | Diagnostic suspended launch using the pinned executable, safe argv, existing artifact lease, Job Object, revalidation, and resume | The trace matches the canonical order and reports the first failing Windows operation, if any. |
| W-02 | Inject or reproduce failures at `CreateProcess`, Job Object assignment, revalidation, resume, readiness observation, and cleanup | Each failure has a distinct bounded phase/operation record and does not publish `sidecar.ready`. |
| W-03 | Start a child that exits immediately; invoke rollback twice after observing the stopped handle | The first rollback records `process_already_stopped`; zero termination calls are made; the second rollback is a no-op. |
| W-04 | Cause a primary launch failure and an independent cleanup failure | The report retains the primary failure and lists cleanup separately without an `ExceptionGroup` replacement. |
| W-05 | Run with a process that remains alive until cleanup | One termination request, bounded wait, Job Object cleanup, and reaping are observed. |
| W-06 | Run the existing native smoke after the slice is implemented | The report contains bounded diagnostics; full native success remains subject to the existing readiness, transcript, quality, and release gates. |

The endpoint race, proxy authentication, full attestation strengthening, and
independent lock trust-root probes are explicitly deferred to their follow-up
RFCs. This test plan must not report them as passing based on W-01 through W-06.

## Acceptance Criteria

The implementation of this RFC is complete only when every criterion below is
demonstrated by the linked test/probe evidence. A criterion is pass/fail; no
manual interpretation of a generic smoke status substitutes for the stated
observation.

| ID | Exact acceptance criterion | Traceability |
|---|---|---|
| AC-01 | For every injected startup failure, the serialized report contains exactly one immutable `primary_failure` with `startup_phase`, `operation`, `error_code`, and `error_type`; it contains `win32_error_code` when supplied by Windows. | G-01, T-01, T-08, W-02, W-04 |
| AC-02 | When cleanup also fails, the original `primary_failure` fields are byte-for-byte unchanged and cleanup is represented separately as `complete`, `failed`, or `pending`; no serialized `ExceptionGroup` replaces it. | G-02, T-05, W-04 |
| AC-03 | If no process was created, or the owned process handle is already signaled, the cleanup trace contains zero termination API calls. | G-03, T-01, T-02, W-03 |
| AC-04 | For one owned running process, all repeated or concurrent cleanup paths produce at most one termination API call and one reap/close sequence. | G-03, T-03, T-04, W-05 |
| AC-05 | A diagnostic section never exceeds 32 startup records, 16 cleanup-failure records, 128 characters per enum/code field, or 64 KiB serialized size; overflow emits only `diagnostics_truncated`. | G-04, T-06 |
| AC-06 | The Windows trace proves artifact revalidation occurs before resume, Job Object assignment occurs before resume, and readiness/attestation observation occurs only after resume. | G-05, T-07, W-01, W-02 |
| AC-07 | Failure at any startup phase emits neither `sidecar.ready` nor `asr.completed`, and all acquired resources are either cleaned or explicitly marked pending with a second start rejected before process creation. | G-01, G-02, T-07, T-09, W-02, W-04 |
| AC-08 | `make check` and `python scripts/spec_check.py` pass; no production source, test implementation, runtime artifact, or model file is changed by this RFC commit. | G-06, repository diff, verification log |
| AC-09 | The RFC and catalog explicitly identify endpoint race, proxy authentication, full attestation strengthening, and independent lock trust-root as follow-up blockers; no acceptance result claims those items are solved. | G-07, follow-up table, deferred-probe statement |

Traceability goals:

| Goal | Definition |
|---|---|
| G-01 | Preserve the first failure and expose its phase/operation. |
| G-02 | Separate primary startup failure from cleanup outcome. |
| G-03 | Make process ownership and termination idempotent. |
| G-04 | Bound and sanitize diagnostics. |
| G-05 | Establish one authoritative launch order. |
| G-06 | Keep the change specification-only and pass repository checks. |
| G-07 | Prevent deferred security work from being represented as delivered. |

## Exit Criteria

This RFC may move from Proposed to Approved only after review confirms that the
scope is limited to the executable slice, the launch order above is the sole
ordering contract, and all four follow-up blockers have named owners or
accepted issue references. The implementation may begin only after approval.

The implementation may move from Approved to technically complete only after
AC-01 through AC-09 pass with T-01 through T-08 and W-01 through W-06 evidence.
That status does not declare native transcription success. Native success still
requires the existing `sidecar.ready`, non-empty cold and warm transcription,
cleanup, quality, latency, memory, benchmark, and third-party-notice gates.

## Rollout and Rollback

### Rollout

1. Implement the transaction ledger and termination gate behind the existing
   native smoke path; do not change public ASR ports or deferred security
   semantics.
2. Run T-01 through T-08 on every supported development platform.
3. Run W-01 through W-06 on the pinned Windows environment.
4. Retain sanitized failure reports and probe traces for root-cause analysis.

### Rollback

If diagnostics regress, cleanup becomes pending unexpectedly, or any Windows
probe violates the termination gate, disable the native runtime composition for
that build and return an explicit setup/runtime failure. Do not fall back to
fake transcription or silently switch backend. Revert only the implementation
of this slice after preserving the failing report and probe evidence.

## Open Questions

- Which exact Windows operation produces the reported `PermissionError`? W-01
  and W-02 are intended to answer this without guessing from an
  `ExceptionGroup`.
- What exact process-handle result is observed when the child exits between the
  zero-timeout state query and the termination call? W-03 must record the
  numeric result and define the no-second-kill mapping.
- Does the pinned runner expose a stable numeric Win32 error for each failure
  point without requiring raw Windows messages? W-02 and W-04 must answer this.
- Which owners and issue references will be assigned to the four follow-up
  RFCs before this RFC is approved?

## Decision Log

- Narrow the RFC to primary-error preservation, stopped-process kill safety,
  bounded diagnostics, and Windows validation.
- Treat `/ready` observation and `sidecar.ready` publication as distinct
  concepts; only the latter is an application success event.
- Keep the canonical order `revalidate -> resume -> readiness observation ->
  existing attestation -> sidecar.ready publication`.
- Keep endpoint ownership, proxy authentication, attestation strengthening, and
  lock trust-root design as explicit follow-up blockers.
- Recommend Proposed, not Approved, until the design review and follow-up
  ownership conditions in Exit Criteria are satisfied.
