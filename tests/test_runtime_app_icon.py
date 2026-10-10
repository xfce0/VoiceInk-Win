from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    if os.environ.get("VOICEINK_GUI_TESTS") == "1":
        raise
    pytest.skip("PySide6 is required for presentation tests", allow_module_level=True)

from scripts.frontend_build import ICON_SOURCE
from voiceink_win.presentation.app_icon import (
    application_icon_paths,
    load_application_icon,
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
        tmp_path / "frozen" / "voiceink-shell.ico",
        tmp_path / "frozen" / "voiceink-shell.svg",
        tmp_path / "package" / "voiceink-shell.ico",
        tmp_path / "package" / "voiceink-shell.svg",
    )
    assert paths[-2:] == (
        Path("/repo/packaging/voiceink-shell-windows-x64/voiceink-shell.ico"),
        Path("/repo/packaging/voiceink-shell-windows-x64/voiceink-shell.svg"),
    )
