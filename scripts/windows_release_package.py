"""Build and smoke-test the pinned relocatable Windows x64 release package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

try:
    from scripts.native_smoke_lock import load_native_smoke_lock
    from scripts.portable_package import build_package
except ModuleNotFoundError:
    from native_smoke_lock import load_native_smoke_lock
    from portable_package import build_package

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / ".github" / "native-smoke" / "artifact-lock.template.json"
DEFAULT_OUTPUT = ROOT / "release" / "voiceink-shell-windows-x64"
DEFAULT_BUNDLE = ROOT / "release" / "voiceink-shell-windows-x64.zip"
DEFAULT_REPORT = ROOT / "release" / "windows-release-smoke.json"
SIDECAR_ID = "nemo-speech-cpp-windows-amd64"
MODEL_ID = "parakeet-tdt-0.6b-v3.oss-align.q8_0"
DOWNLOAD_TIMEOUT_SECONDS = 300
SMOKE_TIMEOUT_SECONDS = 600


class WindowsReleasePackageError(RuntimeError):
    """Raised when the Windows release package contract cannot be completed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_verified(url: str, expected_sha256: str, destination: Path, label: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "VoiceInk-Win release package smoke"})
    try:
        with (
            urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response,
            destination.open("wb") as target,
        ):
            shutil.copyfileobj(response, target, length=1024 * 1024)
    except (OSError, ValueError) as error:
        destination.unlink(missing_ok=True)
        raise WindowsReleasePackageError(f"{label} download failed") from error
    actual_sha256 = _sha256(destination)
    if actual_sha256.lower() != expected_sha256.lower():
        destination.unlink(missing_ok=True)
        raise WindowsReleasePackageError(f"{label} checksum mismatch")
    return destination


