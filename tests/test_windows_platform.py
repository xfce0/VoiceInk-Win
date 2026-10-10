from types import SimpleNamespace

import pytest

from scripts.windows_platform import WindowsPlatformError, detect_windows_platform


def _version(build: int) -> SimpleNamespace:
    return SimpleNamespace(major=10, minor=0, build=build)


def test_detects_windows_10_x64_native_build() -> None:
    profile = detect_windows_platform(
        system=lambda: "Windows",
        machine=lambda: "AMD64",
        windows_version=lambda: _version(19045),
        environ={"PROCESSOR_ARCHITECTURE": "AMD64"},
    )

    assert profile.windows_name == "Windows 10"
    assert profile.host_architecture == "x64"
    assert profile.process_architecture == "x64"
    assert profile.execution_mode == "native-x64"
    assert profile.target_architecture == "x64"
    assert profile.package_id == "voiceink-shell-windows-x64"


def test_detects_windows_11_x64_native_build() -> None:
    profile = detect_windows_platform(
        system=lambda: "Windows",
        machine=lambda: "x86_64",
        windows_version=lambda: _version(22631),
        environ={"PROCESSOR_ARCHITECTURE": "AMD64"},
    )

    assert profile.windows_name == "Windows 11"
    assert "version=Windows 11" in profile.diagnostic
    assert "native_arm64_artifact=false" in profile.diagnostic


def test_selects_x64_settings_for_arm64_host_under_emulation() -> None:
    profile = detect_windows_platform(
        system=lambda: "Windows",
        machine=lambda: "ARM64",
        windows_version=lambda: _version(26100),
        environ={"PROCESSOR_ARCHITECTURE": "AMD64"},
    )

    assert profile.host_architecture == "arm64"
    assert profile.process_architecture == "x64"
    assert profile.emulation is True
    assert profile.execution_mode == "x64-on-arm64-emulation"
    assert profile.target_architecture == "x64"
    assert "native ARM64" not in profile.diagnostic


def test_uses_native_architecture_override_for_x64_process_on_arm64() -> None:
    profile = detect_windows_platform(
        system=lambda: "Windows",
        machine=lambda: "AMD64",
        windows_version=lambda: _version(26100),
        environ={
            "PROCESSOR_ARCHITECTURE": "AMD64",
            "PROCESSOR_ARCHITEW6432": "ARM64",
        },
    )

    assert profile.host_architecture == "arm64"
    assert profile.execution_mode == "x64-on-arm64-emulation"


def test_rejects_native_arm64_without_claiming_an_arm64_artifact() -> None:
    with pytest.raises(WindowsPlatformError, match="Native ARM64.*artifact matrix"):
        detect_windows_platform(
            system=lambda: "Windows",
            machine=lambda: "ARM64",
            windows_version=lambda: _version(26100),
            environ={"PROCESSOR_ARCHITECTURE": "ARM64"},
        )


def test_rejects_non_windows_with_explicit_make_check_guidance() -> None:
    with pytest.raises(WindowsPlatformError, match="make check"):
        detect_windows_platform(
            system=lambda: "Darwin",
            machine=lambda: "arm64",
            windows_version=lambda: _version(22631),
            environ={},
        )


def test_rejects_old_windows_build() -> None:
    with pytest.raises(WindowsPlatformError, match="Windows 10/11"):
        detect_windows_platform(
            system=lambda: "Windows",
            machine=lambda: "AMD64",
            windows_version=lambda: _version(9600),
            environ={"PROCESSOR_ARCHITECTURE": "AMD64"},
        )


def test_rejects_32_bit_toolchain() -> None:
    with pytest.raises(WindowsPlatformError, match="x64 Python/toolchain"):
        detect_windows_platform(
            system=lambda: "Windows",
            machine=lambda: "x86",
            windows_version=lambda: _version(22631),
            environ={"PROCESSOR_ARCHITECTURE": "x86"},
        )
