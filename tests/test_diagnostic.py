from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from pathlib import Path

from voiceink_win.diagnostic import DiagnosticLogger, run_diagnostic
from voiceink_win.infrastructure import FakeMediaNormalizer, FakeSnapshotStore


def _arguments(source: Path, ffmpeg: Path, manifest: Path, logs: Path) -> Namespace:
    return Namespace(
        input=source,
        ffmpeg=str(ffmpeg),
        ffmpeg_manifest=manifest,
        ffmpeg_sha256=None,
        logs_dir=logs,
    )


def _diagnostic_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "input.wav"
    source.write_bytes(b"fixture")
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_bytes(b"ffmpeg")
    manifest = tmp_path / "ffmpeg.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "test",
                "provenance_url": "https://example.test/ffmpeg",
                "sha256": hashlib.sha256(b"ffmpeg").hexdigest(),
                "license": "test",
            }
        ),
        encoding="ascii",
    )
    return source, ffmpeg, manifest


def test_diagnostic_does_not_publish_success_when_cleanup_fails(tmp_path: Path) -> None:
    source, ffmpeg, manifest = _diagnostic_fixture(tmp_path)
    store = FakeSnapshotStore(tmp_path / "work", cleanup_error=True)
    result = run_diagnostic(
        _arguments(source, ffmpeg, manifest, tmp_path / "logs"),
        store_factory=lambda root: store,
        normalizer_factory=lambda executable: FakeMediaNormalizer(),
    )

    assert result == 1
    log_files = tuple((tmp_path / "logs").rglob("diagnostic.jsonl"))
    content = log_files[0].read_text(encoding="utf-8")
    assert "diagnostic.succeeded" not in content
    assert "diagnostic.cleanup_failed" in content
    assert '"workspace.cleanup"' in content
    assert '"workspace.verify"' in content


def test_diagnostic_publishes_success_only_after_confirmed_cleanup(tmp_path: Path) -> None:
    source, ffmpeg, manifest = _diagnostic_fixture(tmp_path)
    result = run_diagnostic(
        _arguments(source, ffmpeg, manifest, tmp_path / "logs"),
        store_factory=lambda root: FakeSnapshotStore(root),
        normalizer_factory=lambda executable: FakeMediaNormalizer(),
    )

    assert result == 0
    content = next((tmp_path / "logs").rglob("diagnostic.jsonl")).read_text(encoding="utf-8")
    assert "diagnostic.succeeded" in content


def test_diagnostic_accumulates_release_and_close_failures(tmp_path: Path) -> None:
    source, ffmpeg, manifest = _diagnostic_fixture(tmp_path)

    class FailingStore(FakeSnapshotStore):
        def release_source(self, source) -> None:
            super().release_source(source)
            raise OSError("source path and transcript must not be logged")

        def close(self, *, timeout: float) -> None:
            del timeout
            raise OSError("store close failed")

    store = FailingStore(tmp_path / "work")
    result = run_diagnostic(
        _arguments(source, ffmpeg, manifest, tmp_path / "logs"),
        store_factory=lambda root: store,
        normalizer_factory=lambda executable: FakeMediaNormalizer(),
    )

    assert result == 1
    content = next((tmp_path / "logs").rglob("diagnostic.jsonl")).read_text(encoding="utf-8")
    assert '"source.release"' in content
    assert '"store.close"' in content
    assert "source path and transcript" not in content


def test_diagnostic_redacts_paths_sensitive_fields_and_error_messages(tmp_path: Path) -> None:
    directory = tmp_path / "logs"
    logger = DiagnosticLogger(directory, redactions=(str(tmp_path),))
    try:
        try:
            raise ValueError("raw transcript from C:\\Users\\alice\\secret.wav")
        except ValueError as error:
            logger.exception("diagnostic.failed", error)
        logger.event(
            "diagnostic.details",
            traceback="/Users/alice/private.wav C:\\Users\\alice\\private.wav /var/tmp/x",
            transcript="raw transcript",
        )
    finally:
        logger.close()

    content = "\n".join(path.read_text(encoding="utf-8") for path in directory.iterdir())
    assert "C:\\Users\\alice" not in content
    assert "/Users/alice" not in content
    assert "/var/tmp/x" not in content
    assert "raw transcript" not in content
    assert "error_type" in content
    assert "error_code" in content
