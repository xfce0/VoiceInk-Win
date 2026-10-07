from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.native_smoke as native_smoke
from scripts.native_smoke import (
    _build_native_smoke_application,
    _cleanup_status,
    _run_snapshot_security_probes_with_store,
    _safe_rtfx,
    _success_status,
    _transcribe_with_timing,
)
from voiceink_win.domain import CanonicalAudio, InvalidSourceError, SourceChangedError
from voiceink_win.infrastructure import RuntimeArtifactManifest

ROOT = Path(__file__).resolve().parents[1]


def _artifact(path: Path, digest: str) -> RuntimeArtifactManifest:
    return RuntimeArtifactManifest(
        version="release-1",
        provenance_url="https://example.invalid/runtime",
        sha256=digest,
        license="Apache-2.0",
        allowed_path=path,
    )


def test_native_smoke_selects_named_sidecar_executable() -> None:
    workflow = (ROOT / ".github/workflows/native-smoke.yml").read_text(encoding="utf-8")

    assert "-Filter nemo-speech.exe" in workflow
    assert "-Filter *.exe" not in workflow
    assert "expected exactly one nemo-speech.exe" in workflow
    assert "expected exactly one ffmpeg.exe" in workflow
    assert "persist-credentials: false" in workflow
    assert "Invoke-WebRequest -Uri $url -OutFile $target -TimeoutSec 120" in workflow
    assert "timeout-minutes: 30" in workflow
    assert "permissions:" in workflow and "contents: read" in workflow
    assert "native-smoke-setup.log" in workflow


def test_snapshot_security_probes_allow_an_existing_inputs_directory(
    tmp_path: Path, monkeypatch
) -> None:
    original = b"native snapshot security fixture"
    digest = hashlib.sha256(original).hexdigest()
    root = tmp_path / "snapshot-security"
    inputs = root / "inputs"
    inputs.mkdir(parents=True)

    class Store:
        def __init__(self) -> None:
            self.snapshot_calls = 0

        def validate_source(self, path: Path, *, max_bytes: int):
            del max_bytes
            if path.parent.name == "junction":
                raise InvalidSourceError("junction rejected")
            return SimpleNamespace(path=path, identity=f"identity-{path.name}")

        def create_workspace(self, job_id, attempt):
            return SimpleNamespace(path=root / f"workspace-{job_id}-{attempt}")

        def snapshot(self, source, workspace):
            del workspace
            self.snapshot_calls += 1
            if self.snapshot_calls < 3:
                raise SourceChangedError("changed")
            return SimpleNamespace(
                path=source.path,
                manifest=SimpleNamespace(
                    snapshot_identity=source.identity,
                    snapshot_sha256=digest,
                ),
                verified_input=SimpleNamespace(identity=source.identity),
            )

        def verify(self, snapshot):
            return snapshot

        def release_snapshot(self, snapshot):
            del snapshot

        def cleanup(self, workspace):
            del workspace

        def release_source(self, source):
            del source

    class GetFileAttributes:
        argtypes = None
        restype = None

        def __call__(self, path):
            del path
            return 1

    class Kernel32:
        GetFileAttributesW = GetFileAttributes()

    def fake_run(arguments, **kwargs):
        del kwargs
        Path(arguments[-2]).mkdir()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(native_smoke.subprocess, "run", fake_run)
    monkeypatch.setattr(
        native_smoke.ctypes,
        "windll",
        SimpleNamespace(kernel32=Kernel32()),
        raising=False,
    )

    result = _run_snapshot_security_probes_with_store(Store(), root)

    assert result["snapshot_hash_verified"]
    assert result["junction_rejected"]


