"""Handle-based source snapshots and fail-closed private workspace cleanup."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import stat
import time
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from threading import Lock
from typing import Protocol

from voiceink_win.domain import (
    AsrTimeoutError,
    Attempt,
    CancellationError,
    CancellationToken,
    InvalidSourceError,
    JobId,
    JobWorkspace,
    MonotonicClock,
    ResourceLimitExceededError,
    ResourceLimitRejectedError,
    SnapshotManifest,
    SourceChangedError,
    SourceMedia,
    SourceSnapshot,
    VerifiedMediaHandle,
)

from .clock import SystemMonotonicClock

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses the native snapshot store
    fcntl = None

SOURCE_LIMIT = 2 * 1024**3
WORKSPACE_LIMIT = 768 * 1024**2
WORKSPACE_ROOT_MARKER = ".voiceink-owned-root"
WORKSPACE_ACTIVE_LOCK = ".voiceink.active.lock"
ATTEMPT_QUARANTINE_PREFIX = ".voiceink-attempt-quarantine-"
QUARANTINE_METADATA = ".voiceink-quarantine.json"


def _prepare_workspace_root(root: Path) -> Path:
    requested_root = Path(root).absolute()
    root_existed = requested_root.exists()
    LocalMediaSnapshotStore._reject_symlink_components(requested_root)
    requested_root.mkdir(parents=True, exist_ok=True)
    LocalMediaSnapshotStore._reject_symlink_components(requested_root)
    marker = requested_root / WORKSPACE_ROOT_MARKER
    if marker.exists() or marker.is_symlink():
        if marker.is_symlink() or not marker.is_dir():
            raise OSError("workspace root ownership marker is invalid")
    elif not root_existed:
        marker.mkdir(mode=0o700)
    return requested_root.resolve(strict=True)


def _validate_workspace_root(root: Path) -> None:
    marker = root / WORKSPACE_ROOT_MARKER
    if marker.is_symlink() or not marker.is_dir():
        raise OSError("workspace root ownership marker is invalid")


def _check_bool(result, function, arguments):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def _check_handle(result, function, arguments):
    value = getattr(result, "value", result)
    if value in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    return result


class WindowsMediaSecurityAdapter(Protocol):
    def validate_source(self, path: Path) -> None: ...

    def cleanup_workspace(self, path: Path) -> None: ...


class WindowsAdapterRequiredError(RuntimeError):
    """Raised instead of silently claiming Windows reparse-point safety."""


class _WindowsFileTime(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class _WindowsFileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation", _WindowsFileTime),
        ("access", _WindowsFileTime),
        ("write", _WindowsFileTime),
        ("volume_serial", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


class NativeWindowsMediaSecurityAdapter:
    """Use Win32 handles to reject network/reparse paths before file I/O."""

    _GENERIC_READ = 0x80000000
    _DELETE = 0x00010000
    _FILE_SHARE_READ = 0x1
    _FILE_SHARE_WRITE = 0x2
    _FILE_SHARE_DELETE = 0x4
    _OPEN_EXISTING = 3
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_DISPOSITION_INFO_EX = 64
    _FILE_DISPOSITION_FLAG_DELETE = 0x1
    _FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE = 0x10
    _FILE_TYPE_DISK = 0x1
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsAdapterRequiredError("native Windows adapter requires Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.CreateFileW.errcheck = _check_handle
        self._kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_WindowsFileInformation),
        ]
        self._kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        self._kernel32.GetFileInformationByHandle.errcheck = _check_bool
        self._kernel32.GetFileType.argtypes = [wintypes.HANDLE]
        self._kernel32.GetFileType.restype = wintypes.DWORD
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.CloseHandle.errcheck = _check_bool
        self._kernel32.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            wintypes.INT,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._kernel32.SetFileInformationByHandle.restype = wintypes.BOOL

    def validate_source(self, path: Path) -> None:
        path_text = str(path)
        if not path.is_absolute() or self._is_unc(path_text):
            raise InvalidSourceError("network and relative paths are not permitted")
        self._assert_no_reparse_components(path)
        handle = self._open(path, is_directory=False)
        self._close_after_validation(handle, path)

    def cleanup_workspace(self, path: Path) -> None:
        if not path.is_absolute() or self._is_unc(str(path)):
            raise WindowsAdapterRequiredError("workspace path is not local and absolute")
        self._assert_no_reparse_components(path)
        self._assert_directory(path)
        for child in path.iterdir():
            handle = self._open(child, is_directory=None, allow_reparse=True)
            try:
                info = _WindowsFileInformation()
                self._kernel32.GetFileInformationByHandle(handle, ctypes.byref(info))
                is_directory = bool(info.attributes & self._FILE_ATTRIBUTE_DIRECTORY)
                is_reparse = bool(info.attributes & self._FILE_ATTRIBUTE_REPARSE_POINT)
                if is_reparse or not is_directory:
                    self._delete_handle(handle)
                    continue
            finally:
                self._close(handle)
            if is_directory:
                self.cleanup_workspace(child)
                handle = self._open(child, is_directory=True)
                try:
                    self._delete_handle(handle)
                finally:
                    self._close(handle)

    def _open(
        self,
        path: Path,
        *,
        is_directory: bool | None,
        allow_reparse: bool = False,
        access: int | None = None,
    ) -> int:
        flags = self._FILE_FLAG_OPEN_REPARSE_POINT
        if is_directory is not False:
            flags |= self._FILE_FLAG_BACKUP_SEMANTICS
        handle = self._kernel32.CreateFileW(
            str(path),
            self._GENERIC_READ | self._DELETE if access is None else access,
            self._FILE_SHARE_READ | self._FILE_SHARE_WRITE | self._FILE_SHARE_DELETE,
            None,
            self._OPEN_EXISTING,
            flags,
            None,
        )
        if handle in (None, self._INVALID_HANDLE_VALUE):
            raise InvalidSourceError(
                "Windows handle open failed", cause=OSError(ctypes.get_last_error())
            )
        info = _WindowsFileInformation()
        try:
            self._kernel32.GetFileInformationByHandle(handle, ctypes.byref(info))
        except BaseException as error:
            self._close(handle)
            raise InvalidSourceError("Windows file identity lookup failed", cause=error) from error
        if info.attributes & self._FILE_ATTRIBUTE_REPARSE_POINT and not allow_reparse:
            self._close(handle)
            raise InvalidSourceError("reparse points are not permitted")
        if is_directory is True and not info.attributes & self._FILE_ATTRIBUTE_DIRECTORY:
            self._close(handle)
            raise InvalidSourceError("workspace is not a directory")
        if is_directory is False and (
            info.attributes & self._FILE_ATTRIBUTE_DIRECTORY
            or self._kernel32.GetFileType(handle) != self._FILE_TYPE_DISK
        ):
            self._close(handle)
            raise InvalidSourceError("source is not a regular file")
        return int(handle.value)

    def _assert_directory(self, path: Path) -> None:
        handle = self._open(path, is_directory=True)
        self._close(handle)

    def _close_after_validation(self, handle: int, path: Path) -> None:
        del path
        self._close(handle)

    def _close(self, handle: int) -> None:
        from .windows_snapshot import NativeHandleLeaseRegistry

        registry = NativeHandleLeaseRegistry()
        registry.register_handle(handle, self._kernel32.CloseHandle)
        try:
            registry.close()
        except BaseException as error:
            raise WindowsAdapterRequiredError("Windows handle close failed", cause=error) from error

    def _assert_no_reparse_components(self, path: Path) -> None:
        current = Path(path.anchor)
        components = [current]
        for part in path.parts[1:]:
            current /= part
            components.append(current)
        for component in components:
            handle = self._open(component, is_directory=None, access=self._GENERIC_READ)
            self._close(handle)

    @staticmethod
    def _is_unc(path: str) -> bool:
        folded = path.casefold()
        return folded.startswith("\\\\?\\unc\\") or (
            folded.startswith("\\\\") and not folded.startswith("\\\\?\\")
        )

    def _delete_handle(self, handle: int) -> None:
        disposition = ctypes.c_uint32(
            self._FILE_DISPOSITION_FLAG_DELETE
            | self._FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE
        )
        if not self._kernel32.SetFileInformationByHandle(
            handle,
            self._FILE_DISPOSITION_INFO_EX,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise WindowsAdapterRequiredError("Windows handle delete failed")


def _identity(info: os.stat_result) -> str:
    return f"{info.st_dev}:{info.st_ino}"


def _check_lifecycle(
    cancellation: CancellationToken | None,
    deadline: float | None,
    clock: MonotonicClock | None,
) -> None:
    if cancellation is not None and cancellation.is_cancelled():
        raise CancellationError("media snapshot operation was cancelled")
    if deadline is not None and clock.monotonic() >= deadline:
        raise AsrTimeoutError("media snapshot operation exceeded its deadline")


class _DescriptorLease:
    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor
        self._closed = False
        self._lock = Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                os.close(self._descriptor)
            except OSError:
                pass


def _hash_fd(
    fd: int,
    *,
    max_bytes: int | None = None,
    cancellation: CancellationToken | None = None,
    deadline: float | None = None,
    clock: MonotonicClock | None = None,
) -> tuple[str, int]:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while True:
        if deadline is not None and clock is None:
            raise ValueError("a monotonic clock is required with a deadline")
        _check_lifecycle(cancellation, deadline, clock)
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise ResourceLimitExceededError("source exceeds the configured size limit")
        digest.update(chunk)
    return digest.hexdigest(), total


def _write_all(
    fd: int,
    data: bytes,
    *,
    cancellation: CancellationToken | None = None,
    deadline: float | None = None,
    clock: MonotonicClock | None = None,
) -> None:
    view = memoryview(data)
    while view:
        if deadline is not None and clock is None:
            raise ValueError("a monotonic clock is required with a deadline")
        _check_lifecycle(cancellation, deadline, clock)
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("snapshot write made no progress")
        view = view[written:]


class LocalMediaSnapshotStore:
    """POSIX implementation using openat-style dirfd traversal where available."""

    def __init__(
        self,
        root: Path,
        *,
        import_roots: tuple[Path, ...] = (),
        windows_adapter: WindowsMediaSecurityAdapter | None = None,
        max_workspace_bytes: int = WORKSPACE_LIMIT,
        max_snapshot_bytes: int = SOURCE_LIMIT,
        clock: object | None = None,
    ) -> None:
        if os.name == "nt":
            raise WindowsAdapterRequiredError(
                "the POSIX snapshot backend cannot run on Windows; install the native backend"
            )
        if max_workspace_bytes < 1:
            raise ValueError("workspace quota must be positive")
        if max_snapshot_bytes < 1:
            raise ValueError("snapshot quota must be positive")
        self.root = _prepare_workspace_root(root)
        marker = self.root / WORKSPACE_ROOT_MARKER
        if not marker.exists():
            if not self._contains_only_owned_workspaces():
                raise OSError("workspace root ownership cannot be established safely")
            marker.mkdir(mode=0o700)
        resolved_import_roots = []
        for item in import_roots:
            import_root = Path(item).absolute()
            self._reject_symlink_components(import_root)
            resolved_import_roots.append(import_root.resolve(strict=True))
        self.import_roots = tuple(resolved_import_roots)
        self.windows_adapter = windows_adapter
        self.max_workspace_bytes = max_workspace_bytes
        self.max_snapshot_bytes = max_snapshot_bytes
        self._clock: MonotonicClock = clock or SystemMonotonicClock()
        self._usage_lock = Lock()
        self._workspace_reserved, self._snapshot_reserved = self._scan_workspace_usage()

    def validate_source(self, path: Path, *, max_bytes: int = SOURCE_LIMIT) -> SourceMedia:
        candidate = Path(path).absolute()
        self._reject_symlink_components(candidate)
        try:
            candidate_info = os.lstat(candidate)
        except OSError as error:
            raise InvalidSourceError("source is not a readable local file", cause=error) from error
        if not stat.S_ISREG(candidate_info.st_mode):
            raise InvalidSourceError("source is not a regular file")
        try:
            candidate = candidate.resolve(strict=True)
        except OSError as error:
            raise InvalidSourceError("source is not a readable local file", cause=error) from error
        if self.windows_adapter is not None:
            self.windows_adapter.validate_source(candidate)
        if str(candidate).startswith("\\\\"):
            raise InvalidSourceError("network paths are not permitted")
        if self.import_roots and not any(
            self._contained(candidate, root) for root in self.import_roots
        ):
            raise InvalidSourceError("source is outside the configured import roots")
        try:
            fd = self._open_absolute(candidate, os.O_RDONLY)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise InvalidSourceError("source is not a regular file")
                digest, size = _hash_fd(fd, max_bytes=max_bytes, clock=self._clock)
                after = os.fstat(fd)
            finally:
                os.close(fd)
        except ResourceLimitExceededError:
            raise ResourceLimitRejectedError("source exceeds the admission size limit") from None
        except InvalidSourceError:
            raise
        except OSError as error:
            raise InvalidSourceError("source is not a readable local file", cause=error) from error
        if (_identity(info), info.st_size) != (
            _identity(after),
            after.st_size,
        ) or size != after.st_size:
            raise InvalidSourceError("source changed during admission")
        return SourceMedia(candidate, candidate.name, _identity(info), size, digest)

    def create_workspace(self, job_id: JobId, attempt: int) -> JobWorkspace:
        path = self.root / job_id.value / f"attempt-{attempt}"
        root_fd = self._open_dir(self.root)
        job_fd = -1
        attempt_fd = -1
        job_lock_fd = -1
        job_identity = ""
        partial_workspace = JobWorkspace(path, job_id, Attempt(attempt))
        try:
            try:
                os.mkdir(job_id.value, dir_fd=root_fd)
            except FileExistsError:
                pass
            job_fd = self._open_child_dir(root_fd, job_id.value)
            job_lock_fd = self._open_workspace_lock(job_fd)
            self._lock_directory(job_lock_fd)
            if not self._job_directory_is_empty(job_fd):
                raise OSError("job workspace already contains an active attempt")
            job_identity = _identity(os.fstat(job_fd))
            partial_workspace = JobWorkspace(path, job_id, Attempt(attempt), job_identity)
            os.mkdir(f"attempt-{attempt}", dir_fd=job_fd)
            attempt_fd = self._open_child_dir(job_fd, f"attempt-{attempt}")
            attempt_identity = _identity(os.fstat(attempt_fd))
        except Exception as error:
            if attempt_fd >= 0:
                os.close(attempt_fd)
                attempt_fd = -1
            if job_lock_fd >= 0:
                self._unlock_directory(job_lock_fd)
                os.close(job_lock_fd)
                job_lock_fd = -1
            if job_fd >= 0:
                os.close(job_fd)
                job_fd = -1
            if job_identity:
                error.partial_workspace = partial_workspace
            raise
        finally:
            if attempt_fd >= 0:
                os.close(attempt_fd)
            if job_lock_fd >= 0:
                self._unlock_directory(job_lock_fd)
                os.close(job_lock_fd)
            if job_fd >= 0:
                os.close(job_fd)
            os.close(root_fd)
        workspace = JobWorkspace(path, job_id, Attempt(attempt), job_identity, attempt_identity)
        try:
            with self._workspace_lock(path):
                self._write_manifest(
                    path,
                    {
                        "job_id": job_id.value,
                        "attempt": attempt,
                        "job_identity": job_identity,
                        "attempt_identity": attempt_identity,
                    },
                )
        except Exception as error:
            self._release_workspace(path)
            try:
                self.cleanup(workspace)
            except BaseException as cleanup_error:
                error.partial_workspace = workspace
                error.add_note(f"workspace rollback failed: {cleanup_error}")
            raise
        return workspace

    def snapshot(
        self,
        source: SourceMedia,
        workspace: JobWorkspace,
        *,
        cancellation: CancellationToken | None = None,
        deadline: float | None = None,
    ) -> SourceSnapshot:
        with self._workspace_lock(workspace.path):
            return self._snapshot_locked(
                source,
                workspace,
                cancellation=cancellation,
                deadline=deadline,
            )

    def _snapshot_locked(
        self,
        source: SourceMedia,
        workspace: JobWorkspace,
        *,
        cancellation: CancellationToken | None = None,
        deadline: float | None = None,
    ) -> SourceSnapshot:
        self._reject_symlink_components(source.path)
        source_fd = -1
        workspace_fd = -1
        destination_fd = -1
        reserved = False
        try:
            source_fd = self._open_absolute(source.path, os.O_RDONLY)
            workspace_fd = self._open_workspace_fd(workspace)
            self._assert_workspace_identity(workspace)
            self._reserve_snapshot(workspace.path, source.size)
            reserved = True
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise SourceChangedError("source is not a regular file")
            if source.identity and (_identity(before), before.st_size) != (
                source.identity,
                source.size,
            ):
                raise SourceChangedError("source changed after admission")
            destination_fd = os.open(
                "source.snapshot",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o400,
                dir_fd=workspace_fd,
            )
            digest = hashlib.sha256()
            total = 0
            while True:
                _check_lifecycle(cancellation, deadline, self._clock)
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_snapshot_bytes or total > source.size:
                    raise ResourceLimitExceededError("source exceeds the snapshot quota")
                _write_all(
                    destination_fd,
                    chunk,
                    cancellation=cancellation,
                    deadline=deadline,
                    clock=self._clock,
                )
                digest.update(chunk)
            os.fchmod(destination_fd, 0o400)
            after = os.fstat(source_fd)
            source_digest, source_size = _hash_fd(
                source_fd,
                max_bytes=self.max_snapshot_bytes,
                cancellation=cancellation,
                deadline=deadline,
                clock=self._clock,
            )
            if (
                (_identity(before), before.st_size) != (_identity(after), after.st_size)
                or source_size != total
                or source_digest != digest.hexdigest()
                or source_digest != source.sha256
            ):
                raise SourceChangedError("source changed during snapshot")
            snapshot_info = os.fstat(destination_fd)
            manifest = SnapshotManifest(
                _identity(snapshot_info),
                snapshot_info.st_size,
                digest.hexdigest(),
                workspace.job_identity,
                workspace.attempt_identity,
            )
        except OSError as error:
            if reserved:
                self._release_snapshot(workspace.path, source.size)
            raise SourceChangedError("source snapshot could not be created", cause=error) from error
        except Exception:
            if reserved:
                self._release_snapshot(workspace.path, source.size)
            raise
        finally:
            if destination_fd >= 0:
                os.close(destination_fd)
            if workspace_fd >= 0:
                os.close(workspace_fd)
            if source_fd >= 0:
                os.close(source_fd)
        self._write_manifest(
            workspace.path,
            {
                "job_id": workspace.job_id.value,
                "attempt": workspace.attempt.value,
                "snapshot_identity": manifest.snapshot_identity,
                "snapshot_size": manifest.snapshot_size,
                "snapshot_sha256": manifest.snapshot_sha256,
                "job_identity": manifest.job_identity,
                "attempt_identity": manifest.attempt_identity,
            },
        )
        return SourceSnapshot(
            source,
            workspace.path / "source.snapshot",
            manifest,
            source.identity,
            source.size,
            source.sha256,
        )

    def verify(
        self,
        snapshot: SourceSnapshot,
        *,
        cancellation: CancellationToken | None = None,
        deadline: float | None = None,
    ) -> SourceSnapshot:
        _check_lifecycle(cancellation, deadline, self._clock)
        workspace_fd = self._open_workspace_fd_from_path(snapshot.path.parent)
        snapshot_fd = -1
        job_fd = -1
        try:
            workspace_info = os.fstat(workspace_fd)
            if (
                snapshot.manifest.attempt_identity
                and _identity(workspace_info) != snapshot.manifest.attempt_identity
            ):
                raise SourceChangedError("workspace identity changed before normalization")
            root_fd = self._open_dir(self.root)
            try:
                job_fd = self._open_child_dir(root_fd, snapshot.path.parent.parent.name)
                if (
                    snapshot.manifest.job_identity
                    and _identity(os.fstat(job_fd)) != snapshot.manifest.job_identity
                ):
                    raise SourceChangedError("job workspace identity changed before normalization")
            finally:
                os.close(root_fd)
            snapshot_fd = os.open(
                snapshot.path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=workspace_fd,
            )
            info = os.fstat(snapshot_fd)
            actual_hash, size = _hash_fd(
                snapshot_fd,
                max_bytes=self.max_snapshot_bytes,
                cancellation=cancellation,
                deadline=deadline,
                clock=self._clock,
            )
            if (
                _identity(info) != snapshot.manifest.snapshot_identity
                or size != snapshot.manifest.snapshot_size
                or actual_hash != snapshot.manifest.snapshot_sha256
            ):
                raise SourceChangedError("immutable snapshot verification failed")
            values = self._read_manifest(snapshot.path.parent)
            try:
                manifest = SnapshotManifest(
                    values["snapshot_identity"],
                    values["snapshot_size"],
                    values["snapshot_sha256"],
                    values.get("job_identity", ""),
                    values.get("attempt_identity", ""),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise SourceChangedError("snapshot manifest is invalid", cause=error) from error
            if manifest != snapshot.manifest:
                raise SourceChangedError("snapshot manifest does not match the snapshot")
            os.lseek(snapshot_fd, 0, os.SEEK_SET)
            self._reject_symlink_components(snapshot.source.path)
            source_fd = self._open_absolute(snapshot.source.path, os.O_RDONLY)
            try:
                source_info = os.fstat(source_fd)
                source_hash, source_size = _hash_fd(
                    source_fd,
                    max_bytes=self.max_snapshot_bytes,
                    cancellation=cancellation,
                    deadline=deadline,
                    clock=self._clock,
                )
            finally:
                os.close(source_fd)
            if (
                _identity(source_info) != snapshot.source_identity
                or source_size != snapshot.source_size
                or source_hash != snapshot.source_sha256
            ):
                raise SourceChangedError("source changed before normalization")
            return SourceSnapshot(
                snapshot.source,
                snapshot.path,
                snapshot.manifest,
                snapshot.source_identity,
                snapshot.source_size,
                snapshot.source_sha256,
                VerifiedMediaHandle(
                    f"/dev/fd/{snapshot_fd}",
                    snapshot_fd,
                    snapshot.manifest.snapshot_identity,
                    snapshot.manifest.snapshot_size,
                    snapshot.manifest.snapshot_sha256,
                    _DescriptorLease(snapshot_fd),
                ),
                snapshot_fd,
            )
        except OSError as error:
            if snapshot_fd >= 0:
                os.close(snapshot_fd)
            raise SourceChangedError("snapshot could not be verified", cause=error) from error
        except Exception:
            if snapshot_fd >= 0:
                os.close(snapshot_fd)
            raise
        finally:
            if job_fd >= 0:
                os.close(job_fd)
            os.close(workspace_fd)

    def release_snapshot(self, snapshot: SourceSnapshot) -> None:
        if snapshot.descriptor is not None:
            lease = snapshot.verified_input.lease if snapshot.verified_input else None
            if isinstance(lease, _DescriptorLease):
                lease.close()
                return
            try:
                os.close(snapshot.descriptor)
            except OSError:
                pass

    def cleanup(self, workspace: JobWorkspace, *, deadline: float | None = None) -> None:
        if not workspace.path.is_dir():
            if workspace.path.parent.is_dir():
                with self.workspace_lock(workspace):
                    with self._workspace_lock(workspace.path):
                        return self._cleanup_unlocked(workspace, deadline=deadline)
            return self._cleanup_unlocked(workspace, deadline=deadline)
        with self.workspace_lock(workspace):
            with self._workspace_lock(workspace.path):
                return self._cleanup_unlocked(workspace, deadline=deadline)

    def _cleanup_unlocked(self, workspace: JobWorkspace, *, deadline: float | None = None) -> None:
        try:
            self._cleanup_contents(workspace, deadline=deadline)
        finally:
            try:
                if not workspace.path.exists():
                    self._release_workspace(workspace.path)
                    self._release_snapshot(workspace.path)
            except OSError:
                pass

    def _cleanup_contents(self, workspace: JobWorkspace, *, deadline: float | None = None) -> None:
        self._check_cleanup_deadline(deadline)
        if self.windows_adapter is not None:
            self.windows_adapter.cleanup_workspace(workspace.path)
            return
        expected_path = self.root / workspace.job_id.value / f"attempt-{workspace.attempt.value}"
        if workspace.path != expected_path:
            raise OSError("workspace path does not match its owner")
        if not self._contained(workspace.path, self.root):
            raise OSError("workspace containment cannot be proven")
        root_fd = self._open_dir(self.root)
        job_fd = -1
        attempt_fd = -1
        attempt_quarantine: str | None = None
        try:
            job_fd = self._open_child_dir(root_fd, workspace.job_id.value)
            if workspace.job_identity and _identity(os.fstat(job_fd)) != workspace.job_identity:
                raise OSError("job workspace identity changed")
            try:
                attempt_fd = self._open_child_dir(job_fd, f"attempt-{workspace.attempt.value}")
            except FileNotFoundError:
                attempt_quarantine = self._find_owned_attempt_quarantine(
                    job_fd, workspace.attempt_identity
                )
                if attempt_quarantine is not None:
                    attempt_fd = self._open_child_dir(job_fd, attempt_quarantine)
                elif workspace.attempt_identity:
                    raise
                else:
                    self._remove_empty_job_files_locked(
                        root_fd, workspace.job_id.value, job_fd, workspace.job_identity
                    )
                    return
            if (
                workspace.attempt_identity
                and _identity(os.fstat(attempt_fd)) != workspace.attempt_identity
            ):
                raise OSError("attempt workspace identity changed")
            if attempt_quarantine is None:
                attempt_quarantine = self._quarantine_attempt_locked(
                    job_fd,
                    workspace.job_id.value,
                    f"attempt-{workspace.attempt.value}",
                    attempt_fd,
                    workspace.attempt_identity,
                )
            os.close(attempt_fd)
            attempt_fd = -1
            self._remove_quarantine_directory_locked(
                job_fd,
                attempt_quarantine,
                expected_identity=workspace.attempt_identity,
            )
            self._remove_empty_job_files_locked(
                root_fd, workspace.job_id.value, job_fd, workspace.job_identity
            )
        finally:
            if attempt_fd >= 0:
                os.close(attempt_fd)
            if job_fd >= 0:
                os.close(job_fd)
            os.close(root_fd)

    def sweep_orphans(self, *, max_age_seconds: float) -> int:
        _validate_workspace_root(self.root)
        root_fd = self._open_dir(self.root)
        removed = 0
        try:
            for entry in os.scandir(root_fd):
                if entry.name == WORKSPACE_ROOT_MARKER or entry.name.startswith("."):
                    continue
                info = os.stat(entry.name, dir_fd=root_fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    continue
                if time.time() - info.st_mtime <= max_age_seconds:
                    continue
                if not self._read_only_orphan_candidate(
                    entry.name,
                    self.root / entry.name,
                    job_mtime=info.st_mtime,
                    max_age_seconds=max_age_seconds,
                ):
                    continue
                job_fd = self._open_child_dir(root_fd, entry.name)
                active_lock_fd = self._try_workspace_lock_file(job_fd, WORKSPACE_ACTIVE_LOCK)
                if active_lock_fd < 0:
                    os.close(job_fd)
                    continue
                job_lock_fd = self._try_workspace_lock(job_fd)
                if job_lock_fd < 0:
                    self._unlock_directory(active_lock_fd)
                    os.close(active_lock_fd)
                    os.close(job_fd)
                    continue
                removed_from_job = False
                try:
                    for attempt in tuple(os.scandir(job_fd)):
                        if not attempt.is_dir(follow_symlinks=False):
                            continue
                        attempt_path = self.root / entry.name / attempt.name
                        original_attempt_path = attempt_path
                        ownership = (
                            self._owned_workspace(
                                entry.name,
                                attempt.name,
                                attempt_path,
                                job_mtime=info.st_mtime,
                            )
                            if attempt.name.startswith("attempt-")
                            else self._owned_attempt_quarantine(
                                entry.name,
                                attempt.name,
                                attempt_path,
                                job_mtime=info.st_mtime,
                            )
                        )
                        if ownership is None:
                            continue
                        attempt_identity, attempt_mtime = ownership[:2]
                        if not attempt.name.startswith("attempt-"):
                            original_attempt_path = (
                                self.root / entry.name / f"attempt-{ownership[2]}"
                            )
                        if time.time() - attempt_mtime <= max_age_seconds:
                            continue
                        if attempt.name.startswith(ATTEMPT_QUARANTINE_PREFIX):
                            self._remove_quarantine_directory_locked(
                                job_fd,
                                attempt.name,
                                expected_identity=attempt_identity,
                            )
                        else:
                            attempt_fd = self._open_child_dir(job_fd, attempt.name)
                            try:
                                quarantine_name = self._quarantine_attempt_locked(
                                    job_fd,
                                    entry.name,
                                    attempt.name,
                                    attempt_fd,
                                    attempt_identity,
                                )
                            finally:
                                os.close(attempt_fd)
                            self._remove_quarantine_directory_locked(
                                job_fd,
                                quarantine_name,
                                expected_identity=attempt_identity,
                            )
                        self._release_workspace_tree(original_attempt_path)
                        removed += 1
                        removed_from_job = True
                    if removed_from_job:
                        self._remove_empty_job_files_locked(
                            root_fd, entry.name, job_fd, _identity(os.fstat(job_fd))
                        )
                finally:
                    self._unlock_directory(active_lock_fd)
                    os.close(active_lock_fd)
                    self._unlock_directory(job_lock_fd)
                    os.close(job_lock_fd)
                    os.close(job_fd)
        finally:
            os.close(root_fd)
        return removed

    def _read_only_orphan_candidate(
        self,
        job_name: str,
        job_path: Path,
        *,
        job_mtime: float,
        max_age_seconds: float,
    ) -> bool:
        try:
            with os.scandir(job_path) as entries:
                children = tuple(entries)
        except OSError:
            return False
        owned_orphan = False
        for child in children:
            if child.name in {".voiceink.lock", WORKSPACE_ACTIVE_LOCK}:
                if not child.is_file(follow_symlinks=False):
                    return False
                continue
            child_path = Path(child.path)
            if child.name.startswith("attempt-"):
                if not child.is_dir(follow_symlinks=False):
                    return False
                ownership = self._owned_workspace(
                    job_name,
                    child.name,
                    child_path,
                    job_mtime=job_mtime,
                )
            elif child.name.startswith(ATTEMPT_QUARANTINE_PREFIX):
                if not child.is_dir(follow_symlinks=False):
                    return False
                ownership = self._owned_attempt_quarantine(
                    job_name,
                    child.name,
                    child_path,
                    job_mtime=job_mtime,
                )
            else:
                return False
            if ownership is not None and time.time() - ownership[1] > max_age_seconds:
                owned_orphan = True
        return owned_orphan

    def _is_owned_workspace(self, job_name: str, attempt_name: str, path: Path) -> bool:
        return self._owned_workspace(job_name, attempt_name, path) is not None

    def _owned_workspace(
        self,
        job_name: str,
        attempt_name: str,
        path: Path,
        *,
        job_mtime: float | None = None,
    ) -> tuple[str, float] | None:
        if not attempt_name.startswith("attempt-"):
            return None
        try:
            attempt = int(attempt_name.removeprefix("attempt-"))
            values = self._read_manifest(path)
        except (OSError, ValueError, TypeError, KeyError):
            return None
        if not (
            values.get("job_id") == job_name
            and type(values.get("attempt")) is int
            and values["attempt"] == attempt
            and isinstance(values.get("job_identity"), str)
            and bool(values["job_identity"])
            and isinstance(values.get("attempt_identity"), str)
            and bool(values["attempt_identity"])
        ):
            return None
        identity = self._manifest_workspace_identity(values, path, job_mtime=job_mtime)
        if identity is None:
            return None
        return identity

    def _owned_attempt_quarantine(
        self,
        job_name: str,
        quarantine_name: str,
        path: Path,
        *,
        job_mtime: float,
    ) -> tuple[str, float, int] | None:
        if not quarantine_name.startswith(ATTEMPT_QUARANTINE_PREFIX):
            return None
        try:
            values = self._read_manifest(path)
        except (OSError, ValueError, TypeError, KeyError):
            try:
                values = self._read_manifest(path, filename=QUARANTINE_METADATA)
            except (OSError, ValueError, TypeError, KeyError):
                return None
        if (
            values.get("job_id") != job_name
            or type(values.get("attempt")) is not int
            or not isinstance(values.get("attempt_identity"), str)
            or not values["attempt_identity"]
        ):
            return None
        identity = self._manifest_workspace_identity(values, path, job_mtime=job_mtime)
        if identity is None:
            return None
        return identity[0], identity[1], int(values["attempt"])

    def _find_owned_attempt_quarantine(self, job_fd: int, expected_identity: str) -> str | None:
        matches: list[str] = []
        for entry in os.scandir(job_fd):
            if not entry.name.startswith(ATTEMPT_QUARANTINE_PREFIX):
                continue
            try:
                quarantine_fd = self._open_child_dir(job_fd, entry.name)
            except OSError:
                continue
            try:
                if expected_identity and _identity(os.fstat(quarantine_fd)) == expected_identity:
                    matches.append(entry.name)
            finally:
                os.close(quarantine_fd)
        if len(matches) > 1:
            raise OSError("multiple owned attempt quarantines found")
        return matches[0] if matches else None

    @staticmethod
    def _manifest_workspace_identity(
        values: dict[str, object], path: Path, *, job_mtime: float | None = None
    ) -> tuple[str, float] | None:
        try:
            attempt_info = os.stat(path, follow_symlinks=False)
            job_info = os.stat(path.parent, follow_symlinks=False)
        except OSError:
            return None
        if not (
            stat.S_ISDIR(attempt_info.st_mode)
            and stat.S_ISDIR(job_info.st_mode)
            and values["job_identity"] == _identity(job_info)
            and values["attempt_identity"] == _identity(attempt_info)
        ):
            return None
        return _identity(attempt_info), job_info.st_mtime if job_mtime is None else job_mtime

    def _contains_only_owned_workspaces(self) -> bool:
        with os.scandir(self.root) as jobs:
            entries = tuple(jobs)
        for job in entries:
            if not job.is_dir(follow_symlinks=False) or job.name == WORKSPACE_ROOT_MARKER:
                return False
            with os.scandir(job.path) as attempts:
                attempt_entries = tuple(
                    attempt
                    for attempt in attempts
                    if attempt.name not in {".voiceink.lock", WORKSPACE_ACTIVE_LOCK}
                )
            if not attempt_entries or any(
                not attempt.is_dir(follow_symlinks=False)
                or not self._is_owned_workspace(job.name, attempt.name, Path(attempt.path))
                for attempt in attempt_entries
            ):
                return False
        return True

    def _write_manifest(self, workspace: Path, values: dict[str, object]) -> None:
        encoded = self._encode_manifest(values)
        if len(encoded) > self.max_workspace_bytes:
            raise ResourceLimitExceededError("workspace exceeds the configured quota")
        workspace_fd = self._open_workspace_fd_from_path(workspace)
        try:
            try:
                previous_info = os.stat("manifest.json", dir_fd=workspace_fd, follow_symlinks=False)
                if not stat.S_ISREG(previous_info.st_mode) or previous_info.st_nlink != 1:
                    raise OSError("manifest is not a regular file")
                previous_size = previous_info.st_size
            except FileNotFoundError:
                previous_size = 0
        finally:
            os.close(workspace_fd)
        delta = len(encoded) - previous_size
        if delta > 0:
            self._reserve_workspace(workspace, delta)
        elif delta < 0:
            self._release_workspace(workspace, -delta)
        try:
            workspace_fd = self._open_workspace_fd_from_path(workspace)
            manifest_fd = -1
            try:
                manifest_fd = os.open(
                    "manifest.json",
                    os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=workspace_fd,
                )
                if os.fstat(manifest_fd).st_nlink != 1:
                    raise OSError("manifest hard links are not permitted")
                _write_all(manifest_fd, encoded)
                os.ftruncate(manifest_fd, len(encoded))
            finally:
                if manifest_fd >= 0:
                    os.close(manifest_fd)
                os.close(workspace_fd)
        except Exception:
            if delta > 0:
                self._release_workspace(workspace, delta)
            elif delta < 0:
                self._reserve_workspace(workspace, -delta)
            raise

    def _read_manifest(
        self, workspace: Path, *, filename: str = "manifest.json"
    ) -> dict[str, object]:
        workspace_fd = self._open_workspace_fd_from_path(workspace)
        manifest_fd = -1
        try:
            manifest_fd = os.open(
                filename,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=workspace_fd,
            )
            info = os.fstat(manifest_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 64 * 1024:
                raise OSError("manifest is not a private regular file")
            data = bytearray()
            while True:
                chunk = os.read(manifest_fd, 16 * 1024)
                if not chunk:
                    break
                data.extend(chunk)
            values = json.loads(bytes(data).decode("ascii"))
            if not isinstance(values, dict):
                raise ValueError("manifest must be an object")
            return values
        finally:
            if manifest_fd >= 0:
                os.close(manifest_fd)
            os.close(workspace_fd)

    @staticmethod
    def _encode_manifest(values: dict[str, object]) -> bytes:
        return json.dumps(values, sort_keys=True, separators=(",", ":")).encode("ascii")

    def _scan_workspace_usage(self) -> tuple[dict[Path, int], dict[Path, int]]:
        usage: dict[Path, int] = {}
        snapshots: dict[Path, int] = {}
        for job_entry in os.scandir(self.root):
            if not job_entry.is_dir(follow_symlinks=False):
                continue
            job_path = Path(job_entry.path)
            for attempt_entry in os.scandir(job_path):
                if not attempt_entry.is_dir(follow_symlinks=False):
                    continue
                attempt_path = Path(attempt_entry.path)
                usage[attempt_path] = self._directory_size(attempt_path, exclude_snapshot=True)
                snapshot = attempt_path / "source.snapshot"
                try:
                    info = snapshot.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    snapshots[attempt_path] = info.st_size
        return usage, snapshots

    @classmethod
    def _directory_size(cls, path: Path, *, exclude_snapshot: bool = False) -> int:
        total = 0
        for entry in os.scandir(path):
            if exclude_snapshot and entry.name == "source.snapshot":
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
            elif stat.S_ISDIR(info.st_mode):
                total += cls._directory_size(Path(entry.path), exclude_snapshot=exclude_snapshot)
        return total

    def _reserve_workspace(self, path: Path, amount: int) -> None:
        if amount < 0:
            raise ResourceLimitExceededError("workspace quota cannot be negative")
        with self._usage_lock:
            used = self._workspace_reserved.get(path, 0)
            if used + amount > self.max_workspace_bytes:
                raise ResourceLimitExceededError("workspace exceeds the configured quota")
            self._workspace_reserved[path] = used + amount

    def _release_workspace(self, path: Path, amount: int | None = None) -> None:
        with self._usage_lock:
            current = self._workspace_reserved.get(path, 0)
            remaining = max(0, current - amount) if amount is not None else 0
            if remaining:
                self._workspace_reserved[path] = remaining
            else:
                self._workspace_reserved.pop(path, None)

    def _reserve_snapshot(self, path: Path, amount: int) -> None:
        if amount < 0:
            raise ResourceLimitExceededError("snapshot quota cannot be negative")
        with self._usage_lock:
            used = self._snapshot_reserved.get(path, 0)
            if used + amount > self.max_snapshot_bytes:
                raise ResourceLimitExceededError("snapshots exceed the configured quota")
            self._snapshot_reserved[path] = used + amount

    def _release_snapshot(self, path: Path, amount: int | None = None) -> None:
        with self._usage_lock:
            current = self._snapshot_reserved.get(path, 0)
            remaining = max(0, current - amount) if amount is not None else 0
            if remaining:
                self._snapshot_reserved[path] = remaining
            else:
                self._snapshot_reserved.pop(path, None)

    def _release_workspace_tree(self, root: Path) -> None:
        with self._usage_lock:
            for path in tuple(self._workspace_reserved):
                if path == root or root in path.parents:
                    self._workspace_reserved.pop(path, None)
            for path in tuple(self._snapshot_reserved):
                if path == root or root in path.parents:
                    self._snapshot_reserved.pop(path, None)

    def _open_workspace_fd(self, workspace: JobWorkspace) -> int:
        fd = self._open_workspace_fd_from_path(workspace.path)
        try:
            self._assert_workspace_identity(workspace, fd)
            return fd
        except Exception:
            os.close(fd)
            raise

    def _assert_workspace_identity(
        self, workspace: JobWorkspace, attempt_fd: int | None = None
    ) -> None:
        fd = (
            attempt_fd
            if attempt_fd is not None
            else self._open_workspace_fd_from_path(workspace.path)
        )
        root_fd = self._open_dir(self.root)
        job_fd = -1
        try:
            if workspace.attempt_identity and _identity(os.fstat(fd)) != workspace.attempt_identity:
                raise OSError("attempt workspace identity changed")
            if workspace.job_identity:
                job_fd = self._open_child_dir(root_fd, workspace.job_id.value)
                if _identity(os.fstat(job_fd)) != workspace.job_identity:
                    raise OSError("job workspace identity changed")
        finally:
            if job_fd >= 0:
                os.close(job_fd)
            os.close(root_fd)
            if attempt_fd is None:
                os.close(fd)

    def _open_workspace_fd_from_path(self, path: Path) -> int:
        if not self._contained(path, self.root):
            raise OSError("workspace containment cannot be proven")
        root_fd = self._open_dir(self.root)
        current_fd = root_fd
        try:
            relative = path.relative_to(self.root).parts
            for part in relative:
                next_fd = self._open_child_dir(current_fd, part)
                if current_fd != root_fd:
                    os.close(current_fd)
                current_fd = next_fd
            if current_fd != root_fd:
                os.close(root_fd)
            return current_fd
        except Exception:
            if current_fd != root_fd:
                os.close(current_fd)
            os.close(root_fd)
            raise

    @staticmethod
    def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
        return _identity(left) == _identity(right)

    @staticmethod
    def _open_dir(path: Path) -> int:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))

    @staticmethod
    def _open_child_dir(parent_fd: int, name: str) -> int:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )

    @classmethod
    def _open_absolute(cls, path: Path, flags: int) -> int:
        if not path.is_absolute():
            raise OSError("absolute path required")
        current = cls._open_dir(Path(path.anchor))
        try:
            parts = path.parts[1:]
            for index, part in enumerate(parts):
                final = index == len(parts) - 1
                next_flags = flags | getattr(os, "O_NOFOLLOW", 0)
                if not final:
                    next_flags |= os.O_DIRECTORY
                next_fd = os.open(part, next_flags, dir_fd=current)
                os.close(current)
                current = next_fd
            return current
        except Exception:
            os.close(current)
            raise

    def _remove_entry(
        self,
        parent_fd: int,
        name: str,
        *,
        deadline: float | None = None,
        expected_identity: str | None = None,
    ) -> None:
        self._check_cleanup_deadline(deadline)
        self._lock_directory(parent_fd)
        try:
            self._remove_entry_locked(
                parent_fd,
                name,
                deadline=deadline,
                expected_identity=expected_identity,
            )
        finally:
            self._unlock_directory(parent_fd)

    def _remove_entry_locked(
        self,
        parent_fd: int,
        name: str,
        *,
        deadline: float | None = None,
        expected_identity: str | None = None,
    ) -> None:
        self._check_cleanup_deadline(deadline)
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if expected_identity is not None and _identity(info) != expected_identity:
            raise OSError("workspace entry changed during cleanup")
        quarantine = f".voiceink-delete-{uuid.uuid4().hex}"
        try:
            os.rename(
                name,
                quarantine,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except OSError as error:
            raise OSError("workspace entry could not be quarantined safely") from error
        moved = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
        if not self._same_identity(info, moved):
            raise OSError("workspace entry changed during quarantine")
        if stat.S_ISDIR(info.st_mode):
            child_fd = self._open_child_dir(parent_fd, quarantine)
            try:
                if not self._same_identity(info, os.fstat(child_fd)):
                    raise OSError("workspace directory changed during cleanup")
                children = sorted(
                    os.scandir(child_fd),
                    key=lambda child: child.name == QUARANTINE_METADATA,
                )
                for child in children:
                    self._remove_entry_locked(child_fd, child.name, deadline=deadline)
            finally:
                os.close(child_fd)
            current = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
            if not self._same_identity(info, current):
                raise OSError("workspace directory changed during cleanup")
            os.rmdir(quarantine, dir_fd=parent_fd)
            return
        current = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
        if not self._same_identity(info, current):
            raise OSError("workspace entry changed during cleanup")
        os.unlink(quarantine, dir_fd=parent_fd)

    @staticmethod
    def _lock_directory(directory_fd: int) -> None:
        if fcntl is None:
            raise OSError("POSIX workspace locking is unavailable")
        fcntl.flock(directory_fd, fcntl.LOCK_EX)

    @staticmethod
    def _unlock_directory(directory_fd: int) -> None:
        if fcntl is not None:
            fcntl.flock(directory_fd, fcntl.LOCK_UN)

    @classmethod
    def _try_workspace_lock(cls, workspace_fd: int) -> int:
        return cls._try_workspace_lock_file(workspace_fd, ".voiceink.lock")

    @classmethod
    def _try_workspace_lock_file(cls, workspace_fd: int, name: str) -> int:
        lock_fd = os.open(
            name,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=workspace_fd,
        )
        if fcntl is None:
            os.close(lock_fd)
            raise OSError("POSIX workspace locking is unavailable")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            return -1
        return lock_fd

    @staticmethod
    def _open_workspace_lock(workspace_fd: int) -> int:
        return os.open(
            ".voiceink.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=workspace_fd,
        )

    @staticmethod
    def _job_directory_is_empty(job_fd: int) -> bool:
        return all(
            entry.name in {".voiceink.lock", WORKSPACE_ACTIVE_LOCK} for entry in os.scandir(job_fd)
        )

    def _remove_empty_job_files_locked(
        self, root_fd: int, job_name: str, job_fd: int, expected_identity: str
    ) -> bool:
        if not self._job_directory_is_empty(job_fd):
            return False
        current_identity = _identity(os.stat(job_name, dir_fd=root_fd, follow_symlinks=False))
        if expected_identity and current_identity != expected_identity:
            raise OSError("job workspace identity changed before quarantine")
        expected_identity = current_identity
        quarantine_name = f".voiceink-quarantine-{uuid.uuid4().hex}"
        os.rename(
            job_name,
            quarantine_name,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        quarantine_fd = self._open_child_dir(root_fd, quarantine_name)
        try:
            if _identity(os.fstat(quarantine_fd)) != expected_identity:
                raise OSError("job workspace identity changed during quarantine")
        finally:
            os.close(quarantine_fd)
        for entry in os.scandir(job_fd):
            os.unlink(entry.name, dir_fd=job_fd)
        os.rmdir(quarantine_name, dir_fd=root_fd)
        return True

    def _quarantine_attempt_locked(
        self,
        job_fd: int,
        job_name: str,
        attempt_name: str,
        attempt_fd: int,
        expected_identity: str,
    ) -> str:
        current_identity = _identity(os.fstat(attempt_fd))
        if expected_identity and current_identity != expected_identity:
            raise OSError("attempt workspace identity changed before quarantine")
        expected_identity = expected_identity or current_identity
        if (
            _identity(os.stat(attempt_name, dir_fd=job_fd, follow_symlinks=False))
            != expected_identity
        ):
            raise OSError("attempt path changed before quarantine")
        quarantine_name = f"{ATTEMPT_QUARANTINE_PREFIX}{uuid.uuid4().hex}"
        os.rename(attempt_name, quarantine_name, src_dir_fd=job_fd, dst_dir_fd=job_fd)
        quarantine_fd = self._open_child_dir(job_fd, quarantine_name)
        try:
            if _identity(os.fstat(quarantine_fd)) != expected_identity:
                raise OSError("attempt workspace identity changed during quarantine")
            self._write_quarantine_metadata(
                quarantine_fd,
                attempt=int(attempt_name.removeprefix("attempt-")),
                job_name=job_name,
                job_identity=_identity(os.fstat(job_fd)),
                attempt_identity=expected_identity,
            )
            os.fsync(job_fd)
        finally:
            os.close(quarantine_fd)
        return quarantine_name

    @staticmethod
    def _write_quarantine_metadata(
        directory_fd: int,
        *,
        attempt: int,
        job_name: str,
        job_identity: str,
        attempt_identity: str,
    ) -> None:
        data = json.dumps(
            {
                "attempt": attempt,
                "job_id": job_name,
                "job_identity": job_identity,
                "attempt_identity": attempt_identity,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        metadata_fd = os.open(
            QUARANTINE_METADATA,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        try:
            os.write(metadata_fd, data)
            os.fsync(metadata_fd)
        finally:
            os.close(metadata_fd)
        os.fsync(directory_fd)

    def _remove_quarantine_directory_locked(
        self, parent_fd: int, name: str, *, expected_identity: str | None = None
    ) -> None:
        quarantine_fd = self._open_child_dir(parent_fd, name)
        metadata: bytes | None = None
        try:
            quarantine_identity = _identity(os.fstat(quarantine_fd))
            if expected_identity and quarantine_identity != expected_identity:
                raise OSError("quarantine identity changed before cleanup")
            try:
                metadata_fd = os.open(
                    QUARANTINE_METADATA,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=quarantine_fd,
                )
            except FileNotFoundError:
                pass
            else:
                try:
                    metadata = os.read(metadata_fd, 64 * 1024)
                finally:
                    os.close(metadata_fd)
            children = sorted(
                os.scandir(quarantine_fd),
                key=lambda child: child.name == QUARANTINE_METADATA,
            )
            for child in children:
                if child.name == QUARANTINE_METADATA:
                    continue
                self._remove_entry_locked(quarantine_fd, child.name)
            if (
                _identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
                != quarantine_identity
            ):
                raise OSError("quarantine path changed before metadata removal")
            if metadata is not None:
                os.unlink(QUARANTINE_METADATA, dir_fd=quarantine_fd)
            try:
                if (
                    _identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
                    != quarantine_identity
                ):
                    raise OSError("quarantine path changed before directory removal")
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                if metadata is not None:
                    metadata_fd = os.open(
                        QUARANTINE_METADATA,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=quarantine_fd,
                    )
                    try:
                        os.write(metadata_fd, metadata)
                        os.fsync(metadata_fd)
                    finally:
                        os.close(metadata_fd)
                    os.fsync(quarantine_fd)
                raise
        finally:
            os.close(quarantine_fd)

    @contextmanager
    def _workspace_lock(self, workspace: Path):
        job_fd = self._open_workspace_fd_from_path(workspace.parent)
        lock_fd = -1
        try:
            lock_fd = self._open_workspace_lock(job_fd)
            self._lock_directory(lock_fd)
            yield
        finally:
            if lock_fd >= 0:
                self._unlock_directory(lock_fd)
                os.close(lock_fd)
            os.close(job_fd)

    @contextmanager
    def workspace_lock(self, workspace: JobWorkspace):
        job_fd = self._open_workspace_fd_from_path(workspace.path.parent)
        lock_fd = -1
        try:
            lock_fd = os.open(
                WORKSPACE_ACTIVE_LOCK,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=job_fd,
            )
            self._lock_directory(lock_fd)
            yield
        finally:
            if lock_fd >= 0:
                self._unlock_directory(lock_fd)
                os.close(lock_fd)
            os.close(job_fd)

    def _check_cleanup_deadline(self, deadline: float | None) -> None:
        if deadline is not None and self._clock.monotonic() >= deadline:
            raise TimeoutError("workspace cleanup deadline expired")

    @staticmethod
    def _contained(path: Path, root: Path) -> bool:
        try:
            return os.path.commonpath((str(path.absolute()), str(root.absolute()))) == str(
                root.absolute()
            )
        except ValueError:
            return False

    @staticmethod
    def _reject_symlink_components(path: Path) -> None:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            if current.is_symlink():
                raise InvalidSourceError("source path contains a symbolic link")
