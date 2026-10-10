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
    constraints = (ROOT / "packaging" / "windows-build-constraints.txt").read_text(encoding="utf-8")

    assert "build: require-windows build-deps" in makefile
    assert 'install --editable ".[gui,build]"' in makefile
    assert "pip==26.2.1" in constraints
    assert "PySide6==6.12.0" in constraints
    assert "pyinstaller==6.22.3" in constraints
    assert "Pillow==12.3.0" in constraints
    assert 'install --upgrade "pip==$(PIP_VERSION)"' in makefile
    assert '--constraint "$(BUILD_CONSTRAINTS)"' in makefile
    assert "PyInstaller" in makefile
    assert "scripts/frontend_build.py" in makefile
    assert "check: spec-check format-check lint test compile" in makefile
    assert "unrelated entries found" in (ROOT / "scripts" / "frontend_build.py").read_text(
        encoding="utf-8"
    )
    assert "SMOKE_TIMEOUT_SECONDS = 120" in (ROOT / "scripts" / "frontend_build.py").read_text(
        encoding="utf-8"
    )


def test_frontend_build_generates_repository_icon_and_passes_it_to_both_pyinstaller_commands(
    tmp_path: Path, monkeypatch
) -> None:
    commands: list[list[str]] = []
    icon_output = tmp_path / "build" / "dist" / "voiceink-shell.ico"
    monkeypatch.setattr(frontend_build, "ICON_OUTPUT", icon_output)

    def fake_run(command, **kwargs) -> None:
        commands.append(command)
        if command[1] == str(frontend_build.ICON_BUILDER):
            icon_output.parent.mkdir(parents=True)
            icon_output.write_bytes(b"ico")

    monkeypatch.setattr(
        frontend_build,
        "_run",
        fake_run,
    )
    frontend_build._generate_icon("python")
    frontend_build._build_executable("voiceink-shell", "--windowed", "python")
    frontend_build._build_executable("voiceink-shell-smoke", "--console", "python")

    assert commands[0] == [
        "python",
        str(frontend_build.ICON_BUILDER),
        str(frontend_build.ICON_SOURCE),
        str(icon_output),
    ]
    pyinstaller_commands = commands[1:]
    assert len(pyinstaller_commands) == 2
    assert all(
        command[command.index("--icon") + 1] == str(icon_output) for command in pyinstaller_commands
    )
    assert all(
        f"{icon_output};{frontend_build.RUNTIME_ICON_DESTINATION}" in command
        for command in pyinstaller_commands
    )
    assert all(
        f"{frontend_build.ICON_SOURCE};{frontend_build.RUNTIME_ICON_DESTINATION}" in command
        for command in pyinstaller_commands
    )
    assert 'fill="#db594b"' in frontend_build.ICON_SOURCE.read_text(encoding="utf-8")
    assert 'd="M3 12h2l1.5-5L9 19l2-14 2.5 11 1.5-4H21"' in frontend_build.ICON_SOURCE.read_text(
        encoding="utf-8"
    )


def test_frontend_build_packages_the_complete_sqlite_migration_contract() -> None:
    source = frontend_build.MIGRATION_SOURCE
    assert {path.name for path in source.glob("*.sql")} >= {
        "001_initial.sql",
        "002_persistence_hardening.sql",
    }
    assert frontend_build.MIGRATION_DESTINATION == "voiceink_win/infrastructure/migrations"
    build_source = (ROOT / "scripts" / "frontend_build.py").read_text(encoding="utf-8")
    assert '"--add-data"' in build_source
    assert "MIGRATION_DESTINATION" in build_source


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


def test_readme_documents_windows_make_prerequisites() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "GNU Make" in readme
    assert "sh.exe" in readme
    assert "make --version" in readme


def test_frontend_smoke_reports_output_on_nonzero_exit(monkeypatch) -> None:
    class FailedProcess:
        returncode = 17

        def communicate(self, *, timeout):
            assert timeout == frontend_build.SMOKE_TIMEOUT_SECONDS
            return "smoke stdout", "smoke stderr"

    monkeypatch.setattr(frontend_build.subprocess, "Popen", lambda *args, **kwargs: FailedProcess())

    with pytest.raises(frontend_build.FrontendBuildError, match="exit code 17") as error:
        frontend_build._run_smoke()

    assert "smoke stdout" in str(error.value)
    assert "smoke stderr" in str(error.value)


def test_frontend_smoke_terminates_and_reports_output_on_timeout(monkeypatch) -> None:
    terminated = False

    class HungProcess:
        pid = 42
        returncode = None

        def communicate(self, *, timeout=None):
            if timeout is not None:
                assert timeout in {10, frontend_build.SMOKE_TIMEOUT_SECONDS}
            if self.returncode is None:
                raise frontend_build.subprocess.TimeoutExpired(
                    "voiceink-shell-smoke.exe",
                    timeout,
                    output="partial stdout",
                    stderr="partial stderr",
                )
            return "", ""

    process = HungProcess()

    def fake_terminate(candidate) -> None:
        nonlocal terminated
        assert candidate is process
        terminated = True
        candidate.returncode = -9

    monkeypatch.setattr(frontend_build.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(frontend_build, "_terminate_smoke_process", fake_terminate)

    with pytest.raises(frontend_build.FrontendBuildError, match="timed out") as error:
        frontend_build._run_smoke()

    assert terminated
    assert "partial stdout" in str(error.value)
    assert "partial stderr" in str(error.value)


def test_frontend_entrypoint_delegates_to_presentation_app() -> None:
    entrypoint = (ROOT / "scripts" / "frontend_entrypoint.py").read_text(encoding="utf-8")

    assert "from voiceink_win.presentation.app import main" in entrypoint
    assert 'package_smoke = "--package-smoke" in sys.argv' in entrypoint
    assert "raise SystemExit(main(smoke=smoke, package_smoke=package_smoke))" in entrypoint


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
