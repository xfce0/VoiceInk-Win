from pathlib import Path

import pytest

from scripts.frontend_package_smoke import validate_executable

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "build-frontend.yml"


def test_frontend_workflow_builds_a_windowed_pyside6_executable() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "runs-on: windows-2022" in workflow
    assert 'pip install --editable ".[gui,build]"' in workflow
    assert "--onefile" in workflow
    assert "--windowed" in workflow
    assert "--collect-all PySide6" in workflow
    assert "--collect-all shiboken6" in workflow
    assert "scripts/frontend_entrypoint.py" in workflow
    assert "--smoke" in workflow
    assert 'QT_QPA_PLATFORM = "offscreen"' in workflow
    assert "voiceink-shell-windows-x64" in workflow


def test_frontend_entrypoint_delegates_to_presentation_app() -> None:
    entrypoint = (ROOT / "scripts" / "frontend_entrypoint.py").read_text(encoding="utf-8")

    assert "from voiceink_win.presentation.app import main" in entrypoint
    assert "raise SystemExit(main(smoke=smoke))" in entrypoint


def test_frontend_package_smoke_validates_x64_gui_pe(tmp_path: Path) -> None:
    executable = tmp_path / "voiceink-shell.exe"
    data = bytearray(0x40 + 24 + 240)
    data[:2] = b"MZ"
    data[0x3C:0x40] = (0x40).to_bytes(4, "little")
    data[0x40:0x44] = b"PE\0\0"
    data[0x44:0x46] = (0x8664).to_bytes(2, "little")
    data[0x54:0x56] = (240).to_bytes(2, "little")
    data[0x58:0x5A] = (0x20B).to_bytes(2, "little")
    data[0x9C:0x9E] = (2).to_bytes(2, "little")
    executable.write_bytes(data)

    validate_executable(executable)

    data[0x44:0x46] = (0x14C).to_bytes(2, "little")
    executable.write_bytes(data)
    with pytest.raises(ValueError, match="expected x64 PE machine"):
        validate_executable(executable)
