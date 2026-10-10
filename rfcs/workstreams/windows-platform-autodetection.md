# RFC: Windows Platform Autodetection for Build Artifacts

## Status

Implemented. The supported artifact matrix remains Windows x64 only.

## Summary

Make and the Windows build scripts need to distinguish Windows 10/11, host
architecture, and the architecture of the Python/toolchain process. The
detector selects the existing x64 frontend and CPU package settings, accepts an
ARM64 host only when the build process is x64 and therefore runs through
Windows x64 emulation, and rejects native ARM64 packaging explicitly. It does
not change runtime UI or claim a native ARM64 artifact.

## Goals

- Detect Windows version and host/process architecture before Windows packaging.
- Select the existing `x64` PyInstaller target and `voiceink-shell-windows-x64`
  package identity for native x64 and x64-on-ARM emulation.
- Emit actionable diagnostics for unsupported OS, old Windows, 32-bit, and
  native ARM64 environments.
- Test the decisions with mocked platform values.

## Non-Goals

- Adding a native ARM64 artifact, ARM64 sidecar, or ARM64 CI matrix entry.
- Changing application/runtime UI, runtime behavior, or artifact contents.
- Making macOS/Linux capable of producing Windows packages.

## Proposed Architecture

`scripts/windows_platform.py` owns the pure platform decision. It reads the
Windows version, `platform.machine()`, `PROCESSOR_ARCHITECTURE`, and
`PROCESSOR_ARCHITEW6432`, then returns a typed profile with `target_architecture`
fixed to `x64`. `make platform-check` and every Windows packaging entry point
use this check. The frontend builder reports the resolved profile before
PyInstaller runs; native ARM64 fails with an explicit emulation/toolchain
message. The existing x64 PE and package-name checks remain authoritative.

The decision is O(1) time and O(1) space. The test seam injects all platform
probes, so no Windows host is required to cover the matrix.

## Exit Criteria

- `make platform-check` prints the selected version, host/process architecture,
  execution mode, x64 target, and package identity on supported Windows hosts.
- Windows 10/11 x64 native and Windows ARM64 with an x64 process are accepted.
- Native ARM64, unsupported Windows, non-Windows, and 32-bit inputs fail without
  implying that a native ARM64 package exists.
- Focused platform tests, specification validation, formatting, lint, compile,
  and repository tests pass.

## Open Questions

- A native ARM64 package requires a separately pinned artifact and CI matrix;
  this RFC intentionally leaves that work for a future RFC.
