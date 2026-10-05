"""Deterministic media adapters for application behavior tests."""

from __future__ import annotations

import hashlib
import shutil
import stat
import time
from enum import StrEnum
from pathlib import Path

from voiceink_win.domain import (
    Attempt,
    CancellationToken,
    InvalidSourceError,
    JobId,
    JobWorkspace,
    MalformedWavError,
    NoAudioStreamError,
    NormalizedAudio,
    ResourceLimitExceededError,
    ResourceLimitRejectedError,
    SnapshotManifest,
    SourceChangedError,
    SourceMedia,
    SourceSnapshot,
    UnsupportedMediaError,
)

from .ffmpeg import WavLimits, validate_wav


class FakeMediaScenario(StrEnum):
    SUCCESS = "success"
    NO_AUDIO = "no_audio"
    UNSUPPORTED = "unsupported"
    MALFORMED_WAV = "malformed_wav"
    RESOURCE_LIMIT = "resource_limit"
    CANCELLED = "cancelled"


def make_wav(pcm: bytes = b"\x00\x00" * 1600) -> bytes:
    fmt = (
        b"fmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + (16_000).to_bytes(4, "little")
        + (32_000).to_bytes(4, "little")
        + (2).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
    )
    data = b"data" + len(pcm).to_bytes(4, "little") + pcm
    body = b"WAVE" + fmt + data
    return b"RIFF" + len(body).to_bytes(4, "little") + body


class FakeMediaNormalizer:
    def __init__(
        self,
        scenario: FakeMediaScenario = FakeMediaScenario.SUCCESS,
        *,
        wav: bytes | None = None,
        limits: WavLimits | None = None,
    ) -> None:
        self.scenario = scenario
        self.wav = wav or make_wav()
        self.limits = limits or WavLimits()
        self.calls = 0
        self.sources: list[Path] = []

    def normalize(
        self,
        source: SourceSnapshot,
        workspace: JobWorkspace,
        cancellation: CancellationToken,
        deadline: float,
    ) -> NormalizedAudio:
        del workspace, deadline
        self.calls += 1
        self.sources.append(source.path)
        if cancellation.is_cancelled() or self.scenario is FakeMediaScenario.CANCELLED:
            raise RuntimeError("fake normalization cancelled")
        if self.scenario is FakeMediaScenario.NO_AUDIO:
            raise NoAudioStreamError("fake media has no audio")
        if self.scenario is FakeMediaScenario.UNSUPPORTED:
            raise UnsupportedMediaError("fake media is unsupported")
        if self.scenario is FakeMediaScenario.RESOURCE_LIMIT:
            raise ResourceLimitExceededError("fake media exceeds quota")
        if self.scenario is FakeMediaScenario.MALFORMED_WAV:
            raise MalformedWavError("fake normalizer returned malformed WAV")
        return validate_wav(self.wav, self.limits)


class FakeSnapshotStore:
    """OS-independent snapshot store for application behavior tests."""

    def __init__(self, root: Path, *, cleanup_error: bool = False) -> None:
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cleanup_error = cleanup_error
        self.workspace_creations = 0
        self.snapshots = 0
        self.cleanup_calls = 0
        self.cleanup_paths: list[Path] = []
        self.source_release_calls = 0

    @staticmethod
    def _describe(path: Path) -> tuple[str, int, str]:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise OSError("path is not a regular file")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return f"{info.st_dev}:{info.st_ino}", info.st_size, digest

    def validate_source(self, path: Path, *, max_bytes: int) -> SourceMedia:
        candidate = Path(path).absolute()
        try:
            identity, size, digest = self._describe(candidate)
        except OSError as error:
            raise InvalidSourceError("source is not a readable local file", cause=error) from error
        if size > max_bytes:
            raise ResourceLimitRejectedError("source exceeds the admission size limit")
        return SourceMedia(candidate, candidate.name, identity, size, digest)

    def create_workspace(self, job_id: JobId, attempt: int) -> JobWorkspace:
        self.workspace_creations += 1
        path = self.root / job_id.value / f"attempt-{attempt}"
        path.mkdir(parents=True, exist_ok=False)
        return JobWorkspace(
            path,
            job_id,
            Attempt(attempt),
            f"{path.parent.stat().st_dev}:{path.parent.stat().st_ino}",
            f"{path.stat().st_dev}:{path.stat().st_ino}",
        )

    def snapshot(
        self,
        source: SourceMedia,
        workspace: JobWorkspace,
        *,
        cancellation=None,
        deadline=None,
    ) -> SourceSnapshot:
        del cancellation, deadline
        self.snapshots += 1
        try:
            identity, size, digest = self._describe(source.path)
            if (identity, size, digest) != (source.identity, source.size, source.sha256):
                raise SourceChangedError("source changed after admission")
            snapshot_path = workspace.path / "source.snapshot"
            snapshot_path.write_bytes(source.path.read_bytes())
            after = self._describe(source.path)
            snapshot_identity, snapshot_size, snapshot_digest = self._describe(snapshot_path)
            if after != (identity, size, digest) or snapshot_digest != source.sha256:
                raise SourceChangedError("source changed during snapshot")
            return SourceSnapshot(
                source,
                snapshot_path,
                SnapshotManifest(
                    snapshot_identity,
                    snapshot_size,
                    snapshot_digest,
                    workspace.job_identity,
                    workspace.attempt_identity,
                ),
                source.identity,
                source.size,
                source.sha256,
            )
        except SourceChangedError:
            raise
        except OSError as error:
            raise SourceChangedError("source snapshot could not be created", cause=error) from error

    def verify(
        self, snapshot: SourceSnapshot, *, cancellation=None, deadline=None
    ) -> SourceSnapshot:
        del cancellation, deadline
        try:
            identity, size, digest = self._describe(snapshot.path)
            source_identity, source_size, source_digest = self._describe(snapshot.source.path)
        except OSError as error:
            raise SourceChangedError("snapshot could not be verified", cause=error) from error
        if (identity, size, digest) != (
            snapshot.manifest.snapshot_identity,
            snapshot.manifest.snapshot_size,
            snapshot.manifest.snapshot_sha256,
        ) or (source_identity, source_size, source_digest) != (
            snapshot.source_identity,
            snapshot.source_size,
            snapshot.source_sha256,
        ):
            raise SourceChangedError("immutable snapshot verification failed")
        return snapshot

    def release_snapshot(self, snapshot: SourceSnapshot) -> None:
        del snapshot

    def cleanup(self, workspace: JobWorkspace, *, deadline: float | None = None) -> None:
        del deadline
        self.cleanup_calls += 1
        self.cleanup_paths.append(workspace.path)
        if self.cleanup_error:
            raise OSError("simulated cleanup failure")
        if workspace.job_identity:
            current_job = workspace.path.parent.stat()
            if f"{current_job.st_dev}:{current_job.st_ino}" != workspace.job_identity:
                raise OSError("job workspace identity changed")
        if workspace.attempt_identity and workspace.path.exists():
            current_attempt = workspace.path.stat()
            if f"{current_attempt.st_dev}:{current_attempt.st_ino}" != workspace.attempt_identity:
                raise OSError("attempt workspace identity changed")
        if workspace.path.exists():
            shutil.rmtree(workspace.path)
        parent = workspace.path.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()

    def release_source(self, source: SourceMedia) -> None:
        del source
        self.source_release_calls += 1

    def sweep_orphans(self, *, max_age_seconds: float) -> int:
        cutoff = time.time() - max_age_seconds
        removed = 0
        for entry in tuple(self.root.iterdir()):
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
                removed += 1
        return removed
