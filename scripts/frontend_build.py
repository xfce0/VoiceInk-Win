"""Build and validate the Windows PySide6 frontend package."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from scripts.windows_platform import (
        WindowsPlatform,
        WindowsPlatformError,
        detect_windows_platform,
    )
except ModuleNotFoundError:
    from windows_platform import WindowsPlatform, WindowsPlatformError, detect_windows_platform

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
WORK = ROOT / "build" / "frontend"
ICON_BUILD_DIR = ROOT / "build" / "dist"
ICON_SOURCE = ROOT / "packaging" / "voiceink-shell-windows-x64" / "voiceink-transcribe.png"
SIDEBAR_ICON_SOURCE = ROOT / "packaging" / "voiceink-shell-windows-x64" / "voiceink-shell.svg"
ICON_BUILDER = ROOT / "scripts" / "build_icon.py"
ICON_OUTPUT = ICON_BUILD_DIR / "voiceink-transcribe.ico"
RUNTIME_ICON_DESTINATION = "."
PACKAGE_README = ROOT / "packaging" / "voiceink-shell-windows-x64" / "README.txt"
VALIDATOR = ROOT / "scripts" / "frontend_package_smoke.py"
ENTRYPOINT = ROOT / "scripts" / "frontend_entrypoint.py"
MIGRATION_SOURCE = ROOT / "src" / "voiceink_win" / "infrastructure" / "migrations"
MIGRATION_DESTINATION = "voiceink_win/infrastructure/migrations"
EXPECTED_OUTPUTS = {
    "README.txt",
    "voiceink-shell.exe",
    "voiceink-shell-smoke.exe",
}
SMOKE_TIMEOUT_SECONDS = 120


class FrontendBuildError(RuntimeError):
    """Raised when the frontend package cannot be built safely."""


def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    try:
        result = subprocess.run(command, cwd=ROOT, env=env, check=False)
    except OSError as error:
        raise FrontendBuildError(f"Unable to run {command[0]!r}: {error}") from error
    if result.returncode != 0:
        rendered = " ".join(command)
        raise FrontendBuildError(f"Command failed with exit code {result.returncode}: {rendered}")


def _require_supported_host() -> WindowsPlatform:
    try:
        profile = detect_windows_platform()
    except WindowsPlatformError as error:
        raise FrontendBuildError(str(error)) from error
    if sys.version_info[:2] not in {(3, 12), (3, 13), (3, 14)}:
        raise FrontendBuildError(
            f"Python 3.12, 3.13, or 3.14 is required; found {platform.python_version()}"
        )
    return profile


def _prepare_outputs() -> None:
    if not PACKAGE_README.is_file():
        raise FrontendBuildError(f"Missing package README: {PACKAGE_README}")
    if DIST.exists():
        unexpected = sorted(
            path.name for path in DIST.iterdir() if path.name not in EXPECTED_OUTPUTS
        )
        if unexpected:
            names = ", ".join(unexpected)
            raise FrontendBuildError(
                f"Refusing to modify dist/: unrelated entries found ({names}). "
                "Move them elsewhere or run `make clean` explicitly before building."
            )
        existing_readme = DIST / "README.txt"
        if (
            existing_readme.is_file()
            and existing_readme.read_bytes() != PACKAGE_README.read_bytes()
        ):
            raise FrontendBuildError(
                "Refusing to replace dist/README.txt because it is not the package README. "
                "Move it elsewhere or run `make clean` explicitly before building."
            )
        for name in EXPECTED_OUTPUTS:
            path = DIST / name
            if path.is_dir():
                raise FrontendBuildError(f"Refusing to replace directory in dist/: {path}")
            if path.exists():
                path.unlink()
    else:
        DIST.mkdir(parents=True)

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)


def _build_executable(name: str, mode: str, python: str) -> None:
    if not MIGRATION_SOURCE.is_dir() or not all(
        (MIGRATION_SOURCE / name).is_file()
        for name in ("001_initial.sql", "002_persistence_hardening.sql")
    ):
        raise FrontendBuildError(f"SQLite migration package is incomplete: {MIGRATION_SOURCE}")
    workpath = WORK / name
    specpath = WORK / "specs"
    command = [
        python,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--paths",
        str(ROOT / "src"),
        "--collect-all",
        "PySide6",
        "--collect-all",
        "shiboken6",
        "--add-data",
        f"{MIGRATION_SOURCE};{MIGRATION_DESTINATION}",
        "--add-data",
        f"{ICON_OUTPUT};{RUNTIME_ICON_DESTINATION}",
        "--add-data",
        f"{ICON_SOURCE};{RUNTIME_ICON_DESTINATION}",
        "--add-data",
        f"{SIDEBAR_ICON_SOURCE};{RUNTIME_ICON_DESTINATION}",
        "--distpath",
        str(DIST),
        "--workpath",
        str(workpath),
        "--specpath",
        str(specpath),
        "--icon",
        str(ICON_OUTPUT),
        mode,
        "--name",
        name,
        str(ENTRYPOINT),
    ]
    _run(command)


def _generate_icon(python: str) -> None:
    if not ICON_SOURCE.is_file() or ICON_SOURCE.stat().st_size == 0:
        raise FrontendBuildError(f"Missing repository-owned application icon source: {ICON_SOURCE}")
    if not SIDEBAR_ICON_SOURCE.is_file():
        raise FrontendBuildError(
            f"Missing repository-owned sidebar icon source: {SIDEBAR_ICON_SOURCE}"
        )
    _run([python, str(ICON_BUILDER), str(ICON_SOURCE), str(ICON_OUTPUT)])
    if not ICON_OUTPUT.is_file() or ICON_OUTPUT.stat().st_size == 0:
        raise FrontendBuildError(f"Icon builder did not produce a non-empty {ICON_OUTPUT}")


def _validate_package(python: str) -> None:
    for name, subsystem in (
        ("voiceink-shell.exe", "gui"),
        ("voiceink-shell-smoke.exe", "console"),
    ):
        executable = DIST / name
        if not executable.is_file() or executable.stat().st_size == 0:
            raise FrontendBuildError(f"PyInstaller did not produce a non-empty {executable}")
        _run([python, str(VALIDATOR), str(executable), "--subsystem", subsystem])

    if not PACKAGE_README.is_file():
        raise FrontendBuildError(f"Missing package README: {PACKAGE_README}")
    target_readme = DIST / "README.txt"
    shutil.copy2(PACKAGE_README, target_readme)
    if target_readme.read_bytes() != PACKAGE_README.read_bytes():
        raise FrontendBuildError("Package README was not copied byte-for-byte")

    unexpected = sorted(path.name for path in DIST.iterdir() if path.name not in EXPECTED_OUTPUTS)
    if unexpected:
        raise FrontendBuildError(f"Unexpected entries in dist/: {', '.join(unexpected)}")


def _format_process_output(output: str | bytes | None) -> str:
    if output is None:
        return "<no output>"
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    return output.strip() or "<no output>"


def _terminate_smoke_process(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if result.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


def _run_smoke() -> None:
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    command = [str(DIST / "voiceink-shell-smoke.exe"), "--smoke"]
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        close_fds=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=SMOKE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        _terminate_smoke_process(process)
        cleanup_error = None
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired as error_after_cleanup:
            _terminate_smoke_process(process)
            cleanup_error = error_after_cleanup
            stdout, stderr = "", ""
        stdout = stdout or error.stdout
        stderr = stderr or error.stderr
        cleanup_note = (
            "\nProcess cleanup did not finish within 10 seconds."
            if cleanup_error is not None
            else ""
        )
        raise FrontendBuildError(
            f"Frontend smoke timed out after {SMOKE_TIMEOUT_SECONDS} seconds.\n"
            f"stdout:\n{_format_process_output(stdout)}\n"
            f"stderr:\n{_format_process_output(stderr)}{cleanup_note}"
        ) from error

    if process.returncode != 0:
        raise FrontendBuildError(
            f"Frontend smoke failed with exit code {process.returncode}.\n"
            f"stdout:\n{_format_process_output(stdout)}\n"
            f"stderr:\n{_format_process_output(stderr)}"
        )


def main() -> int:
    try:
        profile = _require_supported_host()
        print(f"frontend build: selected x64 settings ({profile.diagnostic})")
        _prepare_outputs()
        python = sys.executable
        _generate_icon(python)
        _build_executable("voiceink-shell", "--windowed", python)
        _build_executable("voiceink-shell-smoke", "--console", python)
        _validate_package(python)
        _run_smoke()
    except FrontendBuildError as error:
        print(f"frontend build: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"frontend build: filesystem error: {error}", file=sys.stderr)
        return 1
    print(f"Frontend package ready: {DIST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
