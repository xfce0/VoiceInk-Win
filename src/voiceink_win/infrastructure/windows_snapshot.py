"""Native Windows handle-based imported-media snapshots."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from threading import Event, Lock, Thread, current_thread

from voiceink_win.domain import (
    AsrTimeoutError,
    Attempt,
    CancellationError,
    CancellationToken,
    InvalidInputError,
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
from .media_snapshot import (
    SOURCE_LIMIT,
    WORKSPACE_LIMIT,
    LocalMediaSnapshotStore,
    WindowsAdapterRequiredError,
    _WindowsFileInformation,
)


def _check_lifecycle(
    cancellation: CancellationToken | None,
    deadline: float | None,
    clock: MonotonicClock,
) -> None:
    if cancellation is not None and cancellation.is_cancelled():
        raise CancellationError("media snapshot operation was cancelled")
    if deadline is not None and clock.monotonic() >= deadline:
        raise AsrTimeoutError("media snapshot operation exceeded its deadline")


def _check_cleanup_deadline(deadline: float | None, clock: MonotonicClock) -> None:
    if deadline is not None and clock.monotonic() >= deadline:
        raise TimeoutError("workspace cleanup deadline expired")


def _signature(function, args, result) -> None:
    function.argtypes = args
    function.restype = result


def _check_bool(result, function, arguments):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def _check_find_next(result, function, arguments):
    if result or ctypes.get_last_error() == _ERROR_NO_MORE_FILES:
        return result
    raise ctypes.WinError(ctypes.get_last_error())


def _check_overlapped_result(result, function, arguments):
    if result or ctypes.get_last_error() == _ERROR_HANDLE_EOF:
        return result
    raise ctypes.WinError(ctypes.get_last_error())


def _check_cancel(result, function, arguments):
    if result or ctypes.get_last_error() == 1168:  # ERROR_NOT_FOUND: operation already completed.
        return result
    raise ctypes.WinError(ctypes.get_last_error())


def _check_io_bool(result, function, arguments):
    if result or ctypes.get_last_error() == 997:
        return result
    raise ctypes.WinError(ctypes.get_last_error())


def _check_read_bool(result, function, arguments):
    if result or ctypes.get_last_error() in (997, _ERROR_HANDLE_EOF):
        return result
    raise ctypes.WinError(ctypes.get_last_error())


def _check_handle(result, function, arguments):
    value = getattr(result, "value", result)
    if value in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    return result


_CANCEL_DRAIN_TIMEOUT_MS = 1000
_ERROR_NO_MORE_FILES = 18
_ERROR_HANDLE_EOF = 38
_STATUS_INVALID_PARAMETER = 0xC000000D


class _DetachedOverlapped(Exception):
    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


class _WindowsFileDispositionEx(ctypes.Structure):
    _fields_ = [("flags", wintypes.DWORD)]


class _WindowsFileBasicInformation(ctypes.Structure):
    _fields_ = [
        ("creation_time", ctypes.c_longlong),
        ("last_access_time", ctypes.c_longlong),
        ("last_write_time", ctypes.c_longlong),
        ("change_time", ctypes.c_longlong),
        ("file_attributes", wintypes.DWORD),
        ("reserved", wintypes.DWORD),
    ]


class _WindowsOverlapped(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_size_t),
        ("internal_high", ctypes.c_size_t),
        ("offset", wintypes.DWORD),
        ("offset_high", wintypes.DWORD),
        ("event", wintypes.HANDLE),
    ]


class _WindowsUnicodeString(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.USHORT),
        ("maximum_length", wintypes.USHORT),
        ("buffer", wintypes.LPWSTR),
    ]


class _WindowsObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.ULONG),
        ("root_directory", wintypes.HANDLE),
        ("object_name", ctypes.POINTER(_WindowsUnicodeString)),
        ("attributes", wintypes.ULONG),
        ("security_descriptor", wintypes.LPVOID),
        ("security_quality_of_service", wintypes.LPVOID),
    ]


class _WindowsIoStatusBlock(ctypes.Structure):
    _fields_ = [("status", ctypes.c_long), ("information", ctypes.c_size_t)]


class _WindowsFindData(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("reserved0", wintypes.DWORD),
        ("reserved1", wintypes.DWORD),
        ("name", wintypes.WCHAR * 260),
        ("alternate_name", wintypes.WCHAR * 14),
    ]


class WindowsKernel32:
    """Small injectable kernel32 binding surface."""

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    DELETE = 0x00010000
    SHARE = 0x1 | 0x2 | 0x4
    FILE_SHARE_READ = 0x1
    OPEN_EXISTING = 3
    CREATE_NEW = 1
    CREATE_ALWAYS = 2
    OPEN_ALWAYS = 4
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OVERLAPPED = 0x40000000
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    FILE_ATTRIBUTE_DIRECTORY = 0x10
    FILE_TYPE_DISK = 0x1
    FILE_ATTRIBUTE_READONLY = 0x1
    FILE_BEGIN = 0
    FILE_DISPOSITION_INFO_EX = 64
    FILE_BASIC_INFO = 0
    FILE_DISPOSITION_FLAG_DELETE = 0x1
    FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE = 0x10
    VOLUME_NAME_DOS = 0
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    DUPLICATE_SAME_ACCESS = 0x2
    ERROR_NO_MORE_FILES = 18
    ERROR_HANDLE_EOF = 38
    FILE_OPEN_REPARSE_POINT = 0x00200000
    FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
    FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    FILE_NON_DIRECTORY_FILE = 0x00000040
    FILE_OPEN = 1
    FILE_OPEN_IF = 3
    OBJ_CASE_INSENSITIVE = 0x40

    def __init__(self, dll: object | None = None) -> None:
        self.dll = dll or ctypes.WinDLL("kernel32", use_last_error=True)
        self.ntdll = None
        if dll is None:
            self.ntdll = ctypes.WinDLL("ntdll")
            _signature(
                self.ntdll.NtOpenFile,
                [
                    ctypes.POINTER(wintypes.HANDLE),
                    wintypes.DWORD,
                    ctypes.POINTER(_WindowsObjectAttributes),
                    ctypes.POINTER(_WindowsIoStatusBlock),
                    wintypes.DWORD,
                    wintypes.DWORD,
                ],
                ctypes.c_long,
            )
            _signature(
                self.ntdll.NtCreateFile,
                [
                    ctypes.POINTER(wintypes.HANDLE),
                    wintypes.DWORD,
                    ctypes.POINTER(_WindowsObjectAttributes),
                    ctypes.POINTER(_WindowsIoStatusBlock),
                    ctypes.POINTER(ctypes.c_longlong),
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                ],
                ctypes.c_long,
            )
        _signature(
            self.dll.CreateFileW,
            [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ],
            wintypes.HANDLE,
        )
        self.dll.CreateFileW.errcheck = _check_handle
        _signature(
            self.dll.GetFileInformationByHandle,
            [wintypes.HANDLE, ctypes.POINTER(_WindowsFileInformation)],
            wintypes.BOOL,
        )
        _signature(self.dll.GetFileType, [wintypes.HANDLE], wintypes.DWORD)
        _signature(
            self.dll.GetFinalPathNameByHandleW,
            [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD],
            wintypes.DWORD,
        )
        _signature(
            self.dll.ReadFile,
            [
                wintypes.HANDLE,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                wintypes.LPVOID,
            ],
            wintypes.BOOL,
        )
        self.dll.ReadFile.errcheck = _check_read_bool
        _signature(
            self.dll.WriteFile,
            [
                wintypes.HANDLE,
                wintypes.LPCVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                wintypes.LPVOID,
            ],
            wintypes.BOOL,
        )
        self.dll.WriteFile.errcheck = _check_io_bool
        _signature(
            self.dll.SetFilePointerEx,
            [
                wintypes.HANDLE,
                ctypes.c_longlong,
                ctypes.POINTER(ctypes.c_longlong),
                wintypes.DWORD,
            ],
            wintypes.BOOL,
        )
        self.dll.SetFilePointerEx.errcheck = _check_bool
        _signature(
            self.dll.SetFileInformationByHandle,
            [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD],
            wintypes.BOOL,
        )
        self.dll.SetFileInformationByHandle.errcheck = _check_bool
        for name, args, result in (
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            ("CreateDirectoryW", [wintypes.LPCWSTR, wintypes.LPVOID], wintypes.BOOL),
            ("DeleteFileW", [wintypes.LPCWSTR], wintypes.BOOL),
            ("RemoveDirectoryW", [wintypes.LPCWSTR], wintypes.BOOL),
            ("SetFileAttributesW", [wintypes.LPCWSTR, wintypes.DWORD], wintypes.BOOL),
            ("SetEndOfFile", [wintypes.HANDLE], wintypes.BOOL),
            (
                "FindFirstFileW",
                [wintypes.LPCWSTR, ctypes.POINTER(_WindowsFindData)],
                wintypes.HANDLE,
            ),
            ("FindNextFileW", [wintypes.HANDLE, ctypes.POINTER(_WindowsFindData)], wintypes.BOOL),
            ("FindClose", [wintypes.HANDLE], wintypes.BOOL),
            (
                "CreateEventW",
                [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR],
                wintypes.HANDLE,
            ),
            ("WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            (
                "GetOverlappedResult",
                [
                    wintypes.HANDLE,
                    ctypes.POINTER(_WindowsOverlapped),
                    ctypes.POINTER(wintypes.DWORD),
                    wintypes.BOOL,
                ],
                wintypes.BOOL,
            ),
            ("CancelIoEx", [wintypes.HANDLE, ctypes.POINTER(_WindowsOverlapped)], wintypes.BOOL),
            (
                "DuplicateHandle",
                [
                    wintypes.HANDLE,
                    wintypes.HANDLE,
                    wintypes.HANDLE,
                    ctypes.POINTER(wintypes.HANDLE),
                    wintypes.DWORD,
                    wintypes.BOOL,
                    wintypes.DWORD,
                ],
                wintypes.BOOL,
            ),
            ("GetCurrentProcess", [], wintypes.HANDLE),
        ):
            function = getattr(self.dll, name, None)
            if function is None:
                continue
            _signature(function, args, result)
            if result is wintypes.BOOL:
                function.errcheck = {
                    "CancelIoEx": _check_cancel,
                    "FindNextFileW": _check_find_next,
                    "GetOverlappedResult": _check_overlapped_result,
                }.get(name, _check_bool)


class WindowsMediaSnapshotStore(LocalMediaSnapshotStore):
    """MediaSnapshotStore implemented with Win32 file handles."""

    _clock: MonotonicClock = SystemMonotonicClock()

    def __init__(
        self,
        root: Path,
        *,
        import_roots: tuple[Path, ...] = (),
        max_workspace_bytes: int = WORKSPACE_LIMIT,
        max_snapshot_bytes: int = SOURCE_LIMIT,
        clock: object | None = None,
        kernel32: object | None = None,
    ) -> None:
        if os.name != "nt":
            raise WindowsAdapterRequiredError("Windows snapshot store requires Windows")
        if max_workspace_bytes < 1:
            raise ValueError("workspace quota must be positive")
        if max_snapshot_bytes < 1:
            raise ValueError("snapshot quota must be positive")
        self._api = WindowsKernel32(kernel32)
        requested_root = Path(root).absolute()
        existing_parent = requested_root
        while not existing_parent.exists() and existing_parent != existing_parent.parent:
            existing_parent = existing_parent.parent
        self._assert_no_reparse_components(existing_parent)
        requested_root.mkdir(parents=True, exist_ok=True)
        self._assert_no_reparse_components(requested_root)
        self.root = requested_root
        self._assert_directory(self.root)
        self._root_canonical = self._canonical(self.root)
        root_handle = self._open(
            self.root,
            self._api.GENERIC_READ,
            self._api.OPEN_EXISTING,
            directory=True,
            share=self._api.FILE_SHARE_READ,
        )
        try:
            self._root_identity, _ = self._identity(root_handle)
        finally:
            self._close(root_handle)
        self.import_roots = tuple(self._canonical(Path(item).absolute()) for item in import_roots)
        self.max_workspace_bytes = max_workspace_bytes
        self.max_snapshot_bytes = max_snapshot_bytes
        self._clock: MonotonicClock = clock or SystemMonotonicClock()
        self.windows_adapter = None
        self._usage_lock = Lock()
        self._workspace_reserved: dict[Path, int] = {}
        self._snapshot_reserved: dict[Path, int] = {}
        self._reaper_lock = Lock()
        self._reapers: set[Thread] = set()
        self._lease_recovery_lock = Lock()
        self._active_leases: set[_HandleLease] = set()
        self._failed_close_leases: set[_HandleLease] = set()
        self._lease_recovery = self._failed_close_leases
        self._lease_by_handle: dict[int, _HandleLease] = {}
        self._lease_recovery_wakeup = Event()
        self._lease_reaper: Thread | None = None
        self._lease_shutdown_deadline: float | None = None
        self.sweep_orphans(max_age_seconds=24 * 60 * 60)
        self._workspace_reserved, self._snapshot_reserved = self._scan_workspace_usage()

    def validate_source(self, path: Path, *, max_bytes: int = SOURCE_LIMIT) -> SourceMedia:
        candidate = Path(path).absolute()
        if self._is_unc(candidate):
            raise InvalidSourceError("network paths are not permitted")
        self._assert_no_reparse_components(candidate)
        handle = self._open(candidate, self._api.GENERIC_READ, self._api.OPEN_EXISTING)
        try:
            identity, size = self._identity(handle)
            canonical = self._canonical_handle(handle)
            if self.import_roots and not any(
                self._contained(canonical, root) for root in self.import_roots
            ):
                raise InvalidSourceError("source is outside the configured import roots")
            digest, actual_size = self._hash(handle, max_bytes)
            if actual_size != size:
                raise InvalidSourceError("source changed during admission")
            lease = _HandleLease(self._api, handle)
            self._register_lease(lease)
            return SourceMedia(
                candidate,
                candidate.name,
                identity,
                size,
                digest,
                lease,
            )
        except ResourceLimitExceededError as error:
            self._close_or_recover(handle)
            raise ResourceLimitRejectedError("source exceeds the admission size limit") from error
        except Exception:
            self._close_or_recover(handle)
            raise

    def create_workspace(self, job_id: JobId, attempt: int) -> JobWorkspace:
        job_path = self.root / job_id.value
        attempt_path = job_path / f"attempt-{attempt}"
        self._mkdir(job_path)
        self._assert_no_reparse_components(job_path)
        job_identity = ""
        partial_workspace = JobWorkspace(attempt_path, job_id, Attempt(attempt))
        job_handle: int | None = None
        try:
            job_handle = self._open(
                job_path, self._api.GENERIC_READ, self._api.OPEN_EXISTING, directory=True
            )
            job_identity, _ = self._identity(job_handle)
            partial_workspace = JobWorkspace(attempt_path, job_id, Attempt(attempt), job_identity)
        except BaseException as error:
            error.partial_workspace = partial_workspace
            raise
        finally:
            if job_handle is not None:
                try:
                    self._close(job_handle)
                except BaseException as error:
                    error.partial_workspace = partial_workspace
                    raise
        try:
            self._mkdir(attempt_path)
        except Exception as error:
            error.partial_workspace = partial_workspace
            raise
        job_handle = None
        attempt_handle: int | None = None
        try:
            job_handle = self._open(
                job_path, self._api.GENERIC_READ, self._api.OPEN_EXISTING, directory=True
            )
            attempt_handle = self._open(
                attempt_path, self._api.GENERIC_READ, self._api.OPEN_EXISTING, directory=True
            )
            job_identity, _ = self._identity(job_handle)
            attempt_identity, _ = self._identity(attempt_handle)
        finally:
            errors = self._close_handles((attempt_handle, job_handle))
            if errors:
                cleanup_error = ExceptionGroup("workspace handle cleanup failed", errors)
                cleanup_error.partial_workspace = JobWorkspace(
                    attempt_path, job_id, Attempt(attempt), job_identity, attempt_identity
                )
                raise cleanup_error
        workspace = JobWorkspace(
            attempt_path, job_id, Attempt(attempt), job_identity, attempt_identity
        )
        manifest_directory = self._open(
            workspace.path,
            self._api.GENERIC_READ | self._api.GENERIC_WRITE,
            self._api.OPEN_EXISTING,
            directory=True,
        )
        try:
            self._write_manifest(
                workspace.path,
                {
                    "job_id": job_id.value,
                    "attempt": attempt,
                    "job_identity": job_identity,
                    "attempt_identity": attempt_identity,
                },
                directory_handle=manifest_directory,
            )
        except Exception as error:
            try:
                self.cleanup(workspace)
            except BaseException as cleanup_error:
                error.partial_workspace = workspace
                error.add_note(f"workspace rollback failed: {cleanup_error}")
            raise
        finally:
            errors = self._close_handles((manifest_directory,))
            if errors:
                cleanup_error = ExceptionGroup("workspace manifest handle cleanup failed", errors)
                cleanup_error.partial_workspace = workspace
                raise cleanup_error
        return workspace

    def snapshot(
        self,
        source: SourceMedia,
        workspace: JobWorkspace,
        *,
        cancellation: CancellationToken | None = None,
        deadline: float | None = None,
    ) -> SourceSnapshot:
        self._assert_no_reparse_components(source.path)
        self._assert_no_reparse_components(workspace.path)
        source_lease = source.admission_handle
        source_handle = source_lease.handle if isinstance(source_lease, _HandleLease) else None
        owns_source_handle = source_handle is None
        io_source_handle: int | None = None
        destination_handle: int | None = None
        workspace_handle: int | None = None
        reserved = False
        try:
            if source_handle is None:
                source_handle = self._open(
                    source.path,
                    self._api.GENERIC_READ,
                    self._api.OPEN_EXISTING,
                )
            io_source_handle = self._open(
                source.path,
                self._api.GENERIC_READ,
                self._api.OPEN_EXISTING,
                overlapped=cancellation is not None or deadline is not None,
            )
            workspace_handle = self._open(
                workspace.path,
                self._api.GENERIC_READ,
                self._api.OPEN_EXISTING,
                directory=True,
            )
            workspace_identity, _ = self._identity(workspace_handle)
            if workspace_identity != workspace.attempt_identity:
                raise SourceChangedError("workspace changed before snapshot")
            before_identity, before_size = self._identity(source_handle)
            if (before_identity, before_size) != (source.identity, source.size):
                raise SourceChangedError("source changed after admission")
            if self._identity(io_source_handle)[0] != before_identity:
                raise SourceChangedError("source changed before snapshot copy")
            self._reserve_snapshot(workspace.path, source.size)
            reserved = True
            destination_path = workspace.path / "source.snapshot"
            destination_handle = self._open(
                destination_path,
                self._api.GENERIC_READ | self._api.GENERIC_WRITE,
                self._api.CREATE_NEW,
                overlapped=cancellation is not None or deadline is not None,
            )
            if self._identity(workspace_handle)[0] != workspace.attempt_identity:
                raise SourceChangedError("workspace changed during snapshot creation")
            digest, total = self._copy(
                io_source_handle,
                destination_handle,
                source.size,
                cancellation=cancellation,
                deadline=deadline,
            )
            basic_info = _WindowsFileBasicInformation()
            basic_info.file_attributes = self._api.FILE_ATTRIBUTE_READONLY
            self._api.dll.SetFileInformationByHandle(
                destination_handle,
                self._api.FILE_BASIC_INFO,
                ctypes.byref(basic_info),
                ctypes.sizeof(basic_info),
            )
            after_identity, after_size = self._identity(source_handle)
            if (
                (before_identity, before_size) != (after_identity, after_size)
                or total != source.size
                or digest != source.sha256
            ):
                raise SourceChangedError("source changed during snapshot")
            snapshot_identity, snapshot_size = self._identity(destination_handle)
            manifest = SnapshotManifest(
                snapshot_identity,
                snapshot_size,
                digest,
                workspace.job_identity,
                workspace.attempt_identity,
            )
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
                directory_handle=workspace_handle,
            )
            return SourceSnapshot(
                source,
                destination_path,
                manifest,
                source.identity,
                source.size,
                source.sha256,
            )
        except Exception:
            if reserved:
                self._release_snapshot(workspace.path, source.size)
            raise
        finally:
            errors = self._close_handles(
                (
                    destination_handle,
                    io_source_handle,
                    workspace_handle,
                    source_handle if owns_source_handle else None,
                )
            )
            if errors:
                raise ExceptionGroup("snapshot handle cleanup failed", errors)

    def release_source(self, source: SourceMedia) -> None:
        lease = source.admission_handle
        if isinstance(lease, _HandleLease):
            self._release_lease(lease)
        elif lease is not None:
            self._close_or_recover(lease)

    def close(self, timeout: float = 1.0) -> None:
        self._ensure_lease_recovery_state()
        deadline = self._clock.monotonic() + timeout
        with self._lease_recovery_lock:
            self._lease_shutdown_deadline = deadline
        self._lease_recovery_wakeup.set()
        # A reaper that reached an earlier shutdown deadline has exited; a new
        # close call owns a new retry budget and must re-arm it.
        self._start_lease_reaper()
        with self._reaper_lock:
            reapers = tuple(self._reapers)
        with self._lease_recovery_lock:
            lease_reaper = self._lease_reaper
        for reaper in (*reapers, lease_reaper):
            if reaper is not None:
                reaper.join(timeout=max(0.0, deadline - self._clock.monotonic()))
        with self._reaper_lock:
            reapers_alive = any(reaper.is_alive() for reaper in self._reapers)
        with self._lease_recovery_lock:
            leases_alive = bool(self._active_leases or self._failed_close_leases)
        if reapers_alive or leases_alive:
            raise TimeoutError("Windows snapshot cleanup did not complete before shutdown")

    def verify(
        self,
        snapshot: SourceSnapshot,
        *,
        cancellation: CancellationToken | None = None,
        deadline: float | None = None,
    ) -> SourceSnapshot:
        _check_lifecycle(cancellation, deadline, self._clock)
        self._assert_no_reparse_components(snapshot.path)
        attempt_path = snapshot.path.parent
        job_path = attempt_path.parent
        if not self._contained_path(attempt_path, self.root):
            raise SourceChangedError("snapshot is outside the private workspace root")
        root_handle: int | None = None
        attempt_handle: int | None = None
        job_handle: int | None = None
        handle: int | None = None
        try:
            root_handle = self._open(
                self.root,
                self._api.GENERIC_READ,
                self._api.OPEN_EXISTING,
                directory=True,
                share=self._api.FILE_SHARE_READ,
            )
            job_handle = self._open_relative(
                root_handle,
                job_path.name,
                access=self._api.GENERIC_READ,
                share=self._api.FILE_SHARE_READ,
            )
            attempt_handle = self._open_relative(
                job_handle,
                attempt_path.name,
                access=self._api.GENERIC_READ,
                share=self._api.FILE_SHARE_READ,
            )
            handle = self._open_relative(
                attempt_handle,
                snapshot.path.name,
                access=self._api.GENERIC_READ,
                share=self._api.FILE_SHARE_READ,
            )
            root_identity, _ = self._identity(root_handle)
            attempt_identity, _ = self._identity(attempt_handle)
            job_identity, _ = self._identity(job_handle)
            if root_identity != getattr(self, "_root_identity", root_identity):
                raise SourceChangedError("private workspace root changed during verification")
            self._assert_contained_handle(root_handle)
            self._assert_contained_handle(job_handle)
            self._assert_contained_handle(attempt_handle)
            if (attempt_identity, job_identity) != (
                snapshot.manifest.attempt_identity,
                snapshot.manifest.job_identity,
            ):
                raise SourceChangedError("workspace identity changed during snapshot verification")
            identity, size = self._identity(handle)
            digest, actual_size = self._hash(
                handle,
                self.max_snapshot_bytes,
                cancellation=cancellation,
                deadline=deadline,
            )
            try:
                values = self._read_manifest(snapshot.path.parent, directory_handle=attempt_handle)
                manifest = SnapshotManifest(
                    values["snapshot_identity"],
                    values["snapshot_size"],
                    values["snapshot_sha256"],
                    values.get("job_identity", ""),
                    values.get("attempt_identity", ""),
                )
            except (KeyError, TypeError, ValueError, InvalidInputError) as error:
                raise SourceChangedError("snapshot manifest is invalid", cause=error) from error
            if manifest != snapshot.manifest or (identity, size, actual_size, digest) != (
                manifest.snapshot_identity,
                manifest.snapshot_size,
                manifest.snapshot_size,
                manifest.snapshot_sha256,
            ):
                raise SourceChangedError("immutable snapshot verification failed")
            self._seek(handle, 0)
            workspace_leases = (
                _HandleLease(self._api, root_handle),
                _HandleLease(self._api, job_handle),
                _HandleLease(self._api, attempt_handle),
            )
            for lease in workspace_leases:
                self._register_lease(lease)
            snapshot_lease = _HandleLease(self._api, handle)
            self._register_lease(snapshot_lease)
            return SourceSnapshot(
                snapshot.source,
                snapshot.path,
                manifest,
                snapshot.source_identity,
                snapshot.source_size,
                snapshot.source_sha256,
                VerifiedMediaHandle(
                    str(snapshot.path),
                    handle,
                    identity,
                    size,
                    digest,
                    snapshot_lease,
                    _VerifiedSnapshotHandle(self, handle, identity, size, digest),
                ),
                handle,
                workspace_leases,
            )
        except Exception as original_error:
            errors: list[BaseException] = []
            for pending in (handle, attempt_handle, job_handle, root_handle):
                if pending is None:
                    continue
                try:
                    self._close_or_recover(pending)
                except BaseException as error:
                    errors.append(error)
            if errors:
                raise ExceptionGroup(
                    "snapshot verification cleanup failed", [original_error, *errors]
                ) from original_error
            raise

    def release_snapshot(self, snapshot: SourceSnapshot) -> None:
        leases: list[_HandleLease] = []
        if snapshot.descriptor is not None:
            lease = snapshot.verified_input.lease if snapshot.verified_input else None
            if isinstance(lease, _HandleLease):
                leases.append(lease)
            elif snapshot.verified_input is not None:
                raise OSError("verified snapshot is missing its native handle lease")
        leases.extend(
            lease
            for lease in reversed(snapshot.workspace_leases)
            if isinstance(lease, _HandleLease)
        )
        errors: list[BaseException] = []
        for lease in leases:
            try:
                self._release_lease(lease)
            except BaseException as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("snapshot handle cleanup failed", errors)

    def cleanup(self, workspace: JobWorkspace, *, deadline: float | None = None) -> None:
        _check_cleanup_deadline(deadline, self._clock)
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
        _check_cleanup_deadline(deadline, self._clock)
        expected = self.root / workspace.job_id.value / f"attempt-{workspace.attempt.value}"
        if Path(workspace.path) != expected or not self._contained_path(expected.parent, self.root):
            raise OSError("workspace path does not match its owner")
        _check_cleanup_deadline(deadline, self._clock)
        self._assert_no_reparse_components(expected.parent, deadline=deadline)
        try:
            attempt_handle = self._open(
                expected,
                self._api.GENERIC_READ | self._api.DELETE,
                self._api.OPEN_EXISTING,
                directory=True,
            )
        except FileNotFoundError:
            self._remove_directory(
                expected.parent,
                expected_identity=workspace.job_identity,
                deadline=deadline,
            )
            return
        else:
            self._close(attempt_handle)
        self._remove_tree(expected, expected_identity=workspace.attempt_identity, deadline=deadline)
        self._remove_directory(
            expected.parent,
            expected_identity=workspace.job_identity,
            deadline=deadline,
        )

    def sweep_orphans(self, *, max_age_seconds: float) -> int:
        removed = 0
        for entry in self.root.iterdir():
            try:
                job_handle = self._open(
                    entry, self._api.GENERIC_READ, self._api.OPEN_EXISTING, directory=True
                )
            except OSError:
                continue
            try:
                job_identity, _ = self._identity(job_handle)
                if time.time() - entry.stat().st_mtime <= max_age_seconds:
                    continue
                for attempt in tuple(entry.iterdir()):
                    if not attempt.name.startswith("attempt-"):
                        continue
                    try:
                        attempt_number = int(attempt.name.removeprefix("attempt-"))
                        attempt_info = attempt.stat()
                        if time.time() - attempt_info.st_mtime <= max_age_seconds:
                            continue
                        manifest_path = attempt / "manifest.json"
                        if not manifest_path.exists():
                            attempt_handle = self._open(
                                attempt,
                                self._api.GENERIC_READ | self._api.DELETE,
                                self._api.OPEN_EXISTING,
                                directory=True,
                            )
                            try:
                                self._assert_contained_handle(attempt_handle)
                                attempt_identity, _ = self._identity(attempt_handle)
                            finally:
                                self._close(attempt_handle)
                            self._remove_tree(
                                attempt,
                                expected_identity=attempt_identity,
                            )
                            self._release_workspace_tree(attempt)
                            removed += 1
                            continue
                        values = self._read_manifest(attempt)
                        if (
                            values.get("job_id") != entry.name
                            or values.get("attempt") != attempt_number
                            or values.get("job_identity") != job_identity
                            or not values.get("attempt_identity")
                        ):
                            continue
                        self._remove_tree(
                            attempt,
                            expected_identity=str(values["attempt_identity"]),
                        )
                        self._release_workspace_tree(attempt)
                        removed += 1
                    except (OSError, ValueError, TypeError, KeyError):
                        continue
                if not any(entry.iterdir()):
                    self._remove_directory(entry, expected_identity=job_identity, handle=job_handle)
                    self._release_workspace_tree(entry)
            except OSError:
                pass
            finally:
                self._close(job_handle)
        return removed

    def _open(
        self,
        path: Path,
        access: int,
        disposition: int,
        *,
        directory: bool | None = False,
        share: int | None = None,
        overlapped: bool = False,
    ) -> int:
        if self._is_unc(path):
            raise InvalidSourceError("UNC paths are not permitted")
        flags = self._api.FILE_FLAG_OPEN_REPARSE_POINT
        if directory is not False:
            flags |= self._api.FILE_FLAG_BACKUP_SEMANTICS
        if overlapped:
            flags |= self._api.FILE_FLAG_OVERLAPPED
        raw = self._api.dll.CreateFileW(
            str(path),
            access,
            self._api.SHARE if share is None else share,
            None,
            disposition,
            flags,
            None,
        )
        value = getattr(raw, "value", raw)
        if value in (None, self._api.INVALID_HANDLE_VALUE):
            error_code = ctypes.get_last_error()
            if error_code in {2, 3}:
                raise FileNotFoundError(error_code, "CreateFileW path not found", str(path))
            raise OSError(error_code, "CreateFileW failed")
        info = _WindowsFileInformation()
        if not self._api.dll.GetFileInformationByHandle(value, ctypes.byref(info)):
            self._close(value)
            raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
        if info.attributes & self._api.FILE_ATTRIBUTE_REPARSE_POINT:
            self._close(value)
            raise OSError("reparse point is not permitted")
        is_directory = bool(info.attributes & self._api.FILE_ATTRIBUTE_DIRECTORY)
        if self._api.dll.GetFileType(value) != self._api.FILE_TYPE_DISK:
            self._close(value)
            raise OSError("only disk files and directories are permitted")
        if directory is True and not is_directory:
            self._close(value)
            raise OSError("path is not a directory")
        if directory is False and is_directory:
            self._close(value)
            raise OSError("path is not a regular file")
        return int(value)

    def _identity(self, handle: int) -> tuple[str, int]:
        info = _WindowsFileInformation()
        if not self._api.dll.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
        size = (info.size_high << 32) | info.size_low
        return f"{info.volume_serial}:{info.index_high}:{info.index_low}", size

    def _canonical_handle(self, handle: int) -> str:
        buffer = ctypes.create_unicode_buffer(32768)
        length = self._api.dll.GetFinalPathNameByHandleW(
            handle, buffer, len(buffer), self._api.VOLUME_NAME_DOS
        )
        if not length or length >= len(buffer):
            raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW failed")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\") or (
            value.startswith("\\\\") and not value.startswith("\\\\?\\")
        ):
            raise InvalidSourceError("network paths are not permitted")
        if not value.endswith(":\\"):
            value = value.rstrip("\\")
        return value.casefold()

    def _canonical(self, path: Path) -> str:
        handle = self._open(path, self._api.GENERIC_READ, self._api.OPEN_EXISTING, directory=True)
        try:
            return self._canonical_handle(handle)
        finally:
            self._close(handle)

    def _assert_directory(self, path: Path) -> None:
        handle = self._open(path, self._api.GENERIC_READ, self._api.OPEN_EXISTING, directory=True)
        self._close(handle)

    def _mkdir(self, path: Path) -> None:
        if not self._api.dll.CreateDirectoryW(str(path), None):
            raise OSError(ctypes.get_last_error(), "CreateDirectoryW failed")
        self._assert_directory(path)

    def _remove_directory(
        self,
        path: Path,
        *,
        expected_identity: str = "",
        handle=None,
        deadline: float | None = None,
    ) -> None:
        _check_cleanup_deadline(deadline, self._clock)
        owned = handle is None
        handle = handle or self._open(
            path,
            self._api.GENERIC_READ | self._api.DELETE,
            self._api.OPEN_EXISTING,
            directory=True,
        )
        try:
            _check_cleanup_deadline(deadline, self._clock)
            self._assert_contained_handle(handle)
            _check_cleanup_deadline(deadline, self._clock)
            identity, _ = self._identity(handle)
            if expected_identity and identity != expected_identity:
                raise OSError("workspace identity changed during cleanup")
            _check_cleanup_deadline(deadline, self._clock)
            self._delete_handle(handle)
        finally:
            if owned:
                self._close(handle)

    def _remove_tree(
        self,
        path: Path,
        *,
        expected_identity: str = "",
        deadline: float | None = None,
    ) -> None:
        _check_cleanup_deadline(deadline, self._clock)
        handle = self._open(
            path,
            self._api.GENERIC_READ | self._api.DELETE,
            self._api.OPEN_EXISTING,
            directory=True,
        )
        try:
            _check_cleanup_deadline(deadline, self._clock)
            self._assert_contained_handle(handle)
            _check_cleanup_deadline(deadline, self._clock)
            identity, _ = self._identity(handle)
            if expected_identity and identity != expected_identity:
                raise OSError("workspace identity changed during cleanup")
            tree_handle = handle
            handle = None
            self._remove_tree_handle(tree_handle, identity, path, deadline=deadline)
        finally:
            if handle is not None:
                self._close(handle)

    def _remove_tree_handle(
        self,
        handle: int,
        expected_identity: str,
        path: Path,
        *,
        deadline: float | None = None,
    ) -> None:
        try:
            _check_cleanup_deadline(deadline, self._clock)
            self._assert_contained_handle(handle)
            _check_cleanup_deadline(deadline, self._clock)
            identity, _ = self._identity(handle)
            if identity != expected_identity:
                raise OSError("workspace identity changed during cleanup")
            for child_name in self._directory_names(path, deadline=deadline):
                _check_cleanup_deadline(deadline, self._clock)
                child_handle = self._open_relative(handle, child_name)
                try:
                    _check_cleanup_deadline(deadline, self._clock)
                    self._assert_contained_handle(child_handle)
                    _check_cleanup_deadline(deadline, self._clock)
                    child_identity, _ = self._identity(child_handle)
                    info = _WindowsFileInformation()
                    _check_cleanup_deadline(deadline, self._clock)
                    if not self._api.dll.GetFileInformationByHandle(
                        child_handle, ctypes.byref(info)
                    ):
                        raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
                    if info.attributes & self._api.FILE_ATTRIBUTE_REPARSE_POINT:
                        _check_cleanup_deadline(deadline, self._clock)
                        self._delete_handle(child_handle)
                    elif info.attributes & self._api.FILE_ATTRIBUTE_DIRECTORY:
                        directory_handle = child_handle
                        child_handle = None
                        self._remove_tree_handle(
                            directory_handle,
                            child_identity,
                            path / child_name,
                            deadline=deadline,
                        )
                    else:
                        _check_cleanup_deadline(deadline, self._clock)
                        self._delete_handle(child_handle)
                finally:
                    if child_handle is not None:
                        self._close(child_handle)
            self._remove_directory(
                path,
                expected_identity=expected_identity,
                handle=handle,
                deadline=deadline,
            )
        finally:
            self._close(handle)

    def _directory_names(
        self, path: Path | str, *, deadline: float | None = None
    ) -> tuple[str, ...]:
        _check_cleanup_deadline(deadline, self._clock)
        pattern = str(path / "*" if isinstance(path, Path) else Path(path) / "*")
        data = _WindowsFindData()
        find_handle = self._api.dll.FindFirstFileW(pattern, ctypes.byref(data))
        if find_handle in (None, self._api.INVALID_HANDLE_VALUE):
            error = ctypes.get_last_error()
            if error == 2:
                return ()
            raise ctypes.WinError(error)
        names: list[str] = []
        try:
            while True:
                _check_cleanup_deadline(deadline, self._clock)
                name = data.name
                if name not in {".", ".."}:
                    names.append(name)
                _check_cleanup_deadline(deadline, self._clock)
                if self._api.dll.FindNextFileW(find_handle, ctypes.byref(data)):
                    continue
                if ctypes.get_last_error() != self._api.ERROR_NO_MORE_FILES:
                    raise ctypes.WinError(ctypes.get_last_error())
                return tuple(names)
        finally:
            self._close_find_or_recover(find_handle)

    def _open_relative(
        self,
        parent: int,
        name: str,
        *,
        access: int | None = None,
        share: int | None = None,
        disposition: int = WindowsKernel32.FILE_OPEN,
    ) -> int:
        if self._api.ntdll is None:
            raise WindowsAdapterRequiredError("relative Windows open requires native ntdll")
        buffer = ctypes.create_unicode_buffer(name)
        unicode_name = _WindowsUnicodeString(
            len(name.encode("utf-16-le")),
            ctypes.sizeof(buffer),
            ctypes.cast(buffer, wintypes.LPWSTR),
        )
        attributes = _WindowsObjectAttributes(
            ctypes.sizeof(_WindowsObjectAttributes),
            parent,
            ctypes.pointer(unicode_name),
            self._api.OBJ_CASE_INSENSITIVE,
            None,
            None,
        )
        status = _WindowsIoStatusBlock()
        handle = wintypes.HANDLE()
        desired_access = access or (self._api.GENERIC_READ | self._api.DELETE)
        options = (
            self._api.FILE_OPEN_REPARSE_POINT
            | self._api.FILE_OPEN_FOR_BACKUP_INTENT
            | self._api.FILE_SYNCHRONOUS_IO_NONALERT
        )
        if disposition != self._api.FILE_OPEN:
            result = self._api.ntdll.NtCreateFile(
                ctypes.byref(handle),
                desired_access,
                ctypes.byref(attributes),
                ctypes.byref(status),
                None,
                0,
                self._api.SHARE if share is None else share,
                disposition,
                options | self._api.FILE_NON_DIRECTORY_FILE,
                None,
                0,
            )
        else:
            result = self._api.ntdll.NtOpenFile(
                ctypes.byref(handle),
                desired_access,
                ctypes.byref(attributes),
                ctypes.byref(status),
                self._api.SHARE if share is None else share,
                options,
            )
        if result < 0:
            status = result & 0xFFFFFFFF
            if status == _STATUS_INVALID_PARAMETER and disposition == self._api.FILE_OPEN:
                parent_identity = self._identity(parent)[0]
                parent_canonical = self._canonical_handle(parent)
                fallback_path = Path(self._canonical_handle(parent)) / name
                self._assert_no_reparse_components(fallback_path)
                fallback_handle = self._open(
                    fallback_path,
                    desired_access,
                    self._api.OPEN_EXISTING,
                    directory=None,
                    share=share,
                )
                try:
                    if self._identity(parent)[0] != parent_identity:
                        raise OSError("relative-open parent identity changed")
                    if self._canonical_handle(parent) != parent_canonical:
                        raise OSError("relative-open parent path changed")
                    self._assert_contained_handle(fallback_handle)
                    return fallback_handle
                except BaseException:
                    self._close_or_recover(fallback_handle)
                    raise
            raise OSError(status, f"NtOpenFile failed with NTSTATUS 0x{status:08x}")
        return int(handle.value)

    def _delete_handle(self, handle: int) -> None:
        disposition = _WindowsFileDispositionEx(
            self._api.FILE_DISPOSITION_FLAG_DELETE
            | self._api.FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE
        )
        self._api.dll.SetFileInformationByHandle(
            handle,
            self._api.FILE_DISPOSITION_INFO_EX,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        )

    def _write_manifest(
        self,
        workspace: Path,
        values: dict[str, object],
        *,
        directory_handle: int | None = None,
    ) -> None:
        if directory_handle is None:
            raise OSError("manifest writes require a verified workspace directory handle")
        self._assert_contained_handle(directory_handle)
        if self._canonical_handle(directory_handle) != self._canonical(workspace):
            raise OSError("manifest directory handle does not match the workspace")
        expected_directory_identity = self._identity(directory_handle)[0]
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("ascii")
        previous_size = 0
        handle: int | None = None
        reservation_changed = False
        try:
            if self._identity(directory_handle)[0] != expected_directory_identity:
                raise OSError("workspace identity changed before manifest read")
            try:
                handle = self._open_relative(
                    directory_handle,
                    "manifest.json",
                    access=self._api.GENERIC_READ | self._api.GENERIC_WRITE,
                    disposition=self._api.FILE_OPEN_IF,
                )
            except OSError as error:
                if error.errno != _STATUS_INVALID_PARAMETER:
                    raise
                if self._identity(directory_handle)[0] != expected_directory_identity:
                    raise OSError("workspace identity changed before manifest fallback") from None
                workspace_canonical = self._canonical(workspace)
                self._assert_no_reparse_components(workspace / "manifest.json")
                handle = self._open(
                    workspace / "manifest.json",
                    self._api.GENERIC_READ | self._api.GENERIC_WRITE,
                    self._api.OPEN_ALWAYS,
                    directory=False,
                )
                try:
                    self._assert_contained_handle(handle)
                    if self._identity(directory_handle)[0] != expected_directory_identity:
                        raise OSError("workspace identity changed during manifest fallback")
                    if self._canonical_handle(directory_handle) != workspace_canonical:
                        raise OSError("workspace path changed during manifest fallback")
                    if Path(self._canonical_handle(handle)).parent != Path(workspace_canonical):
                        raise OSError("manifest handle does not belong to the workspace")
                except BaseException:
                    self._close_or_recover(handle)
                    handle = None
                    raise
            info = _WindowsFileInformation()
            self._api.dll.GetFileInformationByHandle(handle, ctypes.byref(info))
            if (
                info.attributes & self._api.FILE_ATTRIBUTE_REPARSE_POINT
                or info.attributes & self._api.FILE_ATTRIBUTE_DIRECTORY
                or info.links != 1
            ):
                raise OSError("manifest must be a private regular non-reparse file")
            _, previous_size = self._identity(handle)
            self._assert_contained_handle(handle)
            if self._identity(directory_handle)[0] != expected_directory_identity:
                raise OSError("workspace identity changed before manifest write")
            delta = len(encoded) - previous_size
            if delta > 0:
                self._reserve_workspace(workspace, delta)
            elif delta < 0:
                self._release_workspace(workspace, -delta)
            reservation_changed = True
            self._seek(handle, 0)
            self._write(handle, encoded)
            self._seek(handle, len(encoded))
            self._api.dll.SetEndOfFile(handle)
            self._seek(handle, 0)
        except Exception:
            if reservation_changed:
                if delta > 0:
                    self._release_workspace(workspace, delta)
                elif delta < 0:
                    self._reserve_workspace(workspace, -delta)
            raise
        finally:
            if handle is not None:
                self._close(handle)

    def _read_manifest(
        self, workspace: Path, *, directory_handle: int | None = None
    ) -> dict[str, object]:
        owns_directory = directory_handle is None
        directory = directory_handle or self._open(
            workspace, self._api.GENERIC_READ, self._api.OPEN_EXISTING, directory=True
        )
        handle: int | None = None
        try:
            handle = self._open_relative(
                directory,
                "manifest.json",
                access=self._api.GENERIC_READ,
                share=self._api.FILE_SHARE_READ,
            )
            info = _WindowsFileInformation()
            self._api.dll.GetFileInformationByHandle(handle, ctypes.byref(info))
            if (
                info.attributes & self._api.FILE_ATTRIBUTE_REPARSE_POINT
                or info.attributes & self._api.FILE_ATTRIBUTE_DIRECTORY
                or info.links != 1
            ):
                raise OSError("manifest must be a private regular non-reparse file")
            _, size = self._identity(handle)
            if size > 64 * 1024:
                raise OSError("manifest exceeds its quota")
            raw = self._read(handle, size)
            values = json.loads(raw.decode("ascii"))
            if not isinstance(values, dict):
                raise ValueError("manifest must be an object")
            return values
        finally:
            if handle is not None:
                self._close(handle)
            if owns_directory:
                self._close(directory)

    def _copy(
        self,
        source: int,
        destination: int,
        limit: int,
        *,
        cancellation: CancellationToken | None = None,
        deadline: float | None = None,
    ) -> tuple[str, int]:
        self._seek(source, 0)
        digest = hashlib.sha256()
        total = 0
        while True:
            _check_lifecycle(cancellation, deadline, self._clock)
            chunk = self._read(
                source,
                1024 * 1024,
                offset=total,
                cancellation=cancellation,
                deadline=deadline,
            )
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ResourceLimitExceededError("source exceeds the snapshot quota")
            self._write(
                destination,
                chunk,
                offset=total - len(chunk),
                cancellation=cancellation,
                deadline=deadline,
            )
            digest.update(chunk)
        return digest.hexdigest(), total

    def _hash(
        self,
        handle: int,
        limit: int | None = None,
        *,
        cancellation: CancellationToken | None = None,
        deadline: float | None = None,
    ) -> tuple[str, int]:
        self._seek(handle, 0)
        digest = hashlib.sha256()
        total = 0
        while True:
            _check_lifecycle(cancellation, deadline, self._clock)
            chunk = self._read(
                handle,
                1024 * 1024,
                offset=total,
                cancellation=cancellation,
                deadline=deadline,
            )
            if not chunk:
                break
            total += len(chunk)
            if limit is not None and total > limit:
                raise ResourceLimitExceededError("source exceeds the configured size limit")
            digest.update(chunk)
        return digest.hexdigest(), total

    def _read(
        self,
        handle: int,
        size: int,
        *,
        offset: int | None = None,
        cancellation: CancellationToken | None = None,
        deadline: float | None = None,
    ) -> bytes:
        if cancellation is not None or deadline is not None:
            return self._read_overlapped(handle, size, offset or 0, cancellation, deadline)
        buffer = ctypes.create_string_buffer(size)
        data = bytearray()
        while len(data) < size:
            count = wintypes.DWORD()
            remaining = size - len(data)
            if not self._api.dll.ReadFile(
                handle, ctypes.byref(buffer, len(data)), remaining, ctypes.byref(count), None
            ):
                error = ctypes.get_last_error()
                if error == _ERROR_HANDLE_EOF:
                    break
                raise OSError(error, "ReadFile failed")
            if count.value == 0:
                break
            data.extend(buffer.raw[len(data) : len(data) + count.value])
        return bytes(data)

    def _write(
        self,
        handle: int,
        data: bytes,
        *,
        offset: int | None = None,
        cancellation: CancellationToken | None = None,
        deadline: float | None = None,
    ) -> None:
        if cancellation is not None or deadline is not None:
            self._write_overlapped(handle, data, offset or 0, cancellation, deadline)
            return
        count = wintypes.DWORD()
        if not self._api.dll.WriteFile(handle, data, len(data), ctypes.byref(count), None):
            raise OSError(ctypes.get_last_error(), "WriteFile failed")
        if count.value != len(data):
            raise OSError("WriteFile made partial progress")

    def _read_overlapped(
        self,
        handle: int,
        size: int,
        offset: int,
        cancellation: CancellationToken | None,
        deadline: float | None,
    ) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        io_handle = self._duplicate(handle)
        try:
            overlapped, event = self._new_overlapped()
        except BaseException as setup_error:
            errors = self._close_handles((io_handle,))
            if errors:
                raise ExceptionGroup(
                    "overlapped I/O setup cleanup failed", [setup_error, *errors]
                ) from setup_error
            raise
        overlapped.offset = offset & 0xFFFFFFFF
        overlapped.offset_high = (offset >> 32) & 0xFFFFFFFF
        detached = False
        try:
            count = wintypes.DWORD()
            pending = not self._api.dll.ReadFile(
                io_handle,
                buffer,
                size,
                ctypes.byref(count),
                ctypes.byref(overlapped),
            )
            if pending:
                error = ctypes.get_last_error()
                if error == _ERROR_HANDLE_EOF:
                    return b""
                if error != 997:
                    raise ctypes.WinError(error)
            try:
                detached = self._wait_overlapped(
                    io_handle,
                    overlapped,
                    event,
                    count,
                    cancellation,
                    deadline,
                    buffer,
                    allow_eof=True,
                )
            except _DetachedOverlapped as error:
                detached = True
                raise error.cause from error.cause
            return buffer.raw[: count.value]
        finally:
            if not detached:
                errors = self._close_handles((event, io_handle))
                if errors:
                    raise ExceptionGroup("overlapped I/O cleanup failed", errors)

    def _write_overlapped(
        self,
        handle: int,
        data: bytes,
        offset: int,
        cancellation: CancellationToken | None,
        deadline: float | None,
    ) -> None:
        io_handle = self._duplicate(handle)
        try:
            overlapped, event = self._new_overlapped()
        except BaseException as setup_error:
            errors = self._close_handles((io_handle,))
            if errors:
                raise ExceptionGroup(
                    "overlapped I/O setup cleanup failed", [setup_error, *errors]
                ) from setup_error
            raise
        overlapped.offset = offset & 0xFFFFFFFF
        overlapped.offset_high = (offset >> 32) & 0xFFFFFFFF
        detached = False
        try:
            count = wintypes.DWORD()
            pending = not self._api.dll.WriteFile(
                io_handle,
                data,
                len(data),
                ctypes.byref(count),
                ctypes.byref(overlapped),
            )
            if pending and ctypes.get_last_error() != 997:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                detached = self._wait_overlapped(
                    io_handle,
                    overlapped,
                    event,
                    count,
                    cancellation,
                    deadline,
                    data,
                )
            except _DetachedOverlapped as error:
                detached = True
                raise error.cause from error.cause
            if count.value != len(data):
                raise OSError("WriteFile made partial progress")
        finally:
            if not detached:
                errors = self._close_handles((event, io_handle))
                if errors:
                    raise ExceptionGroup("overlapped I/O cleanup failed", errors)

    def _duplicate(self, handle: int) -> int:
        current = self._api.dll.GetCurrentProcess()
        duplicate = wintypes.HANDLE()
        if not self._api.dll.DuplicateHandle(
            current,
            handle,
            current,
            ctypes.byref(duplicate),
            0,
            False,
            self._api.DUPLICATE_SAME_ACCESS,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(duplicate.value)

    def _new_overlapped(self) -> tuple[_WindowsOverlapped, int]:
        event = self._api.dll.CreateEventW(None, True, False, None)
        if not event:
            raise ctypes.WinError(ctypes.get_last_error())
        overlapped = _WindowsOverlapped()
        overlapped.event = event
        return overlapped, event

    def _wait_overlapped(
        self,
        handle: int,
        overlapped: _WindowsOverlapped,
        event: int,
        count: wintypes.DWORD,
        cancellation: CancellationToken | None,
        deadline: float | None,
        keepalive: object,
        *,
        allow_eof: bool = False,
    ) -> bool:
        while True:
            try:
                _check_lifecycle(cancellation, deadline, self._clock)
            except (CancellationError, AsrTimeoutError) as error:
                try:
                    self._api.dll.CancelIoEx(handle, ctypes.byref(overlapped))
                except BaseException as cancel_error:
                    self._reap_overlapped(handle, overlapped, event, count, keepalive)
                    raise _DetachedOverlapped(cancel_error) from None
                drain_timeout = _CANCEL_DRAIN_TIMEOUT_MS
                if deadline is not None:
                    drain_timeout = max(
                        0, min(drain_timeout, int((deadline - self._clock.monotonic()) * 1000))
                    )
                drain_result = self._api.dll.WaitForSingleObject(event, drain_timeout)
                if drain_result == 258:
                    self._reap_overlapped(handle, overlapped, event, count, keepalive)
                    raise _DetachedOverlapped(error) from None
                if drain_result != 0:
                    self._reap_overlapped(handle, overlapped, event, count, keepalive)
                    raise _DetachedOverlapped(
                        OSError("overlapped I/O cancellation wait failed")
                    ) from None
                try:
                    self._api.dll.GetOverlappedResult(
                        handle, ctypes.byref(overlapped), ctypes.byref(count), True
                    )
                except OSError as completion_error:
                    if getattr(completion_error, "winerror", None) != 995:
                        raise
                raise
            if deadline is None:
                wait_ms = 50
            else:
                wait_ms = max(0, min(50, int((deadline - self._clock.monotonic()) * 1000)))
            result = self._api.dll.WaitForSingleObject(event, wait_ms)
            if result == 258:
                continue
            if result != 0:
                error = OSError("overlapped I/O wait failed")
                self._reap_overlapped(handle, overlapped, event, count, keepalive)
                raise _DetachedOverlapped(error) from None
            if not self._api.dll.GetOverlappedResult(
                handle, ctypes.byref(overlapped), ctypes.byref(count), True
            ):
                if allow_eof and ctypes.get_last_error() == _ERROR_HANDLE_EOF:
                    count.value = 0
                    return False
                raise ctypes.WinError(ctypes.get_last_error())
            return False

    def _reap_overlapped(
        self,
        handle: int,
        overlapped: _WindowsOverlapped,
        event: int,
        count: wintypes.DWORD,
        keepalive: object,
    ) -> None:
        def reap() -> None:
            retained = keepalive
            try:
                self._api.dll.GetOverlappedResult(
                    handle, ctypes.byref(overlapped), ctypes.byref(count), True
                )
            except OSError:
                pass
            finally:
                self._close_handles((event, handle))
                del retained
                with self._reaper_lock:
                    self._reapers.discard(current_thread())

        reaper = Thread(target=reap, name="windows-overlapped-reaper", daemon=True)
        with self._reaper_lock:
            self._reapers.add(reaper)
        reaper.start()

    def _register_lease(self, lease: _HandleLease) -> None:
        self._ensure_lease_recovery_state()
        with self._lease_recovery_lock:
            existing = self._lease_by_handle.get(lease.handle)
            if existing is not None and existing is not lease:
                raise RuntimeError("native HANDLE already has a different lease")
            self._lease_by_handle[lease.handle] = lease
            self._active_leases.add(lease)

    def _release_lease(self, lease: _HandleLease) -> None:
        self._ensure_lease_recovery_state()
        try:
            lease.close()
        except BaseException:
            with self._lease_recovery_lock:
                self._active_leases.discard(lease)
                self._failed_close_leases.add(lease)
                self._lease_by_handle[lease.handle] = lease
            self._start_lease_reaper()
            self._lease_recovery_wakeup.set()
            raise
        else:
            with self._lease_recovery_lock:
                self._active_leases.discard(lease)
                self._failed_close_leases.discard(lease)
                if self._lease_by_handle.get(lease.handle) is lease:
                    self._lease_by_handle.pop(lease.handle, None)

    def _ensure_lease_recovery_state(self) -> None:
        if not hasattr(self, "_clock"):
            self._clock = SystemMonotonicClock()
        if not hasattr(self, "_reaper_lock"):
            self._reaper_lock = Lock()
        if not hasattr(self, "_reapers"):
            self._reapers = set()
        if hasattr(self, "_lease_recovery_lock"):
            if not hasattr(self, "_lease_by_handle"):
                self._lease_by_handle = {}
            if not hasattr(self, "_active_leases"):
                self._active_leases = set()
            if not hasattr(self, "_failed_close_leases"):
                self._failed_close_leases = set()
            self._lease_recovery = self._failed_close_leases
            return
        self._lease_recovery_lock = Lock()
        self._active_leases = set()
        self._failed_close_leases = set()
        self._lease_recovery = self._failed_close_leases
        self._lease_by_handle: dict[int, _HandleLease] = {}
        self._lease_recovery_wakeup = Event()
        self._lease_reaper = None
        self._lease_shutdown_deadline = None

    def _close_or_recover(self, handle: int) -> None:
        lease = _HandleLease(self._api, handle)
        self._register_lease(lease)
        self._release_lease(lease)

    def _close_find_or_recover(self, handle: int) -> None:
        lease = _FindHandleLease(self._api, handle)
        self._register_lease(lease)
        self._release_lease(lease)

    def _close_handles(self, handles: tuple[int | None, ...]) -> list[BaseException]:
        errors: list[BaseException] = []
        for handle in handles:
            if handle is None:
                continue
            try:
                self._close_or_recover(handle)
            except BaseException as error:
                errors.append(error)
        return errors

    def _lease_recovery_loop(self) -> None:
        while True:
            with self._lease_recovery_lock:
                leases = tuple(self._failed_close_leases)
                shutdown_deadline = self._lease_shutdown_deadline
            if not leases:
                return
            if shutdown_deadline is not None and self._clock.monotonic() >= shutdown_deadline:
                return
            for lease in leases:
                try:
                    lease.close()
                except BaseException:
                    continue
                with self._lease_recovery_lock:
                    self._failed_close_leases.discard(lease)
                    if self._lease_by_handle.get(lease.handle) is lease:
                        self._lease_by_handle.pop(lease.handle, None)
            wait_seconds = 0.05
            if shutdown_deadline is not None:
                wait_seconds = min(
                    wait_seconds,
                    max(0.0, shutdown_deadline - self._clock.monotonic()),
                )
            self._lease_recovery_wakeup.wait(wait_seconds)
            self._lease_recovery_wakeup.clear()

    def _start_lease_reaper(self) -> None:
        with self._lease_recovery_lock:
            if not self._failed_close_leases:
                return
            reaper = self._lease_reaper
            if reaper is not None and reaper.is_alive():
                return
            self._lease_reaper = Thread(
                target=self._lease_recovery_loop,
                name="windows-handle-lease-reaper",
                daemon=True,
            )
            self._lease_reaper.start()

    def _seek(self, handle: int, offset: int) -> None:
        result = ctypes.c_longlong()
        if not self._api.dll.SetFilePointerEx(
            handle, offset, ctypes.byref(result), self._api.FILE_BEGIN
        ):
            raise OSError(ctypes.get_last_error(), "SetFilePointerEx failed")

    def _close(self, handle: int) -> None:
        self._close_or_recover(handle)

    def _contained_path(self, path: Path, root: Path) -> bool:
        canonical = self._canonical(path)
        return canonical == self._root_canonical or canonical.startswith(
            self._root_canonical + "\\"
        )

    def _contained(self, path: str, root: str) -> bool:
        return path == root or path.startswith(root + "\\")

    def _assert_no_reparse_components(self, path: Path, *, deadline: float | None = None) -> None:
        current = path.absolute()
        components: list[Path] = []
        while current != Path(current.anchor):
            components.append(current)
            current = current.parent
        components.append(Path(current.anchor))
        for component in reversed(components):
            _check_cleanup_deadline(deadline, self._clock)
            handle = self._open(
                component,
                self._api.GENERIC_READ,
                self._api.OPEN_EXISTING,
                directory=None,
            )
            self._close(handle)

    @staticmethod
    def _is_unc(path: Path) -> bool:
        value = str(path).casefold()
        return value.startswith("\\\\?\\unc\\") or (
            value.startswith("\\\\") and not value.startswith("\\\\?\\")
        )

    def _scan_workspace_usage(self) -> tuple[dict[Path, int], dict[Path, int]]:
        usage: dict[Path, int] = {}
        snapshots: dict[Path, int] = {}
        for job in self.root.iterdir():
            job_handle = self._open(
                job, self._api.GENERIC_READ, self._api.OPEN_EXISTING, directory=True
            )
            try:
                self._assert_contained_handle(job_handle)
            finally:
                self._close(job_handle)
            for attempt in job.iterdir():
                attempt_handle = self._open(
                    attempt, self._api.GENERIC_READ, self._api.OPEN_EXISTING, directory=True
                )
                try:
                    self._assert_contained_handle(attempt_handle)
                finally:
                    self._close(attempt_handle)
                usage[attempt] = 0

                def scan(directory: Path, attempt_path: Path = attempt) -> None:
                    for child in directory.iterdir():
                        child_handle = self._open(
                            child,
                            self._api.GENERIC_READ,
                            self._api.OPEN_EXISTING,
                            directory=None,
                        )
                        try:
                            self._assert_contained_handle(child_handle)
                            _, size = self._identity(child_handle)
                            info = _WindowsFileInformation()
                            if not self._api.dll.GetFileInformationByHandle(
                                child_handle, ctypes.byref(info)
                            ):
                                raise OSError(
                                    ctypes.get_last_error(),
                                    "GetFileInformationByHandle failed",
                                )
                        finally:
                            self._close(child_handle)
                        if info.attributes & self._api.FILE_ATTRIBUTE_DIRECTORY:
                            scan(child)
                        elif child == attempt_path / "source.snapshot":
                            snapshots[attempt_path] = size
                        else:
                            usage[attempt_path] += size

                scan(attempt)
        return usage, snapshots

    def _assert_contained_handle(self, handle: int) -> None:
        canonical = self._canonical_handle(handle)
        if not self._contained(canonical, self._root_canonical):
            raise OSError("handle is outside the private workspace root")


class _HandleLease:
    def __init__(self, api: WindowsKernel32, handle: int) -> None:
        self._api = api
        self._handle = handle
        self._closed = False
        self._lock = Lock()

    @property
    def handle(self) -> int:
        return self._handle

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if not self._api.dll.CloseHandle(self._handle):
                win_error = getattr(ctypes, "WinError", None)
                if win_error is not None:
                    raise win_error(ctypes.get_last_error())
                raise OSError("CloseHandle failed")
            self._closed = True


class _FindHandleLease(_HandleLease):
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if not self._api.dll.FindClose(self._handle):
                win_error = getattr(ctypes, "WinError", None)
                if win_error is not None:
                    raise win_error(ctypes.get_last_error())
                raise OSError("FindClose failed")
            self._closed = True


class NativeHandleLeaseRegistry:
    """Own native leases and retry failed close operations on a daemon reaper."""

    def __init__(self, *, clock: MonotonicClock | None = None) -> None:
        self._clock = clock or SystemMonotonicClock()
        self._lock = Lock()
        self._active: set[_HandleLease] = set()
        self._recovery: set[_HandleLease] = set()
        self._reaper: Thread | None = None

    @property
    def recovery(self) -> set[_HandleLease]:
        with self._lock:
            return set(self._recovery)

    def register(self, lease: _HandleLease) -> None:
        with self._lock:
            self._active.add(lease)

    def register_handle(self, handle: int, close: Callable[[int], object]) -> _HandleLease:
        lease = _CallableHandleLease(handle, close)
        self.register(lease)
        return lease

    def release(self, lease: _HandleLease) -> None:
        try:
            lease.close()
        except BaseException:
            with self._lock:
                self._active.discard(lease)
                self._recovery.add(lease)
            self._start_reaper()
            raise
        with self._lock:
            self._active.discard(lease)
            self._recovery.discard(lease)

    def close(self, timeout: float = 1.0) -> None:
        deadline = self._clock.monotonic() + timeout
        with self._lock:
            active = tuple(self._active)
        for lease in active:
            try:
                self.release(lease)
            except BaseException:
                continue
        self._start_reaper()
        with self._lock:
            reaper = self._reaper
        if reaper is not None:
            reaper.join(max(0.0, deadline - self._clock.monotonic()))
        with self._lock:
            pending = bool(self._active or self._recovery)
        if pending:
            raise TimeoutError("native handle lease recovery did not finish before shutdown")

    def _start_reaper(self) -> None:
        with self._lock:
            if not self._recovery:
                return
            if self._reaper is not None and self._reaper.is_alive():
                return

            def reap() -> None:
                while True:
                    with self._lock:
                        leases = tuple(self._recovery)
                    if not leases:
                        return
                    for lease in leases:
                        try:
                            lease.close()
                        except BaseException:
                            continue
                        with self._lock:
                            self._recovery.discard(lease)
                    self._clock.sleep(0.05)

            self._reaper = Thread(target=reap, name="native-handle-lease-reaper", daemon=True)
            self._reaper.start()


class _CallableHandleLease(_HandleLease):
    def __init__(self, handle: int, close: Callable[[int], object]) -> None:
        self._handle = handle
        self._close_function = close
        self._closed = False
        self._lock = Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            result = self._close_function(self._handle)
            if result is False:
                win_error = getattr(ctypes, "WinError", None)
                if win_error is not None:
                    raise win_error(ctypes.get_last_error())
                raise OSError("native handle close failed")
            self._closed = True


class _VerifiedSnapshotHandle:
    """Revalidate a held snapshot handle immediately before FFmpeg starts."""

    def __init__(
        self,
        store: WindowsMediaSnapshotStore,
        handle: int,
        identity: str,
        size: int,
        digest: str,
    ) -> None:
        self._store = store
        self._handle = handle
        self._identity = identity
        self._size = size
        self._digest = digest

    def __call__(
        self,
        *,
        cancellation: CancellationToken | None = None,
        deadline: float | None = None,
    ) -> None:
        _check_lifecycle(cancellation, deadline, self._store._clock)
        identity, size = self._store._identity(self._handle)
        if (identity, size) != (self._identity, self._size):
            raise SourceChangedError("snapshot identity changed before normalization")
        digest, actual_size = self._store._hash(
            self._handle,
            self._store.max_snapshot_bytes,
            cancellation=cancellation,
            deadline=deadline,
        )
        self._store._seek(self._handle, 0)
        if (digest, actual_size) != (self._digest, self._size):
            raise SourceChangedError("snapshot hash changed before normalization")


def create_media_snapshot_store(root: Path, **kwargs):
    """Compose the platform-native store without hiding platform selection."""
    if os.name == "nt":
        return WindowsMediaSnapshotStore(root, **kwargs)
    return LocalMediaSnapshotStore(root, **kwargs)
