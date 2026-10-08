from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from threading import Event, Lock, Thread

import pytest

import voiceink_win.infrastructure.process as process_module
from voiceink_win.domain import (
    ConfigurationError,
    RuntimeRecoveryPendingError,
    RuntimeUnavailableError,
)
from voiceink_win.infrastructure import (
    NeMoSidecarRuntime,
    RuntimeArtifactManifest,
    SidecarConfig,
    StartupDiagnostics,
    SubprocessConfig,
    SubprocessSupervisor,
    TransportResponse,
)


class _Process:
    pid = 4321

    def __init__(self, *, running: bool = True) -> None:
        self.running = running
        self.wait_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return None if self.running else 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.wait_calls += 1
        self.running = False
        return 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.running = False


def _force_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    real_os = os

    class PosixOsProxy:
        name = "posix"

        def __getattr__(self, name):
            return getattr(real_os, name)

    monkeypatch.setattr(process_module, "os", PosixOsProxy())


def _supervisor_for_process(process: _Process) -> SubprocessSupervisor:
    supervisor = object.__new__(SubprocessSupervisor)
    supervisor._process = process
    supervisor._windows_job = None
    supervisor._artifact_locks = []
    supervisor._artifact_recovery = set()
    supervisor._artifact_recovery_lock = Lock()
    supervisor._artifact_reaper = None
    supervisor._job_recovery = set()
    supervisor._job_recovery_lock = Lock()
    supervisor._job_reaper = None
    supervisor._process_reaper_generation = None
    supervisor._process_reaper_generation_number = 0
    supervisor._process_reaper_lock = Lock()
    supervisor._process_reaper_done = process_module.Event()
    supervisor._process_reaper_done.set()
    supervisor._termination_lock = Lock()
    supervisor._process_state = process_module._ProcessLifecycleState.RUNNING
    supervisor._clock = process_module._SystemClock()
    supervisor._diagnostics = StartupDiagnostics()
    return supervisor


def _manifest(path: Path, content: bytes) -> RuntimeArtifactManifest:
    return RuntimeArtifactManifest(
        "test",
        "https://example.invalid/runtime",
        hashlib.sha256(content).hexdigest(),
        "Apache-2.0",
        path,
    )


