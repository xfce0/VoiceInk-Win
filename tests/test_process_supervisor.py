from __future__ import annotations

import hashlib
import os
import signal
import time
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

import voiceink_win.infrastructure.media_process as media_process_module
import voiceink_win.infrastructure.process as process_module
from voiceink_win.domain import ConfigurationError, MissingModelError
from voiceink_win.infrastructure import (
    FakeClock,
    RuntimeArtifactManifest,
    RuntimeArtifactVerifier,
    SubprocessConfig,
    SubprocessSupervisor,
)


class FakeProbe:
    def ready(self, timeout: float) -> bool:
        return timeout > 0


class FakeProcess:
    pid = 12345

    def __init__(self) -> None:
        self.running = True
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self.running else 0

    def terminate(self) -> None:
        self.terminated = True
        self.running = False

    def kill(self) -> None:
        self.killed = True
        self.running = False

    def wait(self, timeout: float | None = None) -> int:
        self.running = False
        return 0


def force_posix_os(monkeypatch, module) -> None:
    if os.name != "nt":
        return

    real_os = os

    class PosixOsProxy:
        name = "posix"

        def __getattr__(self, name):
            return getattr(real_os, name)

    monkeypatch.setattr(module, "os", PosixOsProxy())

    class PosixSignalProxy:
        SIGTERM = signal.SIGTERM
        SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

        def __getattr__(self, name):
            return getattr(signal, name)

    if module is media_process_module:
        monkeypatch.setattr(module, "signal", PosixSignalProxy())


def write_artifact(path: Path, content: bytes) -> str:
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def manifest(path: Path, digest: str, *, version: str = "1") -> RuntimeArtifactManifest:
    return RuntimeArtifactManifest(
        version, "https://example.invalid/artifact", digest, "Apache-2.0", path
    )


def test_artifact_verifier_checks_executable_and_model_sha256(tmp_path: Path) -> None:
    executable = tmp_path / "sidecar"
    model = tmp_path / "model.gguf"
    executable_hash = write_artifact(executable, b"executable")
    model_hash = write_artifact(model, b"model")
    executable.chmod(0o755)
    verifier = RuntimeArtifactVerifier()

    verifier.verify_executable(executable, executable_hash)
    verifier.verify_model(model, model_hash)

    with pytest.raises(ConfigurationError):
        verifier.verify(model, "0" * 64, label="model")


def test_artifact_verifier_reads_runtime_artifacts_in_binary_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = tmp_path / "sidecar.exe"
    content = b"MZ\r\nexecutable\r\n"
    expected_hash = write_artifact(artifact, content)
    real_open = os.open
    captured_flags: list[int] = []
    binary_flag = 0x8000

    def open_binary(path, flags):
        captured_flags.append(flags)
        if os.name == "nt":
            return real_open(path, flags)
        return real_open(path, flags & ~binary_flag)

    monkeypatch.setattr(process_module.os, "O_BINARY", binary_flag, raising=False)
    monkeypatch.setattr(process_module.os, "open", open_binary)

    assert RuntimeArtifactVerifier().sha256(artifact) == expected_hash
    assert len(captured_flags) == 1
    assert captured_flags[0] & binary_flag


@pytest.mark.skipif(os.name != "nt", reason="covers Windows CRT binary mode")
def test_artifact_verifier_preserves_windows_binary_artifact_bytes(tmp_path: Path) -> None:
    artifact = tmp_path / "sidecar.exe"
    content = b"MZ\r\nexecutable\r\n"
    expected_hash = write_artifact(artifact, content)

    assert RuntimeArtifactVerifier().sha256(artifact) == expected_hash


def test_artifact_verifier_distinguishes_missing_model(tmp_path: Path) -> None:
    with pytest.raises(MissingModelError):
        RuntimeArtifactVerifier().verify_model(tmp_path / "missing.gguf", "0" * 64)


def test_artifact_verifier_rejects_symlinked_runtime_artifacts(tmp_path: Path) -> None:
    target = tmp_path / "target"
    link = tmp_path / "sidecar"
    digest = write_artifact(target, b"runtime")
    target.chmod(0o755)
    link.symlink_to(target)

    with pytest.raises(ConfigurationError):
        RuntimeArtifactVerifier().verify_executable(link, digest)


