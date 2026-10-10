"""Detect the Windows host and select the supported build artifact profile."""

from __future__ import annotations

import argparse
import os
import platform
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass

X64 = "x64"
ARM64 = "arm64"
X86 = "x86"
UNKNOWN = "unknown"
MIN_WINDOWS_BUILD = 10240
WINDOWS_11_BUILD = 22000


class WindowsPlatformError(RuntimeError):
    """Raised when the current host cannot produce a supported Windows artifact."""


@dataclass(frozen=True)
class WindowsPlatform:
    """Resolved host and artifact settings for a Windows build."""

    windows_name: str
    windows_major: int
    windows_minor: int
    windows_build: int
    host_architecture: str
    process_architecture: str
    target_architecture: str
    emulation: bool

    @property
    def package_id(self) -> str:
        return "voiceink-shell-windows-x64"

    @property
    def execution_mode(self) -> str:
        return "x64-on-arm64-emulation" if self.emulation else "native-x64"

    @property
    def diagnostic(self) -> str:
        return (
            f"version={self.windows_name} (build {self.windows_build}), "
            f"host_architecture={self.host_architecture}, "
            f"process_architecture={self.process_architecture}, "
            f"mode={self.execution_mode}, target_architecture={self.target_architecture}, "
            "native_arm64_artifact=false"
        )


def _normalise_architecture(value: str | None) -> str:
    token = (value or "").strip().casefold().replace("-", "").replace("_", "")
    return {
        "amd64": X64,
        "x8664": X64,
        "x64": X64,
        "arm64": ARM64,
        "aarch64": ARM64,
        "x86": X86,
        "i386": X86,
        "i686": X86,
    }.get(token, UNKNOWN)


def _environment_value(environ: Mapping[str, str], name: str) -> str | None:
    folded_name = name.casefold()
    for key, value in environ.items():
        if key.casefold() == folded_name:
            return value
    return None


def _windows_version(value: object) -> tuple[int, int, int]:
    components: list[int] = []
    for name, index in (("major", 0), ("minor", 1), ("build", 2)):
        component = getattr(value, name, None)
        if component is None:
            try:
                component = value[index]  # type: ignore[index]
            except (IndexError, TypeError) as error:
                raise WindowsPlatformError(
                    "Windows version could not be read from the host"
                ) from error
        try:
            components.append(int(component))
        except (TypeError, ValueError) as error:
            raise WindowsPlatformError("Windows version could not be read from the host") from error
    return components[0], components[1], components[2]


def detect_windows_platform(
    *,
    system: Callable[[], str] | None = None,
    machine: Callable[[], str] | None = None,
    windows_version: Callable[[], object] | None = None,
    environ: Mapping[str, str] | None = None,
) -> WindowsPlatform:
    """Resolve and validate the platform using injectable system probes."""
    system_reader = system or platform.system
    if system_reader() != "Windows":
        raise WindowsPlatformError(
            "Windows packaging is unavailable on this host; run `make check` on macOS/Linux."
        )

    version_reader = windows_version or getattr(sys, "getwindowsversion", None)
    if version_reader is None:
        raise WindowsPlatformError("Windows version probe is unavailable")
    major, minor, build = _windows_version(version_reader())
    if major != 10 or build < MIN_WINDOWS_BUILD:
        raise WindowsPlatformError(
            f"Windows 10/11 is required; detected Windows {major}.{minor} build {build}."
        )
    windows_name = "Windows 11" if build >= WINDOWS_11_BUILD else "Windows 10"

    environment = os.environ if environ is None else environ
    machine_value = (machine or platform.machine)()
    process_architecture = _normalise_architecture(
        _environment_value(environment, "PROCESSOR_ARCHITECTURE") or machine_value
    )
    native_override = _normalise_architecture(
        _environment_value(environment, "PROCESSOR_ARCHITEW6432")
    )
    host_architecture = (
        native_override if native_override != UNKNOWN else _normalise_architecture(machine_value)
    )
    if host_architecture == UNKNOWN:
        host_architecture = process_architecture

    if host_architecture == X64 and process_architecture == X64:
        emulation = False
    elif host_architecture == ARM64 and process_architecture == X64:
        emulation = True
    elif host_architecture == ARM64 and process_architecture == ARM64:
        raise WindowsPlatformError(
            "Native ARM64 Windows packaging is not supported by the artifact matrix. "
            "Install and run the x64 Python/toolchain under Windows x64 emulation."
        )
    elif host_architecture == X86 or process_architecture == X86:
        raise WindowsPlatformError(
            "Windows packaging requires an x64 Python/toolchain; 32-bit Windows is unsupported."
        )
    else:
        raise WindowsPlatformError(
            "Unable to resolve a supported x64 Windows build environment: "
            f"host={host_architecture}, process={process_architecture}."
        )

    return WindowsPlatform(
        windows_name=windows_name,
        windows_major=major,
        windows_minor=minor,
        windows_build=build,
        host_architecture=host_architecture,
        process_architecture=process_architecture,
        target_architecture=X64,
        emulation=emulation,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate and print build settings")
    parser.parse_args()
    try:
        profile = detect_windows_platform()
    except WindowsPlatformError as error:
        print(f"windows platform: ERROR: {error}", file=sys.stderr)
        return 1
    print(f"windows platform: OK: {profile.diagnostic}; package_id={profile.package_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