def _extract_archive(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    try:
        with ZipFile(archive) as source:
            for member in source.infolist():
                relative = PurePosixPath(member.filename.replace("\\", "/"))
                if relative == PurePosixPath("."):
                    continue
                if relative.is_absolute() or ".." in relative.parts:
                    raise WindowsReleasePackageError("archive contains an unsafe path")
                mode = (member.external_attr >> 16) & stat.S_IFMT(0o170000)
                if mode == stat.S_IFLNK:
                    raise WindowsReleasePackageError("archive contains a symbolic link")
                target = root.joinpath(*relative.parts)
                resolved_target = target.resolve()
                if not resolved_target.is_relative_to(root):
                    raise WindowsReleasePackageError("archive contains an unsafe path")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as source_stream, target.open("wb") as target_stream:
                    shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
    except BadZipFile as error:
        raise WindowsReleasePackageError("downloaded archive is not a ZIP file") from error


def _extract_verified_executable(
    archive: Path,
    destination: Path,
    executable_name: str,
    expected_sha256: str,
    label: str,
) -> Path:
    _extract_archive(archive, destination)
    candidates = [
        path
        for path in destination.rglob(executable_name)
        if path.is_file() and not path.is_symlink()
    ]
    if len(candidates) != 1:
        raise WindowsReleasePackageError(
            f"{label} archive must contain exactly one {executable_name}"
        )
    executable = candidates[0]
    if _sha256(executable).lower() != expected_sha256.lower():
        raise WindowsReleasePackageError(f"{label} executable checksum mismatch")
    return executable


def _terminate_process(process: subprocess.Popen[str]) -> None:
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


def _output_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return (value or "<no output>").strip()[-4000:]


def _run_relocation_smoke(package: Path) -> None:
    executable = package / "voiceink-shell-smoke.exe"
    if not executable.is_file():
        raise WindowsReleasePackageError("relocated package is missing voiceink-shell-smoke.exe")
    environment = os.environ.copy()
    for name in (
        "VOICEINK_PACKAGE_ROOT",
        "VOICEINK_RUNTIME_MANIFEST",
        "VOICEINK_ARTIFACT_LOCK",
        "VOICEINK_ARTIFACT_LOCK_SHA256",
    ):
        environment.pop(name, None)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["VOICEINK_SIDECAR_STDERR"] = "inherit"
    environment["VOICEINK_SIDECAR_NO_WARMUP"] = "1"
    process = subprocess.Popen(
        [str(executable), "--smoke", "--package-smoke"],
        cwd=package,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        close_fds=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=SMOKE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        _terminate_process(process)
        cleanup_note = ""
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            _terminate_process(process)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stdout, stderr = "<process cleanup timed out>", "<process cleanup timed out>"
            cleanup_note = "\nProcess cleanup required a second termination."
        raise WindowsReleasePackageError(
            "relocated package smoke timed out\n"
            f"stdout:\n{_output_text(stdout or error.stdout)}\n"
            f"stderr:\n{_output_text(stderr or error.stderr)}{cleanup_note}"
        ) from error
    if process.returncode != 0:
        raise WindowsReleasePackageError(
            f"relocated package smoke failed with exit code {process.returncode}\n"
            f"stdout:\n{_output_text(stdout)}\n"
            f"stderr:\n{_output_text(stderr)}"
        )


def _stage_release_package(
    *,
    lock_path: Path,
    shell_dist: Path,
    staged_output: Path,
    ffmpeg: dict[str, object],
    sidecar: dict[str, object],
    model: dict[str, object],
) -> None:
    with tempfile.TemporaryDirectory(prefix="voiceink-release-artifacts-") as temporary:
        staging = Path(temporary)
        ffmpeg_archive = _download_verified(
            ffmpeg["url"], ffmpeg["archive_sha256"], staging / "ffmpeg.zip", "FFmpeg archive"
        )
        sidecar_archive = _download_verified(
            sidecar["url"], sidecar["archive_sha256"], staging / "sidecar.zip", "sidecar archive"
        )
        model_path = _download_verified(
            model["url"], model["sha256"], staging / "parakeet.gguf", "Parakeet model"
        )
        ffmpeg_path = _extract_verified_executable(
            ffmpeg_archive,
            staging / "ffmpeg",
            "ffmpeg.exe",
            ffmpeg["executable_sha256"],
            "FFmpeg",
        )
        sidecar_path = _extract_verified_executable(
            sidecar_archive,
            staging / "sidecar",
            "nemo-speech.exe",
            sidecar["executable_sha256"],
            "NeMo sidecar",
        )
        package = build_package(
            shell_dist=shell_dist,
            ffmpeg=ffmpeg_path,
            sidecar=sidecar_path,
            model=model_path,
            output=staging / "package",
            lock_template=lock_path,
        )
        with tempfile.TemporaryDirectory(
            prefix="voiceink release smoke ", dir=staged_output.parent
        ) as temporary:
            relocated = Path(temporary) / staged_output.name
            shutil.copytree(package, relocated)
            _run_relocation_smoke(relocated)
        shutil.move(str(package), staged_output)


def _remove_path(path: Path) -> OSError | None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError as error:
        return error
    return None


def build_release_package(
    *, lock_path: Path, shell_dist: Path, output: Path, bundle: Path, report: Path
) -> None:
    lock = load_native_smoke_lock(lock_path)
    artifacts = lock["artifacts"]
    assert isinstance(artifacts, dict)
    ffmpeg = artifacts["ffmpeg"]
    sidecar = artifacts[SIDECAR_ID]
    model = artifacts[MODEL_ID]
    assert isinstance(ffmpeg, dict)
    assert isinstance(sidecar, dict)
    assert isinstance(model, dict)
    if output.exists() or bundle.exists() or report.exists():
        raise WindowsReleasePackageError(
            "release output already exists; clean it before rebuilding"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    staged_output = output.parent / f".{output.name}.staging"
    if staged_output.exists():
        raise WindowsReleasePackageError("release staging output already exists; clean it first")

    try:
        _stage_release_package(
            lock_path=lock_path,
            shell_dist=shell_dist,
            staged_output=staged_output,
            ffmpeg=ffmpeg,
            sidecar=sidecar,
            model=model,
        )
        bundle.parent.mkdir(parents=True, exist_ok=True)
        report.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged_output), output)
        with tempfile.TemporaryDirectory(
            prefix="voiceink-release-publish-", dir=bundle.parent
        ) as temporary:
            temporary_root = Path(temporary)
            temporary_bundle = temporary_root / bundle.name
            produced_bundle = Path(
                shutil.make_archive(
                    str(temporary_bundle.with_suffix("")),
                    "zip",
                    root_dir=output.parent,
                    base_dir=output.name,
                )
            )
            if produced_bundle != temporary_bundle:
                raise WindowsReleasePackageError(
                    "release bundle was written to an unexpected path"
                ) from None
            shutil.move(str(produced_bundle), bundle)
            temporary_report = temporary_root / report.name
            temporary_report.write_text(
                json.dumps(
                    {
                        "schema": "voiceink.windows-release-package-smoke.v1",
                        "status": "passed",
                        "package": output.name,
                        "relocation_path_with_spaces": True,
                        "artifacts": {
                            "ffmpeg": {
                                "archive_sha256": ffmpeg["archive_sha256"],
                                "executable_sha256": ffmpeg["executable_sha256"],
                            },
                            SIDECAR_ID: {
                                "archive_sha256": sidecar["archive_sha256"],
                                "executable_sha256": sidecar["executable_sha256"],
                            },
                            MODEL_ID: {"sha256": model["sha256"]},
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            shutil.move(str(temporary_report), report)
    except BaseException as error:
        cleanup_errors = [
            cleanup_error
            for path in (staged_output, output, bundle, report)
            if (cleanup_error := _remove_path(path)) is not None
        ]
        if cleanup_errors:
            raise ExceptionGroup(
                "release publication and cleanup failed", [error, *cleanup_errors]
            ) from error
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--shell-dist", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        build_release_package(
            lock_path=arguments.lock,
            shell_dist=arguments.shell_dist,
            output=arguments.output,
            bundle=arguments.bundle,
            report=arguments.report,
        )
    except (OSError, TypeError, ValueError, WindowsReleasePackageError) as error:
        print(f"windows release package: {error}")
        return 1
    print(f"Windows release package ready: {arguments.bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
