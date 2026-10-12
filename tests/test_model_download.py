from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import voiceink_win.infrastructure.model_download as model_download
from voiceink_win.infrastructure import ModelDownloadError, ModelDownloadService


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _size: int) -> bytes:
        payload, self._payload = self._payload, b""
        return payload


def _patch_artifact(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    monkeypatch.setattr(
        model_download,
        "TRUSTED_PACKAGE_ARTIFACTS",
        {
            model_download.PARAKEET_ARTIFACT_ID: {
                "version": "test",
                "url": "https://example.invalid/model.gguf",
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        },
    )


def test_model_download_verifies_atomically_and_can_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"verified model"
    _patch_artifact(monkeypatch, payload)
    service = ModelDownloadService(
        environ={"LOCALAPPDATA": str(tmp_path)},
        opener=lambda *_args, **_kwargs: _Response(payload),
    )
    try:
        target = service.download().result(timeout=1)

        assert target == tmp_path / "VoiceInk" / "models" / "parakeet.gguf"
        assert target.read_bytes() == payload
        assert service.is_downloaded

        service.delete()

        assert not target.exists()
        assert not service.is_downloaded
    finally:
        service.close()


def test_model_download_rejects_bad_checksum_without_leaving_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_artifact(monkeypatch, b"expected model")
    service = ModelDownloadService(
        environ={"LOCALAPPDATA": str(tmp_path)},
        opener=lambda *_args, **_kwargs: _Response(b"wrong model"),
    )
    try:
        with pytest.raises(ModelDownloadError, match="checksum"):
            service.download().result(timeout=1)

        assert not service.target.exists()
        assert not list(service.target.parent.glob(".parakeet-*.download"))
    finally:
        service.close()
