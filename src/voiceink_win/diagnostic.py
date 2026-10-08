"""Portable diagnostic runner for the first Windows vertical slice."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import sys
import tempfile
import time
import traceback
import uuid
from datetime import UTC, datetime
from inspect import signature
from pathlib import Path
from typing import Any

from voiceink_win.domain import JobId
from voiceink_win.infrastructure import (
    FfmpegArtifactManifest,
    SubprocessMediaNormalizer,
    VerifiedFfmpegArtifact,
    create_media_snapshot_store,
)

MAX_SOURCE_BYTES = 2 * 1024**3
_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9._-])(?:[A-Za-z]:[\\/]|\\\\|/)(?:[^\\/\s\"'<>|,:;)]*[\\/])*[^\\/\s\"'<>|,:;)]*"
)
_SENSITIVE_FIELDS = frozenset({"audio", "pcm", "transcript", "transcription", "raw_audio"})


class DiagnosticLogger:
    """Write structured and human-readable events without audio or transcript content."""

    def __init__(self, directory: Path, redactions: tuple[str, ...]) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self._redactions = tuple(value for value in redactions if value)
        self._jsonl = (directory / "diagnostic.jsonl").open("a", encoding="utf-8")
        self._text = (directory / "diagnostic.log").open("a", encoding="utf-8")
        self._logger = logging.getLogger(f"voiceink-diagnostic-{id(self)}")
        self._logger.setLevel(logging.INFO)
        handler = logging.FileHandler(directory / "python.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self._logger.addHandler(handler)

    def event(self, name: str, **fields: Any) -> None:
        payload = {
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "event": name,
            **self._sanitize_fields(fields),
        }
        line = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        self._jsonl.write(line + "\n")
        self._jsonl.flush()
        self._text.write(f"{payload['timestamp_utc']} {name} {line}\n")
        self._text.flush()
        self._logger.info("%s", line)

    def exception(self, name: str, error: BaseException) -> None:
        stack = "".join(
            f"  File {frame.filename}, line {frame.lineno}, in {frame.name}\n"
            for frame in traceback.extract_tb(error.__traceback__)
        )
        self.event(
            name,
            error_type=type(error).__name__,
            error_code=(
                getattr(error, "code", None)
                or getattr(error, "winerror", None)
                or getattr(error, "errno", None)
            ),
            traceback=stack,
        )

    def close(self) -> None:
        for stream in (self._jsonl, self._text):
            stream.flush()
            stream.close()
        for handler in self._logger.handlers:
            handler.close()
            self._logger.removeHandler(handler)

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, Path):
            return self._sanitize(str(value))
        if isinstance(value, dict):
            return {
                str(key): "<redacted>"
                if str(key).casefold() in _SENSITIVE_FIELDS
                else self._sanitize(item)
                for key, item in value.items()
            }
        if isinstance(value, list | tuple):
            return [self._sanitize(item) for item in value]
        if isinstance(value, str):
            result = value
            for redaction in self._redactions:
                result = result.replace(redaction, "<redacted>")
            return _ABSOLUTE_PATH.sub("<path>", result)
        return value

    def _sanitize_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        return {
            key: "<redacted>" if key.casefold() in _SENSITIVE_FIELDS else self._sanitize(value)
            for key, value in fields.items()
        }


class NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


def _default_log_directory() -> Path:
    root = os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share"
    return Path(root) / "VoiceInk-Win" / "diagnostics"


def _bundle_directory() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parents[2]


def _resolve_ffmpeg(argument: str | None) -> Path:
    candidates = []
    if argument:
        candidates.append(Path(argument))
    candidates.extend((_bundle_directory() / "ffmpeg.exe", _bundle_directory() / "ffmpeg"))
    discovered = shutil.which("ffmpeg")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("FFmpeg was not found; use --ffmpeg or place ffmpeg.exe beside the EXE")


def _load_manifest(
    path: Path | None, executable: Path, sha256: str | None
) -> FfmpegArtifactManifest:
    if path is not None:
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {}
    bundled_manifest = _bundle_directory() / "ffmpeg.manifest.json"
    if path is not None and sha256 is None and path.resolve() != bundled_manifest.resolve():
        raise ValueError("external FFmpeg manifests require --ffmpeg-sha256")
    digest = sha256 or str(data.get("sha256", ""))
    if not digest:
        raise ValueError("FFmpeg checksum is required through --ffmpeg-sha256 or manifest")
    return FfmpegArtifactManifest(
        version=str(data.get("version", "external")),
        provenance_url=str(data.get("provenance_url", "https://ffmpeg.org/")),
        sha256=digest,
        license=str(data.get("license", "GPL-3.0-or-later")),
        allowed_path=executable,
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="VoiceInk-Win Windows diagnostic runner")
    parser.add_argument("input", type=Path, help="local audio or video file to inspect")
    parser.add_argument("--ffmpeg", type=str, help="path to a verified ffmpeg executable")
    parser.add_argument("--ffmpeg-manifest", type=Path, help="JSON FFmpeg artifact manifest")
    parser.add_argument("--ffmpeg-sha256", type=str, help="expected FFmpeg SHA-256")
    parser.add_argument("--logs-dir", type=Path, help="directory for diagnostic log runs")
    return parser


def run_diagnostic(
    arguments: argparse.Namespace,
    *,
    store_factory=create_media_snapshot_store,
    normalizer_factory=SubprocessMediaNormalizer,
) -> int:
    # Keep the lexical path so the platform snapshot store can reject symlinks
    # and reparse points instead of validating an already-resolved target.
    source = Path(os.path.abspath(os.path.expanduser(str(arguments.input))))
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    log_directory = (arguments.logs_dir or _default_log_directory()) / run_id
    workspace_root = Path(tempfile.gettempdir()) / "voiceink-win-diagnostic"
    logger = DiagnosticLogger(
        log_directory,
        redactions=tuple(
            str(value)
            for value in (
                source,
                Path.home(),
                log_directory.parent,
                Path(tempfile.gettempdir()),
                workspace_root,
                arguments.input,
                getattr(arguments, "ffmpeg", None),
                getattr(arguments, "ffmpeg_manifest", None),
            )
            if value
        ),
    )
    store = None
    snapshot = None
    workspace = None
    source_descriptor = None
    pipeline_error: BaseException | None = None
    cleanup_errors: list[tuple[str, BaseException]] = []
    try:
        logger.event(
            "diagnostic.started",
            run_id=run_id,
            os=platform.platform(),
            machine=platform.machine(),
            processor=platform.processor(),
            python=platform.python_version(),
            executable_frozen=bool(getattr(sys, "frozen", False)),
            input_name=source.name,
        )
        if not source.is_file():
            raise FileNotFoundError("input must be an existing regular file")
        logger.event("source.stat", size=source.stat().st_size)
        executable = _resolve_ffmpeg(arguments.ffmpeg)
        manifest_path = arguments.ffmpeg_manifest
        if manifest_path is None:
            bundled_manifest = _bundle_directory() / "ffmpeg.manifest.json"
            manifest_path = bundled_manifest if bundled_manifest.is_file() else None
        manifest = _load_manifest(manifest_path, executable, arguments.ffmpeg_sha256)
        artifact = VerifiedFfmpegArtifact.verify(executable, manifest)
        logger.event(
            "ffmpeg.verified",
            version=manifest.version,
            sha256=manifest.sha256,
            license=manifest.license,
            provenance_url=manifest.provenance_url,
        )

        store = store_factory(workspace_root)
        source_descriptor = store.validate_source(source, max_bytes=MAX_SOURCE_BYTES)
        logger.event("source.validated", size=source_descriptor.size)
        job_id = JobId(f"diagnostic-{uuid.uuid4().hex}")
        workspace = store.create_workspace(job_id, 1)
        snapshot = store.snapshot(source_descriptor, workspace, cancellation=NeverCancelled())
        snapshot = store.verify(snapshot, cancellation=NeverCancelled())
        logger.event("snapshot.verified", snapshot_size=snapshot.manifest.snapshot_size)

        normalizer = normalizer_factory(executable=artifact)
        clock = getattr(normalizer, "clock", None)
        now = clock.monotonic if clock is not None else time.monotonic
        normalized = normalizer.normalize(
            snapshot,
            workspace,
            NeverCancelled(),
            deadline=now() + 30 * 60,
        )
        logger.event(
            "ffmpeg.normalized",
            sample_count=normalized.sample_count,
            duration_seconds=normalized.duration,
            pcm_bytes=normalized.audio.byte_length,
        )
        logger.event(
            "asr.not_configured",
            message="ASR runtime/model were not included in this diagnostic build",
        )
    except BaseException as error:
        pipeline_error = error
        if workspace is None:
            workspace = getattr(error, "partial_workspace", None)
        logger.exception("diagnostic.failed", error)
    finally:
        if store is not None and snapshot is not None:
            try:
                store.release_snapshot(snapshot)
            except BaseException as error:
                cleanup_errors.append(("snapshot.release", error))
                logger.exception("snapshot.release_failed", error)
        if store is not None and source_descriptor is not None:
            try:
                store.release_source(source_descriptor)
            except BaseException as error:
                cleanup_errors.append(("source.release", error))
                logger.exception("source.release_failed", error)
        if store is not None and workspace is not None:
            try:
                store.cleanup(workspace)
            except BaseException as error:
                cleanup_errors.append(("workspace.cleanup", error))
                logger.exception("workspace.cleanup_failed", error)
            if workspace.path.exists():
                error = RuntimeError("diagnostic workspace still exists after cleanup")
                cleanup_errors.append(("workspace.verify", error))
                logger.exception("workspace.cleanup_unconfirmed", error)
        if store is not None:
            close = getattr(store, "close", None)
            if close is not None:
                try:
                    parameters = signature(close).parameters
                    if "timeout" in parameters or any(
                        parameter.kind is parameter.VAR_KEYWORD for parameter in parameters.values()
                    ):
                        close_result = close(timeout=5.0)
                    else:
                        close_result = close()
                    if close_result is False:
                        raise RuntimeError("diagnostic store close was not confirmed")
                except BaseException as error:
                    cleanup_errors.append(("store.close", error))
                    logger.exception("store.close_failed", error)
            if not _store_recovery_complete(store):
                error = RuntimeError("diagnostic store recovery is not complete")
                cleanup_errors.append(("store.recovery", error))
                logger.exception("store.recovery_unconfirmed", error)
        if pipeline_error is None and not cleanup_errors:
            logger.event("diagnostic.succeeded", log_directory=log_directory)
        elif cleanup_errors:
            logger.event(
                "diagnostic.cleanup_failed",
                errors=[
                    {"operation": operation, "error_type": type(error).__name__}
                    for operation, error in cleanup_errors
                ],
            )
        try:
            logger.event("diagnostic.logs", log_directory=log_directory)
        finally:
            logger.close()
    return 0 if pipeline_error is None and not cleanup_errors else 1


def _store_recovery_complete(store: object) -> bool:
    """Treat successful close plus empty native recovery state as confirmation."""
    confirmed = getattr(store, "recovery_complete", None)
    if confirmed is not None:
        if callable(confirmed):
            confirmed = confirmed()
        if not confirmed:
            return False
    for name in ("recovery", "_lease_recovery", "_failed_close_leases", "_active_leases"):
        value = getattr(store, name, None)
        if callable(value):
            value = value()
        if value:
            return False
    for name in ("_reapers",):
        value = getattr(store, name, ())
        if any(thread.is_alive() for thread in value):
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    return run_diagnostic(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
