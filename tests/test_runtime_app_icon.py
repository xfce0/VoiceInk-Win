from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
except ImportError:
    if os.environ.get("VOICEINK_GUI_TESTS") == "1":
        raise
    pytest.skip("PySide6 is required for presentation tests", allow_module_level=True)

from scripts.build_icon import ICON_SIZES, build_icon
from scripts.frontend_build import ICON_SOURCE, SIDEBAR_ICON_SOURCE
from voiceink_win.presentation.app_icon import (
    APPLICATION_ICON_FILENAMES,
    SIDEBAR_ICON_FILENAME,
    application_icon_paths,
    load_application_icon,
    sidebar_icon_paths,
)


@pytest.fixture(scope="module")
def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_runtime_icon_falls_back_to_repository_svg_when_ico_is_unavailable(
    application: QApplication,
    tmp_path: Path,
) -> None:
    del application

    icon = load_application_icon([tmp_path / "missing.ico", ICON_SOURCE])

    assert not icon.isNull()


def test_runtime_icon_returns_empty_icon_when_assets_are_unavailable(
    application: QApplication,
    tmp_path: Path,
) -> None:
    del application

    icon = load_application_icon([tmp_path / "missing.ico", tmp_path / "missing.svg"])

    assert icon.isNull()


def test_runtime_icon_candidates_include_frozen_and_source_locations(tmp_path: Path) -> None:
    paths = application_icon_paths(
        frozen_root=tmp_path / "frozen",
        executable=tmp_path / "package" / "voiceink-shell.exe",
        source_file=Path("/repo/src/voiceink_win/presentation/app_icon.py"),
    )

    assert paths[:4] == (
        tmp_path / "frozen" / "voiceink-transcribe.png",
        tmp_path / "frozen" / "voiceink-transcribe.ico",
        tmp_path / "package" / "voiceink-transcribe.png",
        tmp_path / "package" / "voiceink-transcribe.ico",
    )
    assert paths[-2:] == (
        Path("/repo/packaging/voiceink-shell-windows-x64/voiceink-transcribe.png"),
        Path("/repo/packaging/voiceink-shell-windows-x64/voiceink-transcribe.ico"),
    )
    assert APPLICATION_ICON_FILENAMES == ("voiceink-transcribe.png", "voiceink-transcribe.ico")


def test_sidebar_branding_asset_is_repository_owned_and_runtime_relocatable(
    tmp_path: Path,
) -> None:
    assert SIDEBAR_ICON_FILENAME == "voiceink-shell.svg"
    assert SIDEBAR_ICON_SOURCE.is_file()
    assert 'fill="#db594b"' in SIDEBAR_ICON_SOURCE.read_text(encoding="utf-8")

    paths = sidebar_icon_paths(
        frozen_root=tmp_path / "frozen",
        executable=tmp_path / "package" / "voiceink-shell.exe",
        source_file=Path("/repo/src/voiceink_win/presentation/app_icon.py"),
    )

    assert paths == (
        tmp_path / "frozen" / SIDEBAR_ICON_FILENAME,
        tmp_path / "package" / SIDEBAR_ICON_FILENAME,
        Path("/repo/packaging/voiceink-shell-windows-x64") / SIDEBAR_ICON_FILENAME,
    )


def test_application_png_provenance_and_generated_ico_sizes(tmp_path: Path) -> None:
    assert hashlib.sha256(ICON_SOURCE.read_bytes()).hexdigest() == (
        "de11e5550a84a03094f4cc60c6aff71045b67ccd020d142d6e379795c32ce0b"
    )
    source = QImage(str(ICON_SOURCE))
    assert source.size() == QSize(750, 750)

    output = tmp_path / "voiceink-transcribe.ico"
    build_icon(ICON_SOURCE, output)
    data = output.read_bytes()
    reserved, icon_type, count = struct.unpack_from("<HHH", data)
    assert (reserved, icon_type, count) == (0, 1, len(ICON_SIZES))

    entries = [struct.unpack_from("<BBBBHHII", data, 6 + index * 16) for index in range(count)]
    assert [width or 256 for width, *_ in entries] == list(ICON_SIZES)
    for entry in entries:
        width, _height, _colors, _reserved, _planes, _bits, length, offset = entry
        payload = data[offset : offset + length]
        assert payload.startswith(b"\x89PNG\r\n\x1a\n")
        image = QImage.fromData(payload, "PNG")
        assert not image.isNull()
        assert image.size() == QSize(width or 256, width or 256)
