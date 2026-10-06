from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts.native_smoke import (
    NATIVE_SMOKE_READINESS_TIMEOUT,
    _build_native_smoke_configs,
)
from voiceink_win.infrastructure import RuntimeArtifactManifest, SidecarConfig

ROOT = Path(__file__).resolve().parents[1]


def _artifact(path: Path, digest: str) -> RuntimeArtifactManifest:
    return RuntimeArtifactManifest(
        version="release-1",
        provenance_url="https://example.invalid/runtime",
        sha256=digest,
        license="Apache-2.0",
        allowed_path=path,
    )


def test_native_smoke_uses_manifest_model_and_cold_start_readiness_timeout(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "nemo-speech.exe"
    model = tmp_path / "parakeet.gguf"
    executable_manifest = _artifact(executable, "a" * 64)
    model_manifest = _artifact(model, "b" * 64)
    runtime_configuration = SimpleNamespace(
        executable=executable,
        model=model,
        model_id="parakeet-custom-model",
        backend="cpu",
    )

    sidecar_config, subprocess_config = _build_native_smoke_configs(
        runtime_configuration,
        "http://127.0.0.1:8123",
        executable_manifest,
        model_manifest,
    )

    assert NATIVE_SMOKE_READINESS_TIMEOUT == 60.0
    assert SidecarConfig("http://127.0.0.1:8123").readiness_timeout == 10.0
    assert sidecar_config.model_id == "parakeet-custom-model"
    assert sidecar_config.readiness_timeout == 60.0
    assert sidecar_config.require_model_attestation
    assert subprocess_config.model_id == "parakeet-custom-model"


def test_native_smoke_selects_named_sidecar_executable() -> None:
    workflow = (ROOT / ".github/workflows/native-smoke.yml").read_text(encoding="utf-8")

    assert "-Filter nemo-speech.exe" in workflow
    assert "-Filter *.exe" not in workflow
    assert "expected exactly one nemo-speech.exe" in workflow
    assert "expected exactly one ffmpeg.exe" in workflow
    assert "Invoke-WebRequest -Uri $url -OutFile $target -TimeoutSec 120" in workflow
    assert "timeout-minutes: 30" in workflow
    assert "permissions:" in workflow and "contents: read" in workflow
    assert "native-smoke-setup.log" in workflow
