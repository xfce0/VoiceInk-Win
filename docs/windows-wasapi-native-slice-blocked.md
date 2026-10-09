# Windows WASAPI Native Slice: Blocked Scaffold

## Status

This is a contract-only scaffold. `implementation_allowed: false` remains the
authoritative state because the microphone RFC and feature specification still
mark D1-D19, G1, G2, G3a, and G4a as blocked, with `G4a=GO` required. No C++
helper, WASAPI call, Python capture adapter, fake microphone backend, package
payload, or UI capability was added.

The machine-readable contracts are:

- `spec/wasapi-protocol-v1.json` - the proposed parent/helper boundary;
- `packaging/windows-wasapi-helper-build-contract.json` - the unapproved MSVC
  C++17 build target and prohibitions; and
- `.github/native-smoke/wasapi-helper-artifact-lock.template.json` - the
  provenance, hash, relative-path, and Authenticode fields that a future
  release artifact must provide.

`scripts/wasapi_contract.py` validates these files without importing Windows
APIs. The default check permits the intentionally unresolved lock template and
still requires the scaffold to remain blocked. `--require-pinned` is an
evidence-time check and must fail until the helper has a real approved artifact
lock.

## Boundary

The target boundary preserves the existing architecture:

```text
Presentation -> Application -> Domain <- Infrastructure
                                      |
                           Windows helper adapter
```

- Domain/Application receive no COM objects, WASAPI packets, native handles,
  endpoint IDs, Windows error values, or protocol JSON.
- The future helper owns COM, WASAPI, native conversion state, and native
  cleanup.
- The future parent owns generation/deadline orchestration, the final bounded
  `CanonicalAudio` allocation, Job Object lifecycle, and typed failure mapping.
- The existing `AsrApplicationService` remains the only ASR path; the existing
  `SubprocessSupervisor`, `sidecar_protocol.py`, and `runtime_manifest.py` are
  references for process, protocol, and artifact-lock behavior, not a live
  microphone implementation.
- The existing desktop composition continues to expose recording as
  unavailable. No UI or presentation behavior is changed by this scaffold.

## Protocol Contract

The proposed v1 frame is `VIKA` plus version/type/flags, generation, sequence,
deadline, payload length, payload, and HMAC-SHA-256. The parent creates the
current-user named pipe, rejects remote clients, passes one inherited server
handle to one helper generation, verifies the connected PID and challenge proof,
and does not reconnect. The bounds are 64 KiB per frame payload, 32 KiB per
canonical chunk, and 4 MiB aggregate IPC queue.

Only canonical mono 16 kHz signed PCM16 chunks may cross this future boundary.
The protocol contract explicitly rejects native packets, native handles, and
endpoint IDs crossing it. It is a declaration for review, not a serializer or
working IPC implementation.

## Decision And Gate Mapping

| Decision/gate | Scaffold responsibility | Still required |
|---|---|---|
| D1, D11, D17 | Preserve proposed duration, queue, scratch, canonical, helper-memory, and RSS accounting as unapproved values | Product/performance approval and Windows measurement |
| D2-D6, G1, G2 | Keep lifecycle, shared ASR admission, request quiescence, recovery, and terminal publication out of this slice | Signed application/scheduler contracts and race evidence |
| D7-D12, G3a | Keep endpoint role, WASAPI mode, format matrix, converter, buffering, and live/import boundary explicit | Windows capability probe, converter/license decision, golden vectors, policy approval |
| D13-D16, G4a | Define helper process, named-pipe, package, artifact, and Job Object contract without implementation | Threat/feasibility review with `GO`, build/crash/reap evidence |
| D18-D19 | Require the named 18-lane matrix and dump/privacy policy fields in future evidence | Runner/fixture record, scanner/canaries, privacy approval |
| G3b, G4b | Keep enablement gates visible and closed | Real Windows device/conversion and clean-machine helper/package evidence |

The scaffold mirrors the complete D1-D19 gate list in each machine-readable
contract. It does not fill approval records, change RFC/spec/catalog status, or
claim that any decision is approved.

## Evidence Checklist

Before any production implementation is permitted:

- [ ] Every D1-D19 row has selected alternative, rejected alternatives,
  approver, date, evidence path, evidence SHA-256, approval-record path, and
  synchronized RFC/spec/catalog commit.
- [ ] G1 and G2 have signed contract, race, ownership, request-quiescence, and
  recovery evidence.
- [ ] G3a has approved `eCapture`/`eConsole` policy, shared event-driven
  WASAPI, exact format matrix, converter/version/license, golden vectors,
  output-length rule, and memory accounting.
- [ ] G4a has compared named pipe, loopback TCP, shared memory, and no-go
  alternatives, then recorded feasibility `GO` for auth/ACL, bounds,
  cancellation, crash containment, package, and recovery.
- [ ] MSVC, C++17, Windows SDK, converter source, and helper source are pinned
  and reproducible for the claimed x64 targets.
- [ ] The helper lock has HTTPS provenance, version, SHA-256, license,
  bundle-relative allowlist, and Authenticode publisher thumbprint. The future
  launch path must verify opened bytes and signature before resume; the adjacent
  manifest cannot be a trust root.
- [ ] G3b runs the real named hardware fixture across the specified runtime
  lanes and proves enumeration, selection, conversion, limits, cancellation,
  device loss, and deterministic output.
- [ ] G4b runs the frozen onedir lanes and proves ACL/HMAC/replay rejection,
  PID binding, Job Object kill-on-close, crash/timeout recovery, clean-machine
  artifact verification, and bounded resource behavior.
- [ ] D19 evidence proves that logs, dumps, reports, and crash artifacts contain
  no PCM, transcript text, endpoint identity, full paths, secrets, or raw native
  exception strings.
- [ ] Only after all gates and acceptance criteria pass may the catalog change
  `implementation_allowed` or `enablement_allowed`.

## Deterministic Local Checks

`make wasapi-contract-check` and the related tests validate schema, protocol
bounds, gate lists, build prohibitions, bundle-relative artifact provenance
fields, ASCII/JSON stability, and the disabled catalog state. These checks are
Windows-agnostic contract checks only and are not native evidence.