def test_supervisor_rechecks_artifact_identity_immediately_before_popen(tmp_path: Path) -> None:
    executable = tmp_path / "sidecar"
    model = tmp_path / "model.gguf"
    executable_hash = write_artifact(executable, b"executable")
    model_hash = write_artifact(model, b"model")
    executable.chmod(0o755)

    class ChangingVerifier(RuntimeArtifactVerifier):
        def __init__(self) -> None:
            self.calls = 0

        def identity(self, path: Path) -> tuple[int, int, int]:
            self.calls += 1
            identity = super().identity(path)
            if self.calls == 4:
                return identity[0], identity[1] + 1, identity[2]
            return identity

    config = SubprocessConfig(
        executable=executable,
        model=model,
        executable_sha256=executable_hash,
        model_sha256=model_hash,
        executable_manifest=manifest(executable, executable_hash),
        model_manifest=manifest(model, model_hash),
        endpoint="http://127.0.0.1:8123",
    )
    with pytest.raises(ConfigurationError, match="identity changed"):
        SubprocessSupervisor(config, verifier=ChangingVerifier()).start()


def test_windows_supervisor_uses_canonical_launch_order(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "sidecar"
    model = tmp_path / "model.gguf"
    executable_hash = write_artifact(executable, b"executable")
    model_hash = write_artifact(model, b"model")
    events: list[str] = []

    class Verifier:
        def verify_manifest(self, path, artifact_manifest, *, label):
            del path, artifact_manifest, label

        def identity(self, path):
            del path
            return (1, 2, 3)

    class Lock:
        def revalidate(self, digest, path):
            del digest, path
            events.append("revalidate")

        def close(self):
            events.append("close")

    class Job:
        def __init__(self, **kwargs):
            del kwargs

        def assign(self, process):
            del process
            events.append("assign")

        def terminate(self):
            events.append("terminate")

        def close(self):
            events.append("job-close")

    process = FakeProcess()

    def popen(argv, **kwargs):
        del argv, kwargs
        events.append("popen")
        return process

    config = SubprocessConfig(
        executable=executable,
        model=model,
        executable_sha256=executable_hash,
        model_sha256=model_hash,
        executable_manifest=manifest(executable, executable_hash),
        model_manifest=manifest(model, model_hash),
        endpoint="http://127.0.0.1:8123",
    )

    class WindowsOsProxy:
        name = "nt"

        def __getattr__(self, name):
            return getattr(os, name)

    monkeypatch.setattr(process_module, "os", WindowsOsProxy())
    monkeypatch.setattr(process_module, "_open_artifact_read_lock", lambda path: Lock())
    monkeypatch.setattr(media_process_module, "WindowsJobObject", Job)
    monkeypatch.setattr(media_process_module, "_resume_suspended_process", lambda pid: None)

    supervisor = SubprocessSupervisor(config, verifier=Verifier(), popen_factory=popen)
    supervisor.start()
    supervisor.terminate(time.monotonic() + 1.0)

    assert events.index("popen") < events.index("assign") < events.index("revalidate")


def test_artifact_lock_retains_failed_handle_for_retry() -> None:
    class Dll:
        def __init__(self) -> None:
            self.failures = 1

        def CloseHandle(self, handle: int) -> bool:
            del handle
            if self.failures:
                self.failures -= 1
                return False
            return True

    lock = process_module._WindowsArtifactReadLock(
        SimpleNamespace(dll=Dll()), [17], (1, 2, 3, 4, 5), "C:/runtime/sidecar"
    )

    with pytest.raises(ExceptionGroup):
        lock.close()
    assert lock._handles == [17]
    lock.close()
    assert lock._handles == []


def test_supervisor_artifact_cleanup_keeps_failed_lock_in_recovery_registry() -> None:
    class Lock:
        def __init__(self) -> None:
            self.failures = 1
            self.closed = False

        def close(self) -> None:
            if self.failures:
                self.failures -= 1
                raise OSError("simulated CloseHandle failure")
            self.closed = True

    supervisor = object.__new__(SubprocessSupervisor)
    lock = Lock()
    supervisor._artifact_locks = [lock]
    supervisor._artifact_recovery = set()
    supervisor._artifact_recovery_lock = process_module.Lock()
    supervisor._artifact_reaper = None

    with pytest.raises(ExceptionGroup):
        supervisor._close_artifact_locks()
    assert lock in supervisor._artifact_recovery

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not lock.closed:
        time.sleep(0.01)
    assert lock.closed
    assert not supervisor._artifact_recovery


def test_supervisor_starts_only_one_reaper_for_a_process_generation() -> None:
    released = process_module.Event()

    class StubbornProcess:
        pid = 789

        def poll(self):
            return 0 if released.is_set() else None

        def wait(self, timeout=None):
            del timeout
            released.wait(1.0)
            return 0

    supervisor = object.__new__(SubprocessSupervisor)
    supervisor._process = process = StubbornProcess()
    supervisor._process_reaper_generation = None
    supervisor._process_reaper_generation_number = 0
    supervisor._process_reaper_lock = process_module.Lock()
    supervisor._process_reaper_done = process_module.Event()
    supervisor._process_reaper_done.set()
    supervisor._artifact_recovery = set()
    supervisor._artifact_recovery_lock = process_module.Lock()
    supervisor._job_recovery = set()
    supervisor._job_recovery_lock = process_module.Lock()
    supervisor._artifact_locks = []
    supervisor._windows_job = None
    supervisor._artifact_reaper = None
    supervisor._job_reaper = None

    supervisor._start_process_reaper(process)
    generation = supervisor._process_reaper_generation
    supervisor._start_process_reaper(process)

    assert supervisor._process_reaper_generation is generation
    assert generation is not None and generation.thread is not None
    assert generation.thread.is_alive()

    released.set()
    assert supervisor._process_reaper_done.wait(1.0)
    assert supervisor.cleanup_complete()


def test_supervisor_reaper_closes_job_attached_before_it_can_finish() -> None:
    observed = Event()
    release = Event()

    class ExitedProcess:
        pid = 790

        def poll(self):
            observed.set()
            release.wait(1.0)
            return 0

        def wait(self, timeout=None):
            del timeout
            return 0

    class Job:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    supervisor = object.__new__(SubprocessSupervisor)
    supervisor._process = process = ExitedProcess()
    supervisor._process_reaper_generation = None
    supervisor._process_reaper_generation_number = 0
    supervisor._process_reaper_lock = process_module.Lock()
    supervisor._process_reaper_done = process_module.Event()
    supervisor._process_reaper_done.set()
    supervisor._artifact_recovery = set()
    supervisor._artifact_recovery_lock = process_module.Lock()
    supervisor._job_recovery = set()
    supervisor._job_recovery_lock = process_module.Lock()
    supervisor._artifact_locks = []
    supervisor._windows_job = None
    supervisor._artifact_reaper = None
    supervisor._job_reaper = None
    job = Job()

    supervisor._start_process_reaper(process, pending_job=job)
    assert observed.wait(1.0)
    release.set()

    assert supervisor._process_reaper_done.wait(1.0)
    assert job.closed
    assert supervisor.cleanup_complete()


def test_ffmpeg_reaper_observes_attached_job_before_finishing_generation() -> None:
    observed = Event()
    release = Event()

    class ExitedProcess:
        pid = 791

        def poll(self):
            observed.set()
            release.wait(1.0)
            return 0

        def wait(self, timeout=None):
            del timeout
            return 0

    class Job:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    runner = object.__new__(media_process_module.WindowsJobObjectProcessRunner)
    runner._clock = media_process_module.SystemMonotonicClock()
    runner._job_recovery = set()
    runner._job_recovery_lock = process_module.Lock()
    runner._job_reaper = None
    runner._reaper_done = process_module.Event()
    runner._reaper_done.set()
    runner._generation = 1
    process = ExitedProcess()
    runner._run_generation = generation = media_process_module._ProcessGeneration(
        1, process=process
    )
    runner._reaper_process = None
    job = Job()

    runner._start_process_reaper(process, generation, pending_job=job)
    assert observed.wait(1.0)
    release.set()

    assert runner.reaper_done.wait(1.0)
    assert generation.done.is_set()
    assert job.closed


def test_job_handle_recovery_does_not_block_process_generation_completion() -> None:
    release_retry = Event()

    class Process:
        pid = 792

    class EventuallyClosableJob:
        def __init__(self) -> None:
            self.failures = 1
            self.closed = False

        def close(self) -> None:
            if self.failures:
                self.failures -= 1
                raise OSError("simulated CloseHandle failure")
            release_retry.wait(1.0)
            self.closed = True

    runner = object.__new__(media_process_module.WindowsJobObjectProcessRunner)
    runner._clock = media_process_module.SystemMonotonicClock()
    runner._job_recovery = set()
    runner._job_recovery_lock = process_module.Lock()
    runner._job_reaper = None
    runner._reaper_done = process_module.Event()
    runner._reaper_done.set()
    runner._reaper_process = None
    generation = media_process_module._ProcessGeneration(1, process=Process())
    generation.reaper_owned = True
    runner._run_generation = generation
    job = EventuallyClosableJob()
    generation.pending_job = job

    runner._finish_process_reaper(generation)

    assert generation.done.is_set()
    assert runner.reaper_done.is_set()
    assert job in runner._job_recovery
    release_retry.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and not job.closed:
        time.sleep(0.01)
    assert job.closed
    assert not runner._job_recovery


def test_supervisor_wait_ready_uses_injected_monotonic_clock() -> None:
    supervisor = object.__new__(SubprocessSupervisor)
    supervisor._clock = FakeClock(100.0)
    supervisor._process = FakeProcess()
    supervisor._readiness_probe = FakeProbe()

    assert supervisor.wait_ready(100.1)


def test_process_cleanup_uses_no_time_after_absolute_deadline(monkeypatch) -> None:
    force_posix_os(monkeypatch, media_process_module)

    class StubbornProcess:
        pid = 456

        def __init__(self) -> None:
            self.wait_timeouts: list[float | None] = []
            self.released = False

        def poll(self):
            return None

        def kill(self):
            return None

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            if timeout is None:
                while not self.released:
                    time.sleep(0.01)
                return -9
            raise TimeoutError

    process = StubbornProcess()
    runner = media_process_module.SubprocessRunner()
    monkeypatch.setattr(media_process_module.os, "killpg", lambda pid, sig: None, raising=False)

    runner._terminate(process, time.monotonic() - 1.0)

    assert [timeout for timeout in process.wait_timeouts if timeout is not None]
    assert all(timeout <= 0.001 for timeout in process.wait_timeouts if timeout is not None)
    process.released = True


def test_unexpected_wait_failure_keeps_generation_owned_reaper_until_process_exits(
    monkeypatch,
) -> None:
    force_posix_os(monkeypatch, media_process_module)

    released = process_module.Event()

    class WaitFailsWhileAlive:
        pid = 457

        def __init__(self) -> None:
            self.wait_calls = 0

        def poll(self):
            return 0 if released.is_set() else None

        def wait(self, timeout=None):
            self.wait_calls += 1
            if timeout is not None:
                raise RuntimeError("injected wait failure")
            released.wait(1.0)
            return 0

    process = WaitFailsWhileAlive()
    runner = media_process_module.SubprocessRunner()
    monkeypatch.setattr(media_process_module.os, "killpg", lambda pid, sig: None, raising=False)

    with pytest.raises(RuntimeError, match="injected wait failure"):
        runner._terminate(process, time.monotonic() + 1.0)

    assert runner.reaper_owns_process()
    with pytest.raises(media_process_module.ProcessTimedOut):
        runner._begin_generation(time.monotonic() + 0.01)

    released.set()
    assert runner.reaper_done.wait(1.0)
    assert not runner.reaper_owns_process()


def test_ffmpeg_reaper_generation_cannot_clear_the_next_run() -> None:
    runner = media_process_module.SubprocessRunner()
    old = runner._begin_generation(time.monotonic() + 1.0)
    old.process = object()
    old.reaper_owned = True
    old.done.set()

    current = runner._begin_generation(time.monotonic() + 1.0)
    current.process = object()
    runner._finish_process_reaper(old)

    assert runner._run_generation is current
    assert not current.done.is_set()

    runner._finish_generation(current)


def test_subprocess_supervisor_uses_safe_argv_and_bounded_readiness(
    tmp_path: Path, monkeypatch
) -> None:
    if os.name == "nt":
        # This test exercises argv construction with a fake process. The real
        # Windows Job Object launch path is covered by native smoke.
        force_posix_os(monkeypatch, process_module)
    executable = tmp_path / "sidecar"
    model = tmp_path / "model.gguf"
    executable_hash = write_artifact(executable, b"executable")
    model_hash = write_artifact(model, b"model")
    executable.chmod(0o755)
    config = SubprocessConfig(
        executable=executable,
        model=model,
        executable_sha256=executable_hash,
        model_sha256=model_hash,
        executable_manifest=manifest(executable, executable_hash),
        model_manifest=manifest(model, model_hash),
        endpoint="http://127.0.0.1:8123",
        extra_args=("--threads", "2"),
    )
    process = FakeProcess()
    monkeypatch.setenv("GH_TOKEN", "must-not-be-inherited")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-be-inherited")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def popen(argv: list[str], **kwargs):
        calls.append((argv, kwargs))
        return process

    killpg_calls: list[tuple[int, object]] = []
    monkeypatch.setattr(
        process_module.os,
        "killpg",
        lambda pid, sig: killpg_calls.append((pid, sig)),
        raising=False,
    )
    supervisor = SubprocessSupervisor(
        config,
        readiness_probe=FakeProbe(),
        popen_factory=popen,
    )

    supervisor.start()

    assert supervisor.wait_ready(time.monotonic() + 1.0)
    assert calls[0][0][:9] == [
        str(executable),
        "serve",
        "--asr-model",
        str(model),
        "--host",
        "127.0.0.1",
        "--port",
        "8123",
        "--no-ui",
    ]
    assert calls[0][0][9:11] == [
        "--asr.model.name",
        config.model_id,
    ]
    assert calls[0][0][11:13] == [
        "--device",
        "cpu",
    ]
    assert "--api-key" not in calls[0][0]
    assert "--http.api-key" not in calls[0][0]
    assert calls[0][0][13:] == ["--threads", "2"]
    assert replace(config, backend="cuda:2").argv()[11:13] == [
        "--device",
        "cuda:2",
    ]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["env"][process_module.ASR_NONCE_ENV] == supervisor.nonce
    assert calls[0][1]["env"][process_module.ASR_API_KEY_ENV] == supervisor.api_key
    assert "GH_TOKEN" not in calls[0][1]["env"]
    assert "GITHUB_TOKEN" not in calls[0][1]["env"]
    assert supervisor.process_tree_mode == "posix-process-group"
    supervisor.terminate(time.monotonic() + 1.0)
    assert killpg_calls


def test_subprocess_config_rejects_non_loopback_endpoint(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        SubprocessConfig(
            executable=tmp_path / "sidecar",
            model=tmp_path / "model.gguf",
            executable_sha256="0" * 64,
            model_sha256="0" * 64,
            executable_manifest=manifest(tmp_path / "sidecar", "0" * 64),
            model_manifest=manifest(tmp_path / "model.gguf", "0" * 64),
            endpoint="http://example.invalid:8123",
        )


@pytest.mark.parametrize(
    "flag",
    [
        "--model",
        "--host",
        "--port",
        "--backend",
        "--asr-model",
        "--api-key",
        "--http.api-key",
        "--asr.model.name",
        "--asr.backend.gpu",
        "--http.host",
        "--http.port",
        "--device",
    ],
)
@pytest.mark.parametrize("form", ["split", "equal"])
def test_subprocess_config_rejects_security_critical_extra_args(
    tmp_path: Path, flag: str, form: str
) -> None:
    extra_args = (flag, "attacker") if form == "split" else (f"{flag}=attacker",)
    with pytest.raises(ConfigurationError, match="security-critical"):
        SubprocessConfig(
            executable=tmp_path / "sidecar",
            model=tmp_path / "model.gguf",
            executable_sha256="0" * 64,
            model_sha256="0" * 64,
            executable_manifest=manifest(tmp_path / "sidecar", "0" * 64),
            model_manifest=manifest(tmp_path / "model.gguf", "0" * 64),
            endpoint="http://127.0.0.1:8123",
            extra_args=extra_args,
        )
