"""Dependency-injection ports for imported media infrastructure."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .cancellation import CancellationToken
from .imported_models import (
    JobId,
    JobWorkspace,
    NormalizedAudio,
    SourceMedia,
    SourceSnapshot,
)


class MonotonicClock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class MediaSnapshotStore(Protocol):
    def validate_source(self, path: Path, *, max_bytes: int) -> SourceMedia: ...

    def snapshot(
        self,
        source: SourceMedia,
        workspace: JobWorkspace,
        *,
        cancellation: CancellationToken | None = None,
        deadline: float | None = None,
    ) -> SourceSnapshot: ...

    def verify(
        self,
        snapshot: SourceSnapshot,
        *,
        cancellation: CancellationToken | None = None,
        deadline: float | None = None,
    ) -> SourceSnapshot: ...

    def release_snapshot(self, snapshot: SourceSnapshot) -> None: ...

    def release_source(self, source: SourceMedia) -> None: ...

    def create_workspace(self, job_id: JobId, attempt: int) -> JobWorkspace: ...

    def cleanup(self, workspace: JobWorkspace, *, deadline: float | None = None) -> None: ...

    def sweep_orphans(self, *, max_age_seconds: float) -> int: ...

    def close(self, timeout: float = 1.0) -> None: ...


class MediaNormalizer(Protocol):
    def normalize(
        self,
        source: SourceSnapshot,
        workspace: JobWorkspace,
        cancellation: CancellationToken,
        deadline: float,
    ) -> NormalizedAudio: ...
