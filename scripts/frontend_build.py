"""Build and validate the Windows PySide6 frontend package."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
WORK = ROOT / "build" / "frontend"
PACKAGE_README = ROOT / "packaging" / "voiceink-shell-windows-x64" / "README.txt"
VALIDATOR = ROOT / "scripts" / "frontend_package_smoke.py"
ENTRYPOINT = ROOT / "scripts" / "frontend_entrypoint.py"
EXPECTED_OUTPUTS = {
    "README.txt",
    "voiceink-shell.exe",
    "voiceink-shell-smoke.exe",
}


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


def _require_supported_host() -> None:
    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise FrontendBuildError(
            "Frontend packaging requires 64-bit Windows. "
            "Run `make check` on macOS/Linux, or run `make build` on Windows 10/11 x64."
        )
    if sys.version_info[:2] not in {(3, 12), (3, 13), (3, 14)}:
        raise FrontendBuildError(
            f"Python 3.12, 3.13, or 3.14 is required; found {platform.python_version()}"
        )


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
        "--distpath",
        str(DIST),
        "--workpath",
        str(workpath),
        "--specpath",
        str(specpath),
        mode,
        "--name",
        name,
        str(ENTRYPOINT),
    ]
    _run(command)


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


def _run_smoke() -> None:
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    _run([str(DIST / "voiceink-shell-smoke.exe"), "--smoke"], env=environment)


def main() -> int:
    try:
        _require_supported_host()
        _prepare_outputs()
        python = sys.executable
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
