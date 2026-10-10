from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import voiceink_win.__main__ as cli
import voiceink_win.composition as composition
from voiceink_win.composition import (
    BackendApplication,
    ImportedMediaConfiguration,
    RuntimePaths,
    build_application,
)
from voiceink_win.domain import (
    AsrCapabilities,
    Attempt,
    ConfigurationError,
    HealthStatus,
    ImportedTranscriptionResult,
    JobId,
    ProcessingMetadata,
    RuntimeDiagnostics,
    RuntimeHealth,
    RuntimeUnavailableError,
    Stage,
    Success,
    TranscriptResult,
)
from voiceink_win.infrastructure import FfmpegArtifactManifest


def _runtime_files(tmp_path: Path) -> tuple[Path, Path, str]:
    executable = tmp_path / "runtime" / "nemo-speech.exe"
    model = tmp_path / "models" / "parakeet.gguf"
    executable.parent.mkdir()
    model.parent.mkdir()
    executable.write_bytes(b"runtime")
    model.write_bytes(b"model")
    lock = {
        "schema": "voiceink.runtime.artifact-lock.v1",
        "version": 1,
        "install_root": str(tmp_path),
        "artifacts": {
            "sidecar": {
                "kind": "executable",
                "version": "release-1",
                "provenance_url": "https://example.invalid/runtime",
                "sha256": hashlib.sha256(b"runtime").hexdigest(),
                "license": "Apache-2.0",
                "allowed_path": str(executable),
            },
            "parakeet-tdt-0.6b-v3.oss-align.q8_0": {
                "kind": "model",
                "version": "model-1",
                "provenance_url": "https://example.invalid/model",
                "sha256": hashlib.sha256(b"model").hexdigest(),
                "license": "CC-BY-4.0",
                "allowed_path": str(model),
            },
        },
    }
    lock_path = tmp_path / "artifact-lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    manifest_path = tmp_path / "runtime-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "voiceink.runtime.manifest.v1",
                "version": 1,
                "executable_artifact_id": "sidecar",
                "model_artifact_id": "parakeet-tdt-0.6b-v3.oss-align.q8_0",
                "executable": str(executable),
                "model": str(model),
                "backend": "cpu",
                "endpoint": "ephemeral-loopback",
            }
        ),
        encoding="utf-8",
    )
    return manifest_path, lock_path, hashlib.sha256(lock_path.read_bytes()).hexdigest()


def _absolute_test_path(*parts: str) -> Path:
    return Path.cwd().joinpath("composition-test", *parts)


def test_runtime_paths_require_all_trusted_configuration_values() -> None:
    with pytest.raises(ConfigurationError, match="VOICEINK_ARTIFACT_LOCK_SHA256"):
        RuntimePaths.from_environment(
            {
                "VOICEINK_RUNTIME_MANIFEST": "manifest.json",
                "VOICEINK_ARTIFACT_LOCK": "lock.json",
            }
        )


def test_build_application_wires_runtime_and_application_service(tmp_path: Path) -> None:
    manifest, artifact_lock, lock_sha256 = _runtime_files(tmp_path)

    application = build_application(
        manifest,
        artifact_lock,
        lock_sha256,
        endpoint="http://127.0.0.1:45678",
    )
    try:
        assert application.capabilities().model_id == "parakeet-tdt-0.6b-v3.oss-align.q8_0"
        assert application.health().status is HealthStatus.STARTING
    finally:
        application.close()

    assert application.health().status is HealthStatus.CLOSED


class _FakeApplication:
    def __init__(
        self,
        status: HealthStatus,
        close_error: Exception | None = None,
        start_error: Exception | None = None,
    ) -> None:
        self._status = status
        self._close_error = close_error
        self._start_error = start_error
        self.diagnostics = {
            "primary_failure": {
                "startup_phase": "resume",
                "operation": "resume_process",
                "error_code": "configuration",
                "error_type": "ConfigurationError",
            },
            "cleanup_outcome": "complete",
            "records": [],
            "cleanup_failures": [],
        }

    @property
    def endpoint(self) -> str:
        return "http://127.0.0.1:45678"

    def start(self) -> None:
        if self._start_error is not None:
            raise self._start_error

    def health(self) -> RuntimeHealth:
        return RuntimeHealth(self._status, "fake health", "cpu")

    def capabilities(self) -> AsrCapabilities:
        return AsrCapabilities("parakeet-tdt-0.6b-v3.oss-align.q8_0", ("cpu",), True)

    def close(self) -> None:
        if self._close_error is not None:
            raise self._close_error


