import subprocess
import sys
from pathlib import Path

import pytest

from scripts import frontend_build
from scripts.frontend_package_smoke import WINDOWS_CONSOLE_SUBSYSTEM, validate_executable

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
WORKFLOW = ROOT / ".github" / "workflows" / "build-frontend.yml"


def test_make_build_contract_is_windows_only_and_reproducible() -> None:
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert "build: require-windows build-deps" in makefile
    assert 'install --editable ".[gui,build]"' in makefile
    assert "PyInstaller" in makefile
    assert "scripts/frontend_build.py" in makefile
    assert "check: spec-check format-check lint test compile" in makefile
    assert "unrelated entries found" in (ROOT / "scripts" / "frontend_build.py").read_text(
        encoding="utf-8"
    )


def test_frontend_build_refuses_unrelated_dist_entries(tmp_path: Path, monkeypatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "unrelated.txt").write_text("do not overwrite", encoding="utf-8")
    package_readme = tmp_path / "README.txt"
    package_readme.write_text("package", encoding="utf-8")
    monkeypatch.setattr(frontend_build, "DIST", dist)
    monkeypatch.setattr(frontend_build, "PACKAGE_README", package_readme)
    monkeypatch.setattr(frontend_build, "WORK", tmp_path / "build" / "frontend")

    with pytest.raises(frontend_build.FrontendBuildError, match="unrelated entries"):
        frontend_build._prepare_outputs()


def test_frontend_workflow_builds_a_windowed_pyside6_executable() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "runs-on: windows-2022" in workflow
    assert "Build Windows x64 GUI and smoke executables" in workflow
    assert "make build" in workflow
    assert "voiceink-shell-windows-x64" in workflow


def test_frontend_entrypoint_delegates_to_presentation_app() -> None:
    entrypoint = (ROOT / "scripts" / "frontend_entrypoint.py").read_text(encoding="utf-8")

    assert "from voiceink_win.presentation.app import main" in entrypoint
    assert "raise SystemExit(main(smoke=smoke))" in entrypoint


def test_frontend_package_smoke_validates_x64_gui_pe(tmp_path: Path) -> None:
    executable = tmp_path / "voiceink-shell.exe"
    data = bytearray(0x40 + 24 + 240)
    data[:2] = b"MZ"
    data[0x3C:0x40] = (0x40).to_bytes(4, "little")
    data[0x40:0x44] = b"PE\0\0"
    data[0x44:0x46] = (0x8664).to_bytes(2, "little")
    data[0x54:0x56] = (240).to_bytes(2, "little")
    data[0x58:0x5A] = (0x20B).to_bytes(2, "little")
    data[0x9C:0x9E] = (2).to_bytes(2, "little")
    executable.write_bytes(data)

    validate_executable(executable)

    data[0x44:0x46] = (0x14C).to_bytes(2, "little")
    executable.write_bytes(data)
    with pytest.raises(ValueError, match="expected x64 PE machine"):
        validate_executable(executable)


def test_frontend_package_smoke_validates_console_subsystem(tmp_path: Path) -> None:
    executable = tmp_path / "voiceink-shell-smoke.exe"
    data = bytearray(0x40 + 24 + 240)
    data[:2] = b"MZ"
    data[0x3C:0x40] = (0x40).to_bytes(4, "little")
    data[0x40:0x44] = b"PE\0\0"
    data[0x44:0x46] = (0x8664).to_bytes(2, "little")
    data[0x54:0x56] = (240).to_bytes(2, "little")
    data[0x58:0x5A] = (0x20B).to_bytes(2, "little")
    data[0x9C:0x9E] = (WINDOWS_CONSOLE_SUBSYSTEM).to_bytes(2, "little")
    executable.write_bytes(data)

    validate_executable(executable, expected_subsystem=WINDOWS_CONSOLE_SUBSYSTEM)

    data[0x9C:0x9E] = (2).to_bytes(2, "little")
    executable.write_bytes(data)
    with pytest.raises(ValueError, match="expected Windows subsystem 3"):
        validate_executable(executable, expected_subsystem=WINDOWS_CONSOLE_SUBSYSTEM)


def test_frontend_package_smoke_cli_checks_console_subsystem(tmp_path: Path) -> None:
    executable = tmp_path / "voiceink-shell-smoke.exe"
    data = bytearray(0x40 + 24 + 240)
    data[:2] = b"MZ"
    data[0x3C:0x40] = (0x40).to_bytes(4, "little")
    data[0x40:0x44] = b"PE\0\0"
    data[0x44:0x46] = (0x8664).to_bytes(2, "little")
    data[0x54:0x56] = (240).to_bytes(2, "little")
    data[0x58:0x5A] = (0x20B).to_bytes(2, "little")
    data[0x9C:0x9E] = (WINDOWS_CONSOLE_SUBSYSTEM).to_bytes(2, "little")
    executable.write_bytes(data)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "frontend_package_smoke.py"),
            str(executable),
            "--subsystem",
            "console",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "passed" in result.stdout

    data[0x9C:0x9E] = (2).to_bytes(2, "little")
    executable.write_bytes(data)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "frontend_package_smoke.py"),
            str(executable),
            "--subsystem",
            "console",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "expected Windows subsystem 3" in result.stderr
