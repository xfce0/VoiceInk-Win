"""Injected process runner for bounded FFmpeg execution."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from threading import Event, Lock, Thread
from typing import Protocol

from voiceink_win.domain import CancellationToken, ResourceLimitExceededError

from .process import _process_is_alive, _run_process_reaper


def _set_signature(function, argtypes, restype) -> None:
    function.argtypes = argtypes
    function.restype = restype


def _check_bool(result, function, arguments):
    if not result:
        get_last_error = getattr(ctypes, "get_last_error", lambda: 1)
        raise ctypes.WinError(get_last_error())
    return result


def _check_handle(result, function, arguments):
    value = getattr(result, "value", result)
    if value in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def _last_error() -> int:
    return getattr(ctypes, "get_last_error", lambda: 1)()


class SystemMonotonicClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class OutputSink(Protocol):
    def write(self, chunk: bytes) -> None: ...

    def bytes(self) -> bytes | memoryview: ...


class BoundedOutputSink:
    def __init__(self, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("output quota must be positive")
        self._max_bytes = max_bytes
        self._data = bytearray()

    def write(self, chunk: bytes) -> None:
        if len(self._data) + len(chunk) > self._max_bytes:
            raise ResourceLimitExceededError("process output exceeds the configured quota")
        self._data.extend(chunk)

    def bytes(self) -> memoryview:
        return memoryview(self._data)


class ProcessTimedOut(Exception):
    pass


class ProcessCancelled(Exception):
    pass


@dataclass(slots=True)
class _ProcessGeneration:
    number: int
    done: Event = field(default_factory=Event)
    process: subprocess.Popen | None = None
    reaper_owned: bool = False
    reaper_started: bool = False
    pending_job: WindowsJobObject | None = None
    observed_job: WindowsJobObject | None = None


def _kill_windows_tree(process: subprocess.Popen) -> None:
    if sys.platform != "win32":
        return
    from .process import SubprocessSupervisor

    SubprocessSupervisor._kill_windows_tree(process)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    stdout: bytes | memoryview
    stderr: bytes
    returncode: int


class ProcessRunner(Protocol):
    def run(
        self,
        argv: list[str],
        *,
        timeout: float,
        deadline: float | None = None,
        cancellation: CancellationToken,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        stdout_sink: OutputSink | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> ProcessResult: ...


class WindowsProcessTreeAdapter(Protocol):
    """Explicit seam for a Windows Job Object implementation."""

    def run(
        self,
        argv: list[str],
        *,
        timeout: float,
        deadline: float | None = None,
        cancellation: CancellationToken,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        stdout_sink: OutputSink | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> ProcessResult: ...


class SubprocessRunner:
    """Run argv without a shell and terminate the complete POSIX process group."""

    process_tree_mode = "windows-job-object-adapter" if os.name == "nt" else "posix-process-group"

    def __init__(
        self,
        windows_adapter: WindowsProcessTreeAdapter | None = None,
        *,
        clock: object | None = None,
    ) -> None:
        self._clock = clock or SystemMonotonicClock()
        self._active_process: subprocess.Popen | None = None
        self._active_lock = Lock()
        self._generation = 0
        self._run_generation: _ProcessGeneration | None = None
        self._reaper_done = Event()
        self._reaper_done.set()
        self._windows_adapter = (
            windows_adapter
            if windows_adapter is not None or os.name != "nt"
            else WindowsJobObjectProcessRunner(clock=self._clock)
        )

    def run(
        self,
        argv: list[str],
        *,
        timeout: float,
        deadline: float | None = None,
        cancellation: CancellationToken,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        stdout_sink: OutputSink | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> ProcessResult:
        if deadline is not None and self._clock.monotonic() >= deadline:
            raise ProcessTimedOut
        if self._windows_adapter is not None:
            return self._windows_adapter.run(
                argv,
                timeout=timeout,
                deadline=deadline,
                cancellation=cancellation,
                max_stdout_bytes=max_stdout_bytes,
                max_stderr_bytes=max_stderr_bytes,
                stdout_sink=stdout_sink,
                pass_fds=pass_fds,
            )
        return self._run_native(
            argv,
            timeout=timeout,
            deadline=deadline,
            cancellation=cancellation,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            stdout_sink=stdout_sink,
            pass_fds=pass_fds,
        )

    def _run_native(
        self,
        argv: list[str],
        *,
        timeout: float,
        deadline: float | None,
        cancellation: CancellationToken,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        stdout_sink: OutputSink | None,
        pass_fds: tuple[int, ...],
    ) -> ProcessResult:
        kwargs: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
        }
        if os.name != "nt":
            kwargs["start_new_session"] = True
            kwargs["pass_fds"] = pass_fds
        if deadline is not None and self._clock.monotonic() >= deadline:
            raise ProcessTimedOut
        operation_deadline = deadline if deadline is not None else self._clock.monotonic() + timeout
        generation = self._begin_generation(operation_deadline)
        try:
            process = subprocess.Popen(argv, **kwargs)
        except BaseException:
            self._finish_generation(generation)
            raise
        generation.process = process
        with self._active_lock:
            self._active_process = process
        try:
            return self._collect_process(
                process,
                timeout=timeout,
                deadline=deadline,
                cancellation=cancellation,
                max_stdout_bytes=max_stdout_bytes,
                max_stderr_bytes=max_stderr_bytes,
                stdout_sink=stdout_sink,
            )
        finally:
            with self._active_lock:
                reaped = generation.reaper_owned
                if self._active_process is process and not reaped:
                    self._active_process = None
            if not reaped:
                self._finish_generation(generation)

    def interrupt(self) -> None:
        with self._active_lock:
            process = self._active_process
        if process is not None:
            self._terminate_process(process, self._clock.monotonic() + 1.0)

    def reaper_owns_process(self) -> bool:
        with self._active_lock:
            generation = self._run_generation
            process = generation.process if generation is not None else None
            owned = generation is not None and generation.reaper_owned
        if not owned:
            return False
        try:
            return process.poll() is None
        except BaseException:
            return True

    @property
    def reaper_done(self) -> Event:
        with self._active_lock:
            generation = self._run_generation
            return generation.done if generation is not None else self._reaper_done

    def _collect_process(
        self,
        process: subprocess.Popen,
        *,
        timeout: float,
        deadline: float | None = None,
        cancellation: CancellationToken,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        stdout_sink: OutputSink | None,
    ) -> ProcessResult:
        stdout: list[bytes] = []
        stderr: list[bytes] = []

        overflow = Event()

        def drain(stream, target: list[bytes], limit: int, sink: OutputSink | None = None) -> None:
            captured = 0
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                if captured + len(chunk) > limit:
                    overflow.set()
                    return
                if sink is not None:
                    try:
                        sink.write(chunk)
                    except ResourceLimitExceededError:
                        overflow.set()
                        return
                if sink is None:
                    target.append(chunk)
                captured += len(chunk)

        threads = [
            Thread(
                target=drain,
                args=(process.stdout, stdout, max_stdout_bytes, stdout_sink),
                daemon=True,
            ),
            Thread(target=drain, args=(process.stderr, stderr, max_stderr_bytes), daemon=True),
        ]
        for thread in threads:
            thread.start()
        operation_deadline = deadline if deadline is not None else self._clock.monotonic() + timeout
        try:
            while process.poll() is None:
                if overflow.is_set():
                    self._terminate_process(process, operation_deadline)
                    raise ResourceLimitExceededError("FFmpeg output exceeds the configured quota")
                if cancellation.is_cancelled():
                    self._terminate_process(process, operation_deadline)
                    raise ProcessCancelled
                if self._clock.monotonic() >= operation_deadline:
                    self._terminate_process(process, operation_deadline)
                    raise ProcessTimedOut
                self._clock.sleep(0.01)
            try:
                process.wait()
            except BaseException:
                if _process_is_alive(process):
                    self._start_process_reaper(process)
                raise
            if overflow.is_set():
                raise ResourceLimitExceededError("FFmpeg output exceeds the configured quota")
        finally:
            for thread in threads:
                thread.join(timeout=max(0.0, operation_deadline - self._clock.monotonic()))
        stdout_bytes = b"" if stdout_sink is not None else b"".join(stdout)
        return ProcessResult(stdout_bytes, b"".join(stderr), process.returncode or 0)

    def _terminate_process(self, process: subprocess.Popen, deadline: float) -> None:
        adapter = self._windows_adapter
        if adapter is not None and hasattr(adapter, "terminate"):
            adapter.terminate(process, deadline)
            return
        self._terminate(process, deadline)

    def _terminate(self, process: subprocess.Popen, deadline: float) -> None:
        if os.name == "nt":
            process.terminate()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=max(0.0, deadline - self._clock.monotonic()))
        except (TimeoutError, subprocess.TimeoutExpired):
            if os.name == "nt":
                try:
                    process.kill()
                except BaseException:
                    if _process_is_alive(process):
                        self._start_process_reaper(process)
                    raise
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except BaseException:
                    if _process_is_alive(process):
                        self._start_process_reaper(process)
                    raise
            remaining = max(0.0, deadline - self._clock.monotonic())
            if remaining:
                try:
                    process.wait(timeout=remaining)
                except (TimeoutError, subprocess.TimeoutExpired):
                    if _process_is_alive(process):
                        self._start_process_reaper(process)
            else:
                if _process_is_alive(process):
                    self._start_process_reaper(process)
        except BaseException:
            if _process_is_alive(process):
                self._start_process_reaper(process)
            raise

    def _start_process_reaper(self, process: subprocess.Popen) -> None:
        with self._active_lock:
            generation = self._run_generation
            if generation is None:
                self._generation += 1
                generation = _ProcessGeneration(self._generation, process=process)
                self._run_generation = generation
                self._reaper_done.clear()
            elif generation.process is not process:
                raise RuntimeError("process reaper has no matching run generation")
            if generation.reaper_started:
                return
            generation.reaper_started = True
            generation.reaper_owned = True
            self._active_process = process
        _run_process_reaper(
            process,
            on_done=lambda: self._finish_process_reaper(generation),
            sleep=self._clock.sleep,
        )

    def _finish_process_reaper(self, generation: _ProcessGeneration) -> None:
        with self._active_lock:
            generation.reaper_owned = False
            process = generation.process
            if self._active_process is process and self._run_generation is generation:
                self._active_process = None
            if self._run_generation is generation:
                self._run_generation = None
        generation.done.set()
        self._reaper_done.set()

    def _begin_generation(self, deadline: float) -> _ProcessGeneration:
        with self._active_lock:
            previous = self._run_generation
        if previous is not None and not previous.done.wait(
            max(0.0, deadline - self._clock.monotonic())
        ):
            raise ProcessTimedOut
        with self._active_lock:
            if self._run_generation is not None:
                if self._run_generation is not previous or not self._run_generation.done.is_set():
                    raise ProcessTimedOut
                self._run_generation = None
            self._generation += 1
            generation = _ProcessGeneration(self._generation)
            self._run_generation = generation
            self._reaper_done.clear()
            return generation

    def _finish_generation(self, generation: _ProcessGeneration) -> None:
        with self._active_lock:
            if self._run_generation is generation:
                self._run_generation = None
                if self._active_process is generation.process:
                    self._active_process = None
        generation.done.set()
        self._reaper_done.set()


class WindowsJobObjectProcessRunner:
    """Contain FFmpeg and descendants in a kill-on-close Job Object.

    The process starts suspended so the Job Object is assigned before any
    descendant can escape containment. The primary thread is resumed only
    after assignment succeeds.
    """

    def __init__(
        self,
        *,
        clock: object | None = None,
        max_processes: int | None = 4,
        max_process_memory_bytes: int | None = 768 * 1024**2,
        kernel32: object | None = None,
    ) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows Job Objects are only available on Windows")
        self._clock = clock or SystemMonotonicClock()
        self._max_processes = max_processes
        self._max_process_memory_bytes = max_process_memory_bytes
        self._kernel32 = kernel32
        self._reaper_done = Event()
        self._reaper_done.set()
        self._generation = 0
        self._run_generation: _ProcessGeneration | None = None
        self._reaper_process: subprocess.Popen | None = None
        self._job_recovery: set[WindowsJobObject] = set()
        self._job_recovery_lock = Lock()
        self._job_reaper: Thread | None = None

    def run(
        self,
        argv: list[str],
        *,
        timeout: float,
        deadline: float | None = None,
        cancellation: CancellationToken,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        stdout_sink: OutputSink | None = None,
        pass_fds: tuple[int, ...] = (),
    ) -> ProcessResult:
        del pass_fds
        if deadline is not None and self._clock.monotonic() >= deadline:
            raise ProcessTimedOut
        operation_deadline = deadline if deadline is not None else self._clock.monotonic() + timeout
        generation = self._begin_generation(operation_deadline)
        try:
            job = WindowsJobObject(
                max_processes=self._max_processes,
                max_process_memory_bytes=self._max_process_memory_bytes,
                kernel32=self._kernel32,
            )
        except BaseException:
            self._finish_generation_if_ready(generation)
            raise
        process = None
        cleanup_required = True
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=0x00000004,  # CREATE_SUSPENDED
            )
            generation.process = process
            job.assign(process)
            _resume_suspended_process(process.pid)
            collector = SubprocessRunner(
                windows_adapter=_WindowsJobRunAdapter(job, self._clock), clock=self._clock
            )
            result = collector._collect_process(
                process,
                timeout=timeout,
                deadline=deadline,
                cancellation=cancellation,
                max_stdout_bytes=max_stdout_bytes,
                max_stderr_bytes=max_stderr_bytes,
                stdout_sink=stdout_sink,
            )
            cleanup_required = False
            return result
        finally:
            cleanup_errors: list[BaseException] = []
            try:
                if process is not None and cleanup_required:
                    self._terminate_failed_process(job, process, deadline, generation)
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                if not self._attach_pending_job(generation, job):
                    job.close()
            except BaseException as error:
                self._retain_job(job, generation)
                cleanup_errors.append(error)
            self._finish_generation_if_ready(generation)
            if cleanup_errors:
                raise ExceptionGroup("Windows process cleanup failed", cleanup_errors)

    def _terminate_failed_process(
        self,
        job: WindowsJobObject,
        process: subprocess.Popen,
        deadline: float | None,
        generation: _ProcessGeneration,
    ) -> None:
        """Fence every failed collection before releasing the Job Object handle."""
        termination_error: BaseException | None = None
        try:
            job.terminate()
        except BaseException as error:
            termination_error = error

        cleanup_deadline = deadline if deadline is not None else self._clock.monotonic() + 1.0
        try:
            process.wait(timeout=max(0.0, cleanup_deadline - self._clock.monotonic()))
        except (TimeoutError, subprocess.TimeoutExpired):
            try:
                process.kill()
                remaining = max(0.0, cleanup_deadline - self._clock.monotonic())
                if remaining:
                    process.wait(timeout=remaining)
                else:
                    self._start_process_reaper(process, generation, pending_job=job)
            except BaseException as error:
                if termination_error is None:
                    termination_error = error
                if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
                    self._start_process_reaper(process, generation, pending_job=job)
        except BaseException as error:
            if termination_error is None:
                termination_error = error
            if _process_is_alive(process):
                self._start_process_reaper(process, generation, pending_job=job)

        if _process_is_alive(process):
            self._start_process_reaper(process, generation, pending_job=job)

        if termination_error is not None:
            raise termination_error

    @property
    def reaper_done(self) -> Event:
        with self._job_recovery_lock:
            generation = self._run_generation
            return generation.done if generation is not None else self._reaper_done

    def reaper_owns_process(self) -> bool:
        with self._job_recovery_lock:
            generation = self._run_generation
            process = generation.process if generation is not None else None
            owned = generation is not None and generation.reaper_owned
        if process is None:
            return False
        try:
            return owned and process.poll() is None
        except BaseException:
            return True

    def _start_process_reaper(
        self,
        process: subprocess.Popen,
        generation: _ProcessGeneration | None = None,
        *,
        pending_job: WindowsJobObject | None = None,
    ) -> None:
        with self._job_recovery_lock:
            generation = generation or self._run_generation
            if generation is None or generation.process is not process:
                raise RuntimeError("process reaper has no matching run generation")
            if generation.reaper_started:
                if pending_job is not None and not generation.done.is_set():
                    generation.pending_job = pending_job
                return
            generation.pending_job = pending_job
            generation.reaper_started = True
            generation.reaper_owned = True
            self._reaper_process = process
        _run_process_reaper(
            process,
            on_done=lambda: self._finish_process_reaper(generation),
            sleep=self._clock.sleep,
        )

    def _finish_process_reaper(self, generation: _ProcessGeneration) -> None:
        with self._job_recovery_lock:
            pending_job = generation.pending_job
            generation.pending_job = None
            generation.observed_job = pending_job
            generation.reaper_owned = False
            if self._reaper_process is generation.process:
                self._reaper_process = None
        if pending_job is not None:
            try:
                pending_job.close()
            except BaseException:
                self._retain_job(pending_job)
        self._finish_generation_if_ready(generation)

    def _attach_pending_job(self, generation: _ProcessGeneration, job: WindowsJobObject) -> bool:
        """Attach a job while holding the same lock observed by the reaper."""
        with self._job_recovery_lock:
            if generation.pending_job is job or generation.observed_job is job:
                return True
            if not generation.reaper_owned or generation.done.is_set():
                return False
            generation.pending_job = job
            return True

    def _retain_job(
        self, job: WindowsJobObject, generation: _ProcessGeneration | None = None
    ) -> None:
        del generation
        with self._job_recovery_lock:
            self._job_recovery.add(job)
        self._start_job_reaper()

    def _start_job_reaper(self) -> None:
        with self._job_recovery_lock:
            if not self._job_recovery:
                return
            if self._job_reaper is not None and self._job_reaper.is_alive():
                return

            def reap() -> None:
                while True:
                    with self._job_recovery_lock:
                        jobs = tuple(self._job_recovery)
                    if not jobs:
                        return
                    for job in jobs:
                        try:
                            job.close()
                        except BaseException:
                            continue
                        with self._job_recovery_lock:
                            self._job_recovery.discard(job)
                    self._clock.sleep(0.05)

            self._job_reaper = Thread(target=reap, name="ffmpeg-job-cleanup-reaper", daemon=True)
            self._job_reaper.start()

    def _begin_generation(self, deadline: float) -> _ProcessGeneration:
        with self._job_recovery_lock:
            previous = self._run_generation
        if previous is not None and not previous.done.wait(
            max(0.0, deadline - self._clock.monotonic())
        ):
            raise ProcessTimedOut
        with self._job_recovery_lock:
            if self._run_generation is not None:
                if self._run_generation is not previous or not self._run_generation.done.is_set():
                    raise ProcessTimedOut
                self._run_generation = None
            self._generation += 1
            generation = _ProcessGeneration(self._generation)
            self._run_generation = generation
            self._reaper_done.clear()
            return generation

    def _finish_generation_if_ready(self, generation: _ProcessGeneration) -> None:
        with self._job_recovery_lock:
            if generation.reaper_owned:
                return
            if self._run_generation is generation:
                self._run_generation = None
                self._reaper_process = None
        generation.done.set()
        self._reaper_done.set()


def _resume_suspended_process(pid: int) -> None:
    """Resume the only primary thread of a process created suspended."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    from .windows_snapshot import NativeHandleLeaseRegistry

    native_handles = NativeHandleLeaseRegistry()

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("thread_id", wintypes.DWORD),
            ("owner_process_id", wintypes.DWORD),
            ("base_priority", wintypes.LONG),
            ("delta_priority", wintypes.LONG),
            ("flags", wintypes.DWORD),
        ]

    invalid_handle = ctypes.c_void_p(-1).value
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot in (None, invalid_handle):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        entry = ThreadEntry()
        entry.size = ctypes.sizeof(ThreadEntry)
        found = False
        if kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            while True:
                if entry.owner_process_id == pid:
                    thread = kernel32.OpenThread(0x0002, False, entry.thread_id)
                    if thread in (None, invalid_handle):
                        raise ctypes.WinError(ctypes.get_last_error())
                    native_handles.register_handle(thread, kernel32.CloseHandle)
                    if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                    found = True
                    break
                if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                    break
        if not found:
            raise OSError("suspended process primary thread was not found")
    finally:
        native_handles.register_handle(snapshot, kernel32.CloseHandle)
        native_handles.close()