def test_cli_rejects_non_ready_application(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "_build_from_args", lambda _args: _FakeApplication(HealthStatus.FAILED)
    )

    assert cli.main(["--once"]) == 3


def test_cli_reports_cleanup_failure_as_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    application = _FakeApplication(
        HealthStatus.READY,
        RuntimeUnavailableError("cleanup remains pending"),
    )
    monkeypatch.setattr(cli, "_build_from_args", lambda _args: application)

    assert cli.main(["--once"]) == 4


def test_cli_reports_bounded_runtime_diagnostics_on_start_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    application = _FakeApplication(
        HealthStatus.FAILED,
        start_error=RuntimeUnavailableError("sidecar did not become ready"),
    )
    monkeypatch.setattr(cli, "_build_from_args", lambda _args: application)

    assert cli.main(["--once"]) == 3

    output = json.loads(capsys.readouterr().out)
    assert output["diagnostics"]["primary_failure"]["operation"] == "resume_process"
    assert "records" in output["diagnostics"]


class _LifecycleFake:
    def __init__(
        self,
        events: list[str],
        label: str,
        *,
        start_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self._events = events
        self._label = label
        self._start_error = start_error
        self._close_error = close_error

    def start(self) -> None:
        self._events.append(f"{self._label}.start")
        if self._start_error is not None:
            raise self._start_error

    def stop_accepting(self) -> None:
        self._events.append("proxy.stop_accepting")

    def close(self) -> None:
        self._events.append(f"{self._label}.close")
        if self._close_error is not None:
            raise self._close_error


class _AsrLifecycleFake:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self._events.append("asr.close")


def test_application_stops_ingress_before_draining_asr() -> None:
    events: list[str] = []
    asr = _AsrLifecycleFake(events)
    application = BackendApplication(
        asr,  # type: ignore[arg-type]
        _LifecycleFake(events, "runtime"),  # type: ignore[arg-type]
        _LifecycleFake(events, "proxy"),  # type: ignore[arg-type]
    )

    application.close()

    assert events == ["proxy.stop_accepting", "asr.close", "proxy.close"]
    assert asr.close_calls == 1


def test_application_rolls_back_workers_when_runtime_start_fails() -> None:
    events: list[str] = []
    application = BackendApplication(
        _AsrLifecycleFake(events),  # type: ignore[arg-type]
        _LifecycleFake(events, "runtime", start_error=RuntimeError("startup failed")),  # type: ignore[arg-type]
        _LifecycleFake(events, "proxy"),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="startup failed"):
        application.start()

    assert events == [
        "proxy.start",
        "runtime.start",
        "proxy.stop_accepting",
        "asr.close",
        "proxy.close",
    ]


def test_imported_media_configuration_is_disabled_or_rejected_explicitly() -> None:
    ffmpeg = _absolute_test_path("ffmpeg.exe")
    workspace = _absolute_test_path("workspace")
    assert ImportedMediaConfiguration.from_environment({}) is None
    assert (
        ImportedMediaConfiguration.from_environment({"VOICEINK_FFMPEG_PATH": str(ffmpeg)}) is None
    )

    assert (
        ImportedMediaConfiguration.from_environment(
            {
                "VOICEINK_FFMPEG_PATH": str(ffmpeg),
                "VOICEINK_IMPORT_WORKSPACE_ROOT": str(workspace),
                "VOICEINK_FFMPEG_VERSION": "ffmpeg-test",
                "VOICEINK_FFMPEG_PROVENANCE_URL": "https://example.invalid/ffmpeg",
                "VOICEINK_FFMPEG_SHA256": "1" * 64,
                "VOICEINK_FFMPEG_LICENSE": "GPL-3.0-or-later",
            }
        )
        is not None
    )

    with pytest.raises(ConfigurationError, match="metadata path does not match"):
        ImportedMediaConfiguration(
            ffmpeg,
            FfmpegArtifactManifest(
                "ffmpeg-test",
                "https://example.invalid/ffmpeg",
                "0" * 64,
                "GPL-3.0-or-later",
                _absolute_test_path("other-ffmpeg.exe"),
            ),
            workspace,
        )


def test_environment_build_supports_ffmpeg_path_only_and_complete_import_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []
    runtime_manifest = _absolute_test_path("runtime.json")
    artifact_lock = _absolute_test_path("lock.json")
    ffmpeg = _absolute_test_path("ffmpeg.exe")
    workspace = _absolute_test_path("workspace")
    imports = _absolute_test_path("imports")

    def fake_build(*args, **kwargs):
        captured.append(kwargs["imported_media"])
        return object()

    monkeypatch.setattr(composition, "build_application", fake_build)
    base = {
        "VOICEINK_RUNTIME_MANIFEST": str(runtime_manifest),
        "VOICEINK_ARTIFACT_LOCK": str(artifact_lock),
        "VOICEINK_ARTIFACT_LOCK_SHA256": "0" * 64,
        "VOICEINK_FFMPEG_PATH": str(ffmpeg),
    }

    assert composition.build_application_from_environment(environ=base) is not None
    assert captured == [None]

    complete = {
        **base,
        "VOICEINK_IMPORT_WORKSPACE_ROOT": str(workspace),
        "VOICEINK_IMPORT_ROOTS": str(imports),
        "VOICEINK_FFMPEG_VERSION": "ffmpeg-test",
        "VOICEINK_FFMPEG_PROVENANCE_URL": "https://example.invalid/ffmpeg",
        "VOICEINK_FFMPEG_SHA256": "1" * 64,
        "VOICEINK_FFMPEG_LICENSE": "GPL-3.0-or-later",
    }

    assert composition.build_application_from_environment(environ=complete) is not None
    configuration = captured[1]
    assert isinstance(configuration, ImportedMediaConfiguration)
    assert configuration.ffmpeg_artifact.sha256 == "1" * 64


def test_disabled_imported_media_facade_rejects_operations() -> None:
    application = BackendApplication(
        _AsrLifecycleFake([]),  # type: ignore[arg-type]
        _LifecycleFake([], "runtime"),  # type: ignore[arg-type]
        _LifecycleFake([], "proxy"),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeUnavailableError, match="not configured"):
        application.submit("/tmp/input.wav")
    with pytest.raises(RuntimeUnavailableError, match="not configured"):
        application.status_or_wait(JobId("job"))
    with pytest.raises(RuntimeUnavailableError, match="not configured"):
        application.cancel(JobId("job"))

    application.close()


class _ImportedServiceFake:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.submitted: tuple[str, object] | None = None
        self.job_id = JobId("import-job")
        self.close_errors: list[BaseException] = []
        self.result = Success(
            "succeeded",
            self.job_id,
            Attempt(1),
            ImportedTranscriptionResult(
                self.job_id,
                "input.wav",
                TranscriptResult("transcript", 0.1),
                ProcessingMetadata(0.1, 1, (), 0.1, Stage.SUCCEEDED),
                RuntimeDiagnostics(),
            ),
        )

    def submit(self, path: str, options=None):
        self.submitted = (path, options)
        return self.job_id

    def wait(self, job_id, timeout=None):
        assert job_id == self.job_id
        assert timeout == 1.5
        return self.result

    def cancel(self, job_id):
        assert job_id == self.job_id
        return True

    def close(self, *, close_asr: bool = True) -> None:
        del close_asr
        self._events.append("imports.close")
        if self.close_errors:
            raise self.close_errors.pop(0)


def test_application_retries_import_close_before_closing_shared_asr() -> None:
    events: list[str] = []
    imports = _ImportedServiceFake(events)
    imports.close_errors.append(RuntimeError("import shutdown failed"))
    asr = _AsrLifecycleFake(events)
    application = BackendApplication(
        asr,  # type: ignore[arg-type]
        _LifecycleFake(events, "runtime"),  # type: ignore[arg-type]
        _LifecycleFake(events, "proxy"),  # type: ignore[arg-type]
        imports,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="import shutdown failed"):
        application.close()

    assert asr.close_calls == 0
    assert events == ["proxy.stop_accepting", "imports.close", "proxy.close"]

    application.close()

    assert asr.close_calls == 1
    assert events == [
        "proxy.stop_accepting",
        "imports.close",
        "proxy.close",
        "proxy.stop_accepting",
        "imports.close",
        "asr.close",
    ]


def test_application_does_not_retry_successful_asr_close_after_proxy_failure() -> None:
    events: list[str] = []
    asr = _AsrLifecycleFake(events)
    application = BackendApplication(
        asr,  # type: ignore[arg-type]
        _LifecycleFake(events, "runtime"),  # type: ignore[arg-type]
        _LifecycleFake(events, "proxy", close_error=RuntimeError("proxy close failed")),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="proxy close failed"):
        application.close()
    with pytest.raises(RuntimeError, match="proxy close failed"):
        application.close()

    assert asr.close_calls == 1
    assert events == [
        "proxy.stop_accepting",
        "asr.close",
        "proxy.close",
        "proxy.stop_accepting",
        "proxy.close",
    ]


def test_imported_media_facade_exposes_only_typed_job_operations() -> None:
    events: list[str] = []
    imports = _ImportedServiceFake(events)
    asr = _AsrLifecycleFake(events)
    application = BackendApplication(
        asr,  # type: ignore[arg-type]
        _LifecycleFake(events, "runtime"),  # type: ignore[arg-type]
        _LifecycleFake(events, "proxy"),  # type: ignore[arg-type]
        imports,  # type: ignore[arg-type]
    )

    assert application.submit("input.wav") == imports.job_id
    assert imports.submitted == ("input.wav", None)
    assert application.status_or_wait(imports.job_id, timeout=1.5) is imports.result
    assert application.cancel(imports.job_id)

    application.close()

    assert events == ["proxy.stop_accepting", "imports.close", "asr.close", "proxy.close"]
    assert asr.close_calls == 1


def test_application_rolls_back_imported_service_and_asr_on_startup_failure() -> None:
    events: list[str] = []
    imports = _ImportedServiceFake(events)
    asr = _AsrLifecycleFake(events)
    application = BackendApplication(
        asr,  # type: ignore[arg-type]
        _LifecycleFake(events, "runtime", start_error=RuntimeError("startup failed")),  # type: ignore[arg-type]
        _LifecycleFake(events, "proxy"),  # type: ignore[arg-type]
        imports,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="startup failed"):
        application.start()

    assert events == [
        "proxy.start",
        "runtime.start",
        "proxy.stop_accepting",
        "imports.close",
        "asr.close",
        "proxy.close",
    ]
    assert asr.close_calls == 1


def test_composition_rollback_closes_imported_service_and_asr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_manifest, artifact_lock, lock_sha256 = _runtime_files(tmp_path)
    events: list[str] = []
    asr = _AsrLifecycleFake(events)
    imports = _ImportedServiceFake(events)

    class FakeProxy:
        endpoint = "http://127.0.0.1:45678"

        def __init__(self, target_endpoint, *, listen_endpoint=None) -> None:
            del target_endpoint, listen_endpoint

        def close(self) -> None:
            events.append("proxy.close")

    class FakeRuntime:
        def __init__(self, *args) -> None:
            del args

    def fail_application(*args) -> None:
        del args
        raise RuntimeError("application construction failed")

    monkeypatch.setattr(composition, "LoopbackProxy", FakeProxy)
    monkeypatch.setattr(composition, "NeMoSidecarRuntime", FakeRuntime)
    monkeypatch.setattr(composition, "AsrApplicationService", lambda runtime: asr)
    monkeypatch.setattr(
        composition,
        "_build_imported_media_service",
        lambda configuration, shared_asr: imports,
    )
    monkeypatch.setattr(composition, "BackendApplication", fail_application)

    imported_configuration = ImportedMediaConfiguration(
        tmp_path / "ffmpeg",
        FfmpegArtifactManifest(
            "ffmpeg-test",
            "https://example.invalid/ffmpeg",
            hashlib.sha256(b"ffmpeg").hexdigest(),
            "GPL-3.0-or-later",
            tmp_path / "ffmpeg",
        ),
        tmp_path / "work",
    )
    with pytest.raises(RuntimeError, match="application construction failed"):
        build_application(
            runtime_manifest,
            artifact_lock,
            lock_sha256,
            imported_media=imported_configuration,
        )

    assert events == ["imports.close", "asr.close", "proxy.close"]
    assert asr.close_calls == 1


def test_composition_wires_one_asr_service_into_imported_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_manifest, artifact_lock, lock_sha256 = _runtime_files(tmp_path)
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_bytes(b"ffmpeg")
    ffmpeg_artifact = FfmpegArtifactManifest(
        "ffmpeg-test",
        "https://example.invalid/ffmpeg",
        hashlib.sha256(b"ffmpeg").hexdigest(),
        "GPL-3.0-or-later",
        ffmpeg,
    )
    configuration = ImportedMediaConfiguration(
        ffmpeg,
        ffmpeg_artifact,
        tmp_path / "work",
    )
    captured: dict[str, object] = {}

    class FakeProxy:
        endpoint = "http://127.0.0.1:45678"

        def __init__(self, target_endpoint, *, listen_endpoint=None) -> None:
            del target_endpoint, listen_endpoint

        def close(self) -> None:
            return None

        def stop_accepting(self) -> None:
            return None

    class FakeRuntime:
        def __init__(self, *args) -> None:
            del args

        def capabilities(self):
            return AsrCapabilities("model", ("cpu",), False)

        def health(self):
            return RuntimeHealth(HealthStatus.STARTING, "starting", "cpu")

        def close(self, deadline=None) -> None:
            del deadline

    class FakeAsr:
        def __init__(self, runtime) -> None:
            captured["asr_service"] = self
            captured["asr_runtime"] = runtime

        def close(self, **kwargs) -> None:
            captured["asr_close"] = kwargs

    class FakeArtifact:
        executable = ffmpeg

        @classmethod
        def verify(cls, executable, manifest):
            captured["artifact"] = (executable, manifest)
            return cls()

    class FakeNormalizer:
        def __init__(self, *, executable) -> None:
            captured["normalizer_artifact"] = executable

    class FakeImports:
        def __init__(self, normalizer, asr, store) -> None:
            captured["normalizer"] = normalizer
            captured["asr"] = asr
            captured["store"] = store

        def close(self, *, close_asr: bool = True) -> None:
            captured["imports_close_asr"] = close_asr
            captured["imports_close"] = True

    store = object()
    monkeypatch.setattr(composition, "LoopbackProxy", FakeProxy)
    monkeypatch.setattr(composition, "NeMoSidecarRuntime", FakeRuntime)
    monkeypatch.setattr(composition, "AsrApplicationService", FakeAsr)
    monkeypatch.setattr(composition, "VerifiedFfmpegArtifact", FakeArtifact)
    monkeypatch.setattr(composition, "SubprocessMediaNormalizer", FakeNormalizer)
    monkeypatch.setattr(composition, "create_media_snapshot_store", lambda *args, **kwargs: store)
    monkeypatch.setattr(composition, "ImportedMediaTranscriptionService", FakeImports)

    application = build_application(
        runtime_manifest,
        artifact_lock,
        lock_sha256,
        imported_media=configuration,
    )
    application.close()

    assert captured["asr"] is captured["asr_service"]
    assert captured["normalizer_artifact"] is not None
    assert captured["artifact"][1] is ffmpeg_artifact  # type: ignore[index]
    assert captured["store"] is store
    assert captured["imports_close_asr"] is False
    assert captured["imports_close"] is True
    assert captured["asr_close"] == {}