def test_native_smoke_records_cold_and_warm_timing_and_rejects_empty_transcript(
    monkeypatch,
) -> None:
    class Asr:
        def __init__(self) -> None:
            self.calls = 0

        def transcribe(self, request):
            self.calls += 1
            return SimpleNamespace(
                text="hello" if self.calls == 1 else "warm hello",
                duration=2.0,
            )

    timestamps = iter((10.0, 10.5, 20.0, 21.0))
    monkeypatch.setattr(native_smoke, "monotonic", lambda: next(timestamps))
    audio = CanonicalAudio(b"\x00\x00" * 16)
    asr = Asr()

    _, cold = _transcribe_with_timing(asr, audio, "cold")
    _, warm = _transcribe_with_timing(asr, audio, "warm")

    assert asr.calls == 2
    assert cold == {"elapsed_seconds": 0.5, "rtfx": 4.0}
    assert warm == {"elapsed_seconds": 1.0, "rtfx": 2.0}
    assert _safe_rtfx(2.0, 0.0) is None

    class EmptyAsr:
        def transcribe(self, request):
            del request
            return SimpleNamespace(text="", duration=2.0)

    monkeypatch.setattr(native_smoke, "monotonic", lambda: 30.0)
    with pytest.raises(RuntimeError, match="empty transcript"):
        _transcribe_with_timing(EmptyAsr(), audio, "warm")


@pytest.mark.parametrize(
    "key",
    [
        "application_close_error",
        "proxy_close_error",
        "events_error",
        "report_write_error",
    ],
)
def test_native_smoke_cleanup_status_fails_for_each_close_error(key: str) -> None:
    assert _cleanup_status({key: "ExecutionError"}) == "failed"


def test_native_smoke_main_prioritizes_cleanup_exit_code(monkeypatch) -> None:
    def fail_run(_report_path, report):
        report["application_close_error"] = "ExecutionError"
        raise RuntimeError("pipeline failed")

    monkeypatch.setattr(native_smoke, "_run", fail_run)
    monkeypatch.setattr(native_smoke, "_write_report_best_effort", lambda *_args: None)

    assert native_smoke.main() == 4


def test_native_smoke_event_writer_failure_is_quality_exit(monkeypatch) -> None:
    monkeypatch.setattr(
        native_smoke,
        "_run",
        lambda *_args: (_ for _ in ()).throw(native_smoke.NativeSmokeDiagnosticsError("writer")),
    )
    monkeypatch.setattr(native_smoke, "_write_report_best_effort", lambda *_args: None)

    assert native_smoke.main() == 4


def test_native_smoke_event_writer_open_failure_is_diagnostics_exit(monkeypatch, tmp_path) -> None:
    class FailingEventWriter:
        def __init__(self, _path) -> None:
            raise PermissionError("events unavailable")

    monkeypatch.setattr(
        "voiceink_win.infrastructure.JsonlEventWriter",
        FailingEventWriter,
    )
    executable = tmp_path / "nemo-speech.exe"
    model = tmp_path / "parakeet.gguf"
    runtime_configuration = SimpleNamespace(
        executable=executable,
        model=model,
        model_id="parakeet-custom-model",
        backend="cpu",
        manifest=SimpleNamespace(
            executable_artifact_id="runtime",
            model_artifact_id="model",
        ),
    )

    with pytest.raises(native_smoke.NativeSmokeDiagnosticsError):
        _build_native_smoke_application(
            runtime_configuration,
            _artifact(executable, "a" * 64),
            _artifact(model, "b" * 64),
        )


def test_native_smoke_reports_technical_success_without_release_quality_pass() -> None:
    report = {"quality_gate": {"status": "not_evaluated"}}

    assert _success_status(report) == "technical_passed"


def test_native_smoke_workflow_pins_cpu_backend_and_rejects_override() -> None:
    workflow = (ROOT / ".github/workflows/native-smoke.yml").read_text(encoding="utf-8")

    assert '"backend": "cpu"' in (
        ROOT / ".github/native-smoke/artifact-lock.template.json"
    ).read_text(encoding="utf-8")
    assert 'Pinned "VOICEINK_SIDECAR_BACKEND" $sidecar.backend' in workflow
    assert "backend = $backend" in workflow
    assert 'VOICEINK_SIDECAR_BACKEND)) { "cpu" } else' not in workflow