class _WindowsJobRunAdapter:
    def __init__(self, job: WindowsJobObject, clock: object | None = None) -> None:
        self._job = job
        self._clock = clock or SystemMonotonicClock()

    def terminate(self, process: subprocess.Popen, deadline: float) -> None:
        self._job.terminate()
        try:
            process.wait(timeout=max(0.0, deadline - self._clock.monotonic()))
        except (TimeoutError, subprocess.TimeoutExpired):
            pass


class _WindowsBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _WindowsIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _WindowsExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _WindowsBasicLimitInformation),
        ("IoInfo", _WindowsIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsJobObject:
    _KILL_ON_JOB_CLOSE = 0x2000
    _ACTIVE_PROCESS_LIMIT = 0x00000008
    _PROCESS_MEMORY_LIMIT = 0x00000100
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    def __init__(
        self,
        *,
        max_processes: int | None = 4,
        max_process_memory_bytes: int | None = 768 * 1024**2,
        kernel32: object | None = None,
    ) -> None:
        if max_processes is not None and max_processes < 1:
            raise ValueError("max_processes must be positive")
        if max_process_memory_bytes is not None and max_process_memory_bytes < 1:
            raise ValueError("max_process_memory_bytes must be positive")
        self._kernel32 = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
        from .windows_snapshot import NativeHandleLeaseRegistry

        self._native_handles = NativeHandleLeaseRegistry()
        _set_signature(
            self._kernel32.CreateJobObjectW,
            [wintypes.LPVOID, wintypes.LPCWSTR],
            wintypes.HANDLE,
        )
        self._kernel32.CreateJobObjectW.errcheck = _check_handle
        _set_signature(
            self._kernel32.SetInformationJobObject,
            [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD],
            wintypes.BOOL,
        )
        _set_signature(
            self._kernel32.AssignProcessToJobObject,
            [wintypes.HANDLE, wintypes.HANDLE],
            wintypes.BOOL,
        )
        _set_signature(
            self._kernel32.TerminateJobObject, [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL
        )
        _set_signature(self._kernel32.CloseHandle, [wintypes.HANDLE], wintypes.BOOL)
        self._kernel32.SetInformationJobObject.errcheck = _check_bool
        self._kernel32.AssignProcessToJobObject.errcheck = _check_bool
        self._kernel32.TerminateJobObject.errcheck = _check_bool
        self._kernel32.CloseHandle.errcheck = _check_bool
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError(_last_error(), "CreateJobObjectW failed")
        self._handle_lease = self._native_handles.register_handle(
            self._handle, self._kernel32.CloseHandle
        )
        limits = _WindowsExtendedLimitInformation()
        flags = self._KILL_ON_JOB_CLOSE
        if max_processes is not None:
            flags |= self._ACTIVE_PROCESS_LIMIT
            limits.BasicLimitInformation.ActiveProcessLimit = max_processes
        if max_process_memory_bytes is not None:
            flags |= self._PROCESS_MEMORY_LIMIT
            limits.ProcessMemoryLimit = max_process_memory_bytes
        limits.BasicLimitInformation.LimitFlags = flags
        try:
            configured = self._kernel32.SetInformationJobObject(
                self._handle,
                self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            )
        except BaseException:
            try:
                self.close()
            except BaseException:
                pass
            raise
        if not configured:
            try:
                self.close()
            finally:
                raise OSError(_last_error(), "SetInformationJobObject failed")

    def assign(self, process: subprocess.Popen) -> None:
        if not self._kernel32.AssignProcessToJobObject(self._handle, process._handle):
            raise OSError(_last_error(), "AssignProcessToJobObject failed")

    def terminate(self) -> None:
        if self._handle:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if getattr(self, "_handle", None):
            self._native_handles.release(self._handle_lease)
            self._handle = None
