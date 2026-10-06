"""Run the real Windows snapshot, FFmpeg, and configured ASR contract."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Thread, Timer
from time import monotonic, sleep

NATIVE_SMOKE_READINESS_TIMEOUT = 60.0


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"native smoke requires {name}")
    return value


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_runtime_configuration():
    from voiceink_win.infrastructure import load_runtime_manifest

    manifest_path = Path(_required("VOICEINK_RUNTIME_MANIFEST"))
    lock_path = Path(_required("VOICEINK_ARTIFACT_LOCK"))
    return load_runtime_manifest(
        manifest_path,
        lock_path,
        lock_sha256=_required("VOICEINK_ARTIFACT_LOCK_SHA256"),
    )


def _load_native_smoke_lock():
    try:
        from scripts.native_smoke_lock import load_native_smoke_lock
    except ModuleNotFoundError:
        from native_smoke_lock import load_native_smoke_lock

    return load_native_smoke_lock(Path(_required("VOICEINK_NATIVE_SMOKE_ARTIFACT_LOCK")))


def _pinned_required(name: str, expected: str) -> str:
    try:
        from scripts.native_smoke_lock import pinned_value
    except ModuleNotFoundError:
        from native_smoke_lock import pinned_value

    return pinned_value(name, expected, _required(name))


def _allocate_loopback_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{listener.getsockname()[1]}"


def _write_report(path: Path, report: dict[str, object]) -> None:
    from voiceink_win.infrastructure import sanitize_report_value

    path.write_text(
        json.dumps(sanitize_report_value(report), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _build_native_smoke_configs(
    runtime_configuration,
    endpoint: str,
    executable_manifest,
    model_manifest,
):
    from voiceink_win.infrastructure import SidecarConfig, SubprocessConfig

    sidecar_config = SidecarConfig(
        endpoint=endpoint,
        model_id=runtime_configuration.model_id,
        backend=runtime_configuration.backend,
        readiness_timeout=NATIVE_SMOKE_READINESS_TIMEOUT,
        require_model_attestation=True,
    )
    subprocess_config = SubprocessConfig(
        executable=runtime_configuration.executable,
        model=runtime_configuration.model,
        executable_sha256=executable_manifest.sha256,
        model_sha256=model_manifest.sha256,
        executable_manifest=executable_manifest,
        model_manifest=model_manifest,
        endpoint=sidecar_config.endpoint,
        backend=sidecar_config.backend,
        model_id=runtime_configuration.model_id,
    )
    return sidecar_config, subprocess_config


class _NativeSmokeTemporaryDirectory:
    def __init__(self, report: dict[str, object]) -> None:
        self._report = report
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.store = None
        self.source = None
        self.snapshot = None
        self.workspace = None

    def __enter__(self) -> str:
        self._temporary = tempfile.TemporaryDirectory(prefix="voiceink-native-smoke-")
        return self._temporary.name

    def __exit__(self, exception_type, exception, traceback) -> bool:
        errors: list[tuple[str, BaseException]] = []
        if self.snapshot is not None and self.store is not None:
            try:
                self.store.release_snapshot(self.snapshot)
            except BaseException as error:
                errors.append(("snapshot.release", error))
        if self.workspace is not None and self.store is not None:
            try:
                self.store.cleanup(self.workspace)
                if self.workspace.path.exists():
                    raise RuntimeError("native smoke workspace still exists after cleanup")
            except BaseException as error:
                errors.append(("workspace.cleanup", error))
        if self.source is not None and self.store is not None:
            try:
                self.store.release_source(self.source)
            except BaseException as error:
                errors.append(("source.release", error))
        if self.store is not None:
            try:
                self.store.close(timeout=5.0)
                self._report["overlapped_reapers_awaited"] = True
            except BaseException as error:
                errors.append(("store.close", error))
                self._report["overlapped_reapers_awaited"] = False
                self._report["store_close_error"] = type(error).__name__
        assert self._temporary is not None
        try:
            self._temporary.cleanup()
        except BaseException as error:
            errors.append(("temporary.cleanup", error))
        if errors:
            self._report.setdefault("cleanup_errors", []).extend(
                {"operation": operation, "error_type": type(error).__name__}
                for operation, error in errors
            )
            if exception is None:
                raise ExceptionGroup("native smoke cleanup failed", [error for _, error in errors])
        return False


def main() -> int:
    report_path = Path(os.environ.get("VOICEINK_NATIVE_SMOKE_REPORT", "native-smoke-report.json"))
    report: dict[str, object] = {"status": "failed", "native": os.name == "nt"}
    try:
        return _run(report_path, report)
    except Exception as error:
        from voiceink_win.infrastructure import safe_failure

        report["failure"] = safe_failure(error)
        _write_report(report_path, report)
        if isinstance(error, (FileNotFoundError, ValueError)):
            return 2
        if getattr(error, "code", None) in {"configuration", "missing_model"}:
            return 2
        if any(
            marker in str(error).casefold()
            for marker in ("quality", "latency", "memory", "cleanup")
        ):
            return 4
        return 3


def _run(report_path: Path, report: dict[str, object]) -> int:
    if os.name != "nt":
        error = RuntimeError("native smoke must run on Windows; Mac runs are not evidence")
        from voiceink_win.infrastructure import safe_failure

        report["failure"] = safe_failure(error)
        _write_report(report_path, report)
        raise error

    from voiceink_win.application import AsrApplicationService
    from voiceink_win.domain import AsrRequest, JobId
    from voiceink_win.infrastructure import (
        FfmpegArtifactManifest,
        JsonlEventWriter,
        NeMoSidecarRuntime,
        RuntimeArtifactManifest,
        RuntimeArtifactVerifier,
        SubprocessMediaNormalizer,
        SubprocessSupervisor,
        UrllibLoopbackTransport,
        VerifiedFfmpegArtifact,
        WindowsMediaSnapshotStore,
        safe_failure,
    )

    native_lock = _load_native_smoke_lock()
    pins = native_lock["artifacts"]
    assert isinstance(pins, dict)
    ffmpeg_pin = pins["ffmpeg"]
    sidecar_pin = pins["nemo-speech-cpp-windows-amd64"]
    model_pin = pins["parakeet-tdt-0.6b-v3.oss-align.q8_0"]
    fixture_pin = pins["fixture"]
    assert isinstance(ffmpeg_pin, dict)
    assert isinstance(sidecar_pin, dict)
    assert isinstance(model_pin, dict)
    assert isinstance(fixture_pin, dict)
    fixture = Path(_required("VOICEINK_NATIVE_SMOKE_FIXTURE")).resolve(strict=True)
    fixture_sha256 = _pinned_required("VOICEINK_NATIVE_SMOKE_FIXTURE_SHA256", fixture_pin["sha256"])
    fixture_license = _pinned_required(
        "VOICEINK_NATIVE_SMOKE_FIXTURE_LICENSE", fixture_pin["license"]
    )
    ffmpeg_path = Path(_required("VOICEINK_FFMPEG_PATH")).resolve(strict=True)
    ffmpeg_sha256 = _pinned_required("VOICEINK_FFMPEG_SHA256", ffmpeg_pin["executable_sha256"])
    runtime_configuration = _load_runtime_configuration()
    if runtime_configuration.model_id != model_pin["model_id"]:
        raise RuntimeError("runtime manifest model ID does not match the tracked native smoke lock")
    if runtime_configuration.executable_artifact.sha256 != sidecar_pin["executable_sha256"]:
        raise RuntimeError("runtime executable hash does not match the tracked native smoke lock")
    if runtime_configuration.model_artifact.sha256 != model_pin["sha256"]:
        raise RuntimeError("runtime model hash does not match the tracked native smoke lock")
    runtime_path = runtime_configuration.executable
    model_path = runtime_configuration.model
    source_sha256 = _hash(fixture)
    if source_sha256.lower() != fixture_sha256.lower():
        raise RuntimeError("native smoke fixture checksum mismatch")
    manifest = FfmpegArtifactManifest(
        version=_pinned_required("VOICEINK_FFMPEG_VERSION", ffmpeg_pin["version"]),
        provenance_url=_pinned_required(
            "VOICEINK_FFMPEG_PROVENANCE_URL", ffmpeg_pin["provenance_url"]
        ),
        sha256=ffmpeg_sha256,
        license=_pinned_required("VOICEINK_FFMPEG_LICENSE", ffmpeg_pin["license"]),
        allowed_path=ffmpeg_path,
    )
    artifact = VerifiedFfmpegArtifact.verify(ffmpeg_path, manifest)
    if _hash(ffmpeg_path).lower() != ffmpeg_sha256.lower():
        raise RuntimeError("FFmpeg checksum changed between verification and smoke setup")

    endpoint = _allocate_loopback_endpoint()
    executable_manifest = RuntimeArtifactManifest(
        version=runtime_configuration.executable_artifact.version,
        provenance_url=runtime_configuration.executable_artifact.provenance_url,
        sha256=runtime_configuration.executable_artifact.sha256,
        license=runtime_configuration.executable_artifact.license,
        allowed_path=runtime_configuration.executable_artifact.allowed_path,
    )
    model_manifest = RuntimeArtifactManifest(
        version=runtime_configuration.model_artifact.version,
        provenance_url=runtime_configuration.model_artifact.provenance_url,
        sha256=runtime_configuration.model_artifact.sha256,
        license=runtime_configuration.model_artifact.license,
        allowed_path=runtime_configuration.model_artifact.allowed_path,
    )
    RuntimeArtifactVerifier().verify_manifest(
        runtime_path, executable_manifest, label="runtime executable"
    )
    RuntimeArtifactVerifier().verify_manifest(model_path, model_manifest, label="runtime model")
    sidecar_config, subprocess_config = _build_native_smoke_configs(
        runtime_configuration,
        endpoint,
        executable_manifest,
        model_manifest,
    )
    supervisor = SubprocessSupervisor(subprocess_config)
    events = JsonlEventWriter(
        Path(os.environ.get("VOICEINK_NATIVE_SMOKE_EVENTS", "native-smoke-events.jsonl"))
    )
    events.event(
        "runtime.verified",
        artifact_id=runtime_configuration.manifest.executable_artifact_id,
        version=executable_manifest.version,
        provenance_url=executable_manifest.provenance_url,
        sha256=executable_manifest.sha256,
        license=executable_manifest.license,
    )
    events.event(
        "model.verified",
        artifact_id=runtime_configuration.manifest.model_artifact_id,
        version=model_manifest.version,
        provenance_url=model_manifest.provenance_url,
        sha256=model_manifest.sha256,
        license=model_manifest.license,
    )
    runtime = NeMoSidecarRuntime(
        sidecar_config,
        UrllibLoopbackTransport(sidecar_config.endpoint),
        supervisor,
    )
    asr = None
    store = None
    source = None
    workspace = None
    snapshot = None
    runtime_started = False
    try:
        runtime.start()
        runtime_started = True
        events.event(
            "sidecar.ready", backend=sidecar_config.backend, model_id=sidecar_config.model_id
        )
        asr = AsrApplicationService(runtime)
        temporary_directory = _NativeSmokeTemporaryDirectory(report)
        with temporary_directory as temporary:
            root = Path(temporary) / "workspace"
            store = WindowsMediaSnapshotStore(root, import_roots=(fixture.parent,))
            temporary_directory.store = store
            report["snapshot_security"] = _run_snapshot_security_probes(
                Path(temporary) / "snapshot-security", report
            )
            workspace = store.create_workspace(JobId("native-smoke"), 1)
            source = store.validate_source(fixture, max_bytes=2 * 1024**3)
            temporary_directory.workspace = workspace
            temporary_directory.source = source
            snapshot = store.verify(store.snapshot(source, workspace))
            temporary_directory.snapshot = snapshot
            normalizer = SubprocessMediaNormalizer(executable=artifact)
            process_tree_mode = getattr(normalizer.runner, "process_tree_mode", None)
            if process_tree_mode != "windows-job-object-adapter":
                raise RuntimeError("native smoke requires the Windows Job Object process runner")
            normalized = normalizer.normalize(
                snapshot, workspace, _NeverCancelled(), monotonic() + 60
            )
            result = asr.transcribe(
                AsrRequest(
                    normalized.audio,
                    request_id="native-smoke",
                    deadline=monotonic() + 60,
                )
            )
            if not result.text.strip():
                raise RuntimeError("configured ASR returned an empty transcript")
            events.event(
                "asr.completed",
                backend=sidecar_config.backend,
                model_id=sidecar_config.model_id,
                duration_seconds=result.duration,
                transcript_length=len(result.text),
            )
            health = runtime.health()
            if health.status.value != "ready":
                raise RuntimeError(f"configured ASR health is {health.status.value}")
            report.update(
                {
                    "pipeline": "passed",
                    "fixture_sha256": source_sha256,
                    "fixture_license": fixture_license,
                    "fixture_provenance_url": fixture_pin["provenance_url"],
                    "source_sha256_unchanged": _hash(fixture) == source_sha256,
                    "sample_count": normalized.sample_count,
                    "transcript_non_empty": True,
                    "runtime": {
                        "model_id": sidecar_config.model_id,
                        "backend": sidecar_config.backend,
                        "health": health.status.value,
                        "executable": {
                            "version": executable_manifest.version,
                            "provenance_url": executable_manifest.provenance_url,
                            "sha256": executable_manifest.sha256,
                            "license": executable_manifest.license,
                        },
                        "model": {
                            "version": model_manifest.version,
                            "provenance_url": model_manifest.provenance_url,
                            "sha256": model_manifest.sha256,
                            "license": model_manifest.license,
                        },
                    },
                    "ffmpeg": {
                        "version": manifest.version,
                        "sha256": manifest.sha256,
                        "process_tree_mode": getattr(normalizer.runner, "process_tree_mode", None),
                    },
                }
            )
            store.release_snapshot(snapshot)
            snapshot = None
            temporary_directory.snapshot = None
            store.cleanup(workspace)
            if workspace.path.exists():
                raise RuntimeError("native smoke workspace still exists after cleanup")
            workspace = None
            temporary_directory.workspace = None
            cancel_after = float(_required("VOICEINK_NATIVE_SMOKE_CANCEL_AFTER_SECONDS"))
            cancellation_report = {}
            try:
                _run_process_tree_probe(cancel_after)
                cancellation_report["ffmpeg_process_tree"] = "passed"
            except Exception as error:
                cancellation_report["ffmpeg_process_tree"] = {
                    "status": "failed",
                    **safe_failure(error),
                }
                report["cancellation"] = cancellation_report
                raise
            try:
                _run_cancellation_probe(store, source, normalizer, cancel_after, report)
                cancellation_report["snapshot_normalization"] = "passed"
            except Exception as error:
                cancellation_report["snapshot_normalization"] = {
                    "status": "failed",
                    **safe_failure(error),
                }
                report["cancellation"] = cancellation_report
                raise
            try:
                _run_asr_cancellation_probe(
                    asr,
                    normalized.audio,
                    float(_required("VOICEINK_NATIVE_SMOKE_ASR_CANCEL_AFTER_SECONDS")),
                )
                cancellation_report["asr_late_result_barrier"] = "passed"
            except Exception as error:
                cancellation_report["asr_late_result_barrier"] = {
                    "status": "failed",
                    **safe_failure(error),
                }
                report["cancellation"] = cancellation_report
                raise
            report["cancellation"] = cancellation_report
    finally:
        if asr is not None:
            try:
                asr.close()
            except Exception as error:
                report["asr_close_error"] = type(error).__name__
        else:
            try:
                runtime.close()
            except Exception as error:
                report["runtime_close_error"] = type(error).__name__
        if runtime_started:
            events.event(
                "runtime.cleaned", status="failed" if "runtime_close_error" in report else "passed"
            )
        events.close()
        _write_report(report_path, report)

    report["source_sha256_unchanged"] = _hash(fixture) == source_sha256
    _write_report(report_path, report)
    if not report["source_sha256_unchanged"]:
        raise RuntimeError("source SHA-256 changed during native smoke")
    cleanup_errors = [
        key for key in ("cleanup_errors", "asr_close_error", "runtime_close_error") if key in report
    ]
    if cleanup_errors:
        raise RuntimeError(f"native smoke cleanup failed: {', '.join(cleanup_errors)}")
    report["status"] = "passed"
    _write_report(report_path, report)
    print("native snapshot/FFmpeg/ASR smoke passed; no fake adapter was used")
    return 0


class _NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


def _run_cancellation_probe(
    store, source, normalizer, cancel_after: float, report: dict[str, object] | None = None
) -> None:
    from voiceink_win.application import CancellationTokenSource
    from voiceink_win.domain import CancellationError, JobId

    if cancel_after <= 0:
        raise RuntimeError("VOICEINK_NATIVE_SMOKE_CANCEL_AFTER_SECONDS must be positive")
    workspace = store.create_workspace(JobId("native-smoke-cancel"), 2)
    snapshot = None
    cancellation = CancellationTokenSource()
    timer = Timer(cancel_after, cancellation.cancel)
    timer.start()
    try:
        snapshot = store.verify(store.snapshot(source, workspace))
        try:
            normalizer.normalize(snapshot, workspace, cancellation.token, monotonic() + 120)
        except CancellationError:
            return
        raise RuntimeError(
            "configured cancellation probe completed before cancellation; use a longer fixture"
        )
    finally:
        timer.cancel()
        _cleanup_probe_resources(store, snapshot=snapshot, workspace=workspace, report=report)
        if workspace.path.exists():
            raise RuntimeError("cancellation probe workspace still exists after cleanup")


def _cleanup_probe_resources(
    store, *, source=None, snapshot=None, workspace=None, report: dict[str, object] | None = None
) -> None:
    errors: list[BaseException] = []
    if snapshot is not None:
        try:
            store.release_snapshot(snapshot)
        except BaseException as error:
            errors.append(error)
    if workspace is not None:
        try:
            store.cleanup(workspace)
        except BaseException as error:
            errors.append(error)
    if source is not None:
        try:
            store.release_source(source)
        except BaseException as error:
            errors.append(error)
    if errors:
        if report is not None:
            report.setdefault("cleanup_errors", []).extend(
                {"operation": "probe.resource", "error_type": type(error).__name__}
                for error in errors
            )
        raise ExceptionGroup("native smoke probe cleanup failed", errors)


def _run_snapshot_security_probes(
    root: Path, report: dict[str, object] | None = None
) -> dict[str, bool]:
    from voiceink_win.infrastructure import WindowsMediaSnapshotStore

    (root / "inputs").mkdir(parents=True, exist_ok=True)
    store = WindowsMediaSnapshotStore(root / "workspaces", import_roots=(root / "inputs",))
    try:
        return _run_snapshot_security_probes_with_store(store, root, report)
    finally:
        try:
            store.close(timeout=5.0)
        except BaseException as error:
            if report is not None:
                report.setdefault("cleanup_errors", []).append(
                    {"operation": "security.store.close", "error_type": type(error).__name__}
                )
            raise


def _run_snapshot_security_probes_with_store(
    store, root: Path, report: dict[str, object] | None = None
) -> dict[str, bool]:
    """Exercise native source admission and immutable snapshot verification."""
    from voiceink_win.domain import InvalidSourceError, JobId, SourceChangedError

    inputs = root / "inputs"
    target = root / "junction-target"
    inputs.mkdir(parents=True)
    target.mkdir()
    original = b"native snapshot security fixture"

    def expect_changed(path: Path, mutation) -> None:
        path.write_bytes(original)
        source = store.validate_source(path, max_bytes=1024)
        workspace = None
        try:
            workspace = store.create_workspace(JobId(f"probe-{path.stem}"), 1)
            mutation(path)
            try:
                store.snapshot(source, workspace)
            except SourceChangedError:
                return
            raise RuntimeError(f"native smoke accepted a changed source: {path.name}")
        finally:
            _cleanup_probe_resources(store, source=source, workspace=workspace, report=report)

    mutation_path = inputs / "mutation.bin"
    expect_changed(
        mutation_path, lambda path: path.write_bytes(b"native snapshot security FIXTURE")
    )

    replacement_path = inputs / "replacement.bin"

    def replace(path: Path) -> None:
        path.unlink()
        path.write_bytes(original)

    expect_changed(replacement_path, replace)

    junction = inputs / "junction"
    junction_target = target / "media.bin"
    junction_target.write_bytes(original)
    junction_result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if junction_result.returncode != 0:
        raise RuntimeError("native smoke could not create a junction probe")
    try:
        try:
            store.validate_source(junction / "media.bin", max_bytes=1024)
        except (InvalidSourceError, OSError):
            junction_rejected = True
        else:
            junction_rejected = False
    finally:
        junction.rmdir()
    if not junction_rejected:
        raise RuntimeError("native smoke accepted a junction source path")

    verification_path = inputs / "verification.bin"
    verification_path.write_bytes(original)
    source = store.validate_source(verification_path, max_bytes=1024)
    workspace = store.create_workspace(JobId("probe-verification"), 1)
    snapshot = None
    try:
        snapshot = store.verify(store.snapshot(source, workspace))
        get_attributes = ctypes.windll.kernel32.GetFileAttributesW
        get_attributes.argtypes = [ctypes.c_wchar_p]
        get_attributes.restype = ctypes.c_uint32
        attributes = get_attributes(str(snapshot.path))
        readonly = attributes != 0xFFFFFFFF and bool(attributes & 0x1)
        identity_verified = snapshot.manifest.snapshot_identity == snapshot.verified_input.identity
        hash_verified = snapshot.manifest.snapshot_sha256 == _hash(snapshot.path)
        if not (readonly and identity_verified and hash_verified):
            raise RuntimeError("native smoke snapshot verification was incomplete")
    finally:
        _cleanup_probe_resources(
            store, source=source, snapshot=snapshot, workspace=workspace, report=report
        )

    return {
        "source_mutation_rejected": True,
        "path_replacement_rejected": True,
        "junction_rejected": junction_rejected,
        "snapshot_readonly_verified": readonly,
        "snapshot_identity_verified": identity_verified,
        "snapshot_hash_verified": hash_verified,
        "verified_before_normalization": True,
    }


def _run_process_tree_probe(timeout_seconds: float) -> None:
    from voiceink_win.application import CancellationTokenSource
    from voiceink_win.infrastructure import ProcessCancelled, WindowsJobObjectProcessRunner

    if timeout_seconds <= 0:
        raise RuntimeError("VOICEINK_NATIVE_SMOKE_CANCEL_AFTER_SECONDS must be positive")
    with tempfile.TemporaryDirectory(prefix="voiceink-native-tree-") as temporary:
        child_pid_path = Path(temporary) / "child.pid"
        child_code = (
            "import subprocess,sys,time; "
            "time.sleep(1); "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)']); "
            "open(sys.argv[1],'w').write(str(child.pid)); "
            "time.sleep(300)"
        )
        cancellation = CancellationTokenSource()
        outcome: list[BaseException | object] = []
        runner = WindowsJobObjectProcessRunner()

        def run() -> None:
            try:
                outcome.append(
                    runner.run(
                        [sys.executable, "-c", child_code, str(child_pid_path)],
                        timeout=max(30.0, timeout_seconds),
                        cancellation=cancellation.token,
                        max_stdout_bytes=1024,
                        max_stderr_bytes=1024,
                    )
                )
            except BaseException as error:
                outcome.append(error)

        worker = Thread(target=run, name="native-smoke-process-tree")
        worker.start()
        deadline = monotonic() + max(10.0, timeout_seconds)
        while not child_pid_path.exists() and monotonic() < deadline:
            sleep(0.05)
        if not child_pid_path.exists():
            cancellation.cancel()
            worker.join(10)
            raise RuntimeError("native Job Object probe did not create a child process")
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        cancellation.cancel()
        worker.join(10)
        if worker.is_alive() or not outcome or not isinstance(outcome[0], ProcessCancelled):
            raise RuntimeError("native Job Object probe did not cancel the parent process")
        tasklist = subprocess.run(
            ["tasklist", "/FI", f"PID eq {child_pid}", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if str(child_pid) in tasklist.stdout:
            raise RuntimeError("native Job Object probe left a descendant process alive")


def _run_asr_cancellation_probe(asr, audio, cancel_after: float) -> None:
    from voiceink_win.application import CancellationTokenSource
    from voiceink_win.domain import AsrRequest, CancellationError

    if cancel_after <= 0:
        raise RuntimeError("VOICEINK_NATIVE_SMOKE_ASR_CANCEL_AFTER_SECONDS must be positive")
    cancellation = CancellationTokenSource()
    outcome: list[BaseException | object] = []

    def transcribe() -> None:
        try:
            outcome.append(
                asr.transcribe(
                    AsrRequest(
                        audio,
                        request_id="native-smoke-cancel",
                        deadline=monotonic() + 120,
                        cancellation=cancellation.token,
                    )
                )
            )
        except BaseException as error:
            outcome.append(error)

    worker = Thread(target=transcribe, name="native-smoke-asr-cancel")
    worker.start()
    if not asr.wait_active(10):
        raise RuntimeError("configured ASR cancellation probe did not enter the runtime")
    timer = Timer(cancel_after, cancellation.cancel)
    timer.start()
    try:
        worker.join(120)
        if worker.is_alive():
            raise RuntimeError("configured ASR cancellation probe did not finish")
        if not outcome or not isinstance(outcome[0], CancellationError):
            raise RuntimeError("configured ASR cancellation probe published a result")
        if not asr.wait_idle(120):
            raise RuntimeError("configured ASR cancellation probe left an active request")
    finally:
        timer.cancel()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"native smoke failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from error