def test_t01_failure_before_process_creation_preserves_typed_error_and_does_not_terminate(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "sidecar"
    model = tmp_path / "model.gguf"
    executable.write_bytes(b"executable")
    model.write_bytes(b"model")
    executable.chmod(0o755)
    executable_manifest = _manifest(executable, b"executable")
    model_manifest = _manifest(model, b"model")

    class FailingVerifier:
        def verify_manifest(self, *args, **kwargs) -> None:
            del args, kwargs
            raise ConfigurationError("private path must not be reported")

    popen_calls: list[object] = []
    supervisor = SubprocessSupervisor(
        SubprocessConfig(
            executable,
            model,
            executable_manifest.sha256,
            model_manifest.sha256,
            executable_manifest,
            model_manifest,
            "http://127.0.0.1:8123",
        ),
        verifier=FailingVerifier(),
        popen_factory=lambda *args, **kwargs: popen_calls.append((args, kwargs)),
    )

    with pytest.raises(ConfigurationError):
        supervisor.start()

    assert popen_calls == []
    assert supervisor.diagnostics.primary_failure is not None
    assert supervisor.diagnostics.primary_failure.error_code == "configuration"
    assert not any(
        record["operation"] == "terminate_process"
        for record in supervisor.diagnostics.as_dict()["records"]
    )


def test_t02_already_stopped_process_is_reaped_once_without_termination(monkeypatch) -> None:
    _force_posix(monkeypatch)
    process = _Process(running=False)
    supervisor = _supervisor_for_process(process)
    killpg_calls: list[object] = []
    monkeypatch.setattr(process_module.os, "killpg", lambda *args: killpg_calls.append(args))

    supervisor.terminate(time.monotonic() + 1.0)

    assert process.wait_calls == 1
    assert killpg_calls == []
    assert process.kill_calls == 0


def test_t03_running_owned_process_gets_one_termination_and_is_reaped(monkeypatch) -> None:
    _force_posix(monkeypatch)
    process = _Process()
    supervisor = _supervisor_for_process(process)
    killpg_calls: list[object] = []
    monkeypatch.setattr(process_module.os, "killpg", lambda *args: killpg_calls.append(args))

    supervisor.terminate(time.monotonic() + 1.0)

    assert len(killpg_calls) == 1
    assert process.wait_calls == 1
    assert process.poll() == 0


def test_windows_pid_tree_fallback_refuses_unowned_process_cleanup(monkeypatch) -> None:
    _force_posix(monkeypatch)

    class WindowsOsProxy:
        name = "nt"

        def __getattr__(self, name):
            return getattr(os, name)

    class FailingTerminateProcess:
        pid = 4322

        def __init__(self) -> None:
            self.running = True
            self.terminate_calls = 0
            self.kill_calls = 0
            self.released = Event()

        def poll(self) -> int | None:
            return None if self.running else 0

        def terminate(self) -> None:
            self.terminate_calls += 1
            raise OSError(5, "terminate failed")

        def kill(self) -> None:
            self.kill_calls += 1
            self.running = False
            self.released.set()

        def wait(self, timeout: float | None = None) -> int:
            if timeout is None:
                self.released.wait(1.0)
            self.running = False
            return 0

    monkeypatch.setattr(process_module, "os", WindowsOsProxy())
    process = FailingTerminateProcess()
    supervisor = _supervisor_for_process(process)

    with pytest.raises(RuntimeRecoveryPendingError, match="ownership is unavailable"):
        supervisor.terminate(time.monotonic() + 1.0)

    assert process.terminate_calls == 0
    assert process.running
    assert supervisor._process_state is process_module._ProcessLifecycleState.TERMINATION_FAILED
    process.released.set()
    process.running = False
    assert supervisor._process_reaper_done.wait(1.0)
    assert not supervisor.cleanup_complete()


@pytest.mark.parametrize("actual_creation_time", [None, 101])
def test_windows_child_pid_creation_time_mismatch_is_recovery_pending(actual_creation_time) -> None:
    with pytest.raises(RuntimeRecoveryPendingError, match="child process identity"):
        process_module._require_matching_process_creation_time(100, actual_creation_time)


def test_windows_pid_tree_fallback_never_terminates_unverified_children(monkeypatch) -> None:
    monkeypatch.setattr(process_module.sys, "platform", "win32")
    process = _Process()
    process.kill_calls = 0

    def kill() -> None:
        process.kill_calls += 1

    process.kill = kill

    with pytest.raises(RuntimeRecoveryPendingError, match="child PID ownership"):
        SubprocessSupervisor._kill_windows_tree(process)

    assert process.kill_calls == 0


def test_t04_repeated_concurrent_cleanup_cannot_issue_a_second_termination(monkeypatch) -> None:
    _force_posix(monkeypatch)
    process = _Process()
    supervisor = _supervisor_for_process(process)
    entered = Event()
    release = Event()
    killpg_calls: list[object] = []

    def killpg(*args) -> None:
        killpg_calls.append(args)
        entered.set()
        release.wait(timeout=1.0)
        process.running = False

    monkeypatch.setattr(process_module.os, "killpg", killpg)
    first = Thread(target=supervisor.terminate, args=(time.monotonic() + 1.0,))
    second = Thread(target=supervisor.terminate, args=(time.monotonic() + 1.0,))
    first.start()
    assert entered.wait(1.0)
    second.start()
    release.set()
    threads = [first, second]
    for thread in threads:
        thread.join(timeout=2.0)

    assert all(not thread.is_alive() for thread in threads)
    assert len(killpg_calls) == 1


def test_t05_cleanup_failure_does_not_replace_primary_failure() -> None:
    class Supervisor:
        api_key = "test-key"
        nonce = None

        def __init__(self) -> None:
            self.running = True
            self.terminate_calls = 0
            self.kill_calls = 0
            self.diagnostics = StartupDiagnostics()

        def start(self) -> None:
            self.running = True

        def wait_ready(self, deadline: float) -> bool:
            del deadline
            return False

        def is_running(self) -> bool:
            return self.running

        def terminate(self, deadline: float) -> None:
            del deadline
            self.terminate_calls += 1

        def kill(self, deadline: float | None = None) -> None:
            del deadline
            self.kill_calls += 1
            self.running = False
            raise OSError("secret path")

        def cleanup_complete(self) -> bool:
            return True

    class Transport:
        def close(self) -> None:
            pass

    supervisor = Supervisor()
    runtime = NeMoSidecarRuntime(SidecarConfig("http://127.0.0.1:8123"), Transport(), supervisor)

    with pytest.raises(RuntimeUnavailableError, match="did not become ready"):
        runtime.start()

    diagnostics = runtime.diagnostics.as_dict()
    assert diagnostics["primary_failure"]["error_code"] == "runtime_unavailable"
    assert diagnostics["primary_failure"]["operation"] == "probe_readiness"
    assert diagnostics["cleanup_outcome"] == "failed"
    assert diagnostics["primary_failure"] != diagnostics["cleanup_failures"][0]


def test_t06_diagnostics_are_bounded_and_redacted() -> None:
    diagnostics = StartupDiagnostics()
    secret = "/Users/alice/private/model.gguf raw transcript"
    diagnostics.record("configuration", "validate_endpoint", "../../private/model.gguf")
    for _ in range(40):
        diagnostics.record("configuration", "validate_endpoint", "x" * 500)
    for _ in range(20):
        diagnostics.record_cleanup_failure("close_resource", RuntimeError(secret))

    payload = diagnostics.as_dict()
    serialized = json.dumps(payload, ensure_ascii=True)
    assert len(payload["records"]) == 32
    assert len(payload["cleanup_failures"]) == 16
    assert payload["diagnostics_truncated"] is True
    assert len(serialized.encode()) <= 64 * 1024
    assert secret not in serialized
    for record in [*payload["records"], *payload["cleanup_failures"]]:
        assert all(
            len(str(record[field])) <= 128 for field in ("startup_phase", "operation", "error_code")
        )


def test_t07_launch_order_observability_places_readiness_and_attestation_after_resume() -> None:
    diagnostics = StartupDiagnostics()
    diagnostics.record("configuration", "validate_endpoint", "stale")

    class Supervisor:
        api_key = "test-key"
        nonce = None

        @property
        def diagnostics(self) -> StartupDiagnostics:
            return diagnostics

        def start(self) -> None:
            diagnostics.record("process_create_suspended", "create_process", "created")
            diagnostics.record("job_assignment", "assign_job", "assigned")
            diagnostics.record("artifact_revalidation", "revalidate_artifact", "revalidated")
            diagnostics.record("resume", "resume_process", "resumed")

        def wait_ready(self, deadline: float) -> bool:
            del deadline
            return True

        def is_running(self) -> bool:
            return True

        def terminate(self, deadline: float) -> None:
            del deadline

        def kill(self, deadline: float | None = None) -> None:
            del deadline

    class Transport:
        def get(self, *args, **kwargs) -> TransportResponse:
            del args, kwargs
            return TransportResponse(
                200,
                b'{"data":[{"id":"parakeet-tdt-0.6b-v3.oss-align.q8_0","capability":"transcription"}]}',
            )

        def close(self) -> None:
            pass

    runtime = NeMoSidecarRuntime(
        SidecarConfig("http://127.0.0.1:8123", require_model_attestation=True),
        Transport(),
        Supervisor(),
    )
    runtime.start()

    assert all(record["error_code"] != "stale" for record in diagnostics.as_dict()["records"])
    operations = [record["operation"] for record in diagnostics.as_dict()["records"]]
    assert operations.index("revalidate_artifact") < operations.index("resume_process")
    assert operations.index("resume_process") < operations.index("probe_readiness")
    assert operations.index("probe_readiness") < operations.index("observe_attestation")


def test_t08_numeric_win32_code_is_retained_without_windows_message() -> None:
    class Win32Failure(OSError):
        winerror = 87

    diagnostics = StartupDiagnostics()
    cause = Win32Failure("raw Windows message")
    wrapped = ConfigurationError("safe configuration error", cause=cause)
    diagnostics.record_failure("resume", "resume_process", wrapped)

    payload = diagnostics.as_dict()
    assert payload["primary_failure"]["win32_error_code"] == 87
    assert "raw Windows message" not in json.dumps(payload)
    assert wrapped.cause is cause


def test_t09_pending_recovery_rejects_second_start_before_process_creation() -> None:
    class Supervisor:
        api_key = "test-key"
        nonce = None

        def __init__(self) -> None:
            self.start_calls = 0
            self.running = False
            self.diagnostics = StartupDiagnostics()

        def start(self) -> None:
            self.start_calls += 1
            self.running = True

        def wait_ready(self, deadline: float) -> bool:
            del deadline
            return False

        def is_running(self) -> bool:
            return self.running

        def terminate(self, deadline: float) -> None:
            del deadline

        def kill(self, deadline: float | None = None) -> None:
            del deadline

        def cleanup_complete(self) -> bool:
            return False

    class Transport:
        def close(self) -> None:
            pass

    supervisor = Supervisor()
    runtime = NeMoSidecarRuntime(
        SidecarConfig("http://127.0.0.1:8123", shutdown_timeout=0.01),
        Transport(),
        supervisor,
    )

    with pytest.raises(RuntimeUnavailableError):
        runtime.start()
    with pytest.raises(RuntimeRecoveryPendingError):
        runtime.start()

    assert supervisor.start_calls == 1
    assert runtime.diagnostics.cleanup_outcome == "pending"
