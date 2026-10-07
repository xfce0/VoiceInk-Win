"""Validate the frozen frontend executable without opening a GUI."""

from __future__ import annotations

import argparse
from pathlib import Path

EXPECTED_MACHINE = 0x8664
EXPECTED_PE32_PLUS = 0x20B
WINDOWS_GUI_SUBSYSTEM = 2
WINDOWS_CONSOLE_SUBSYSTEM = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--subsystem", choices=("gui", "console"), default="gui")
    return parser


def _read_exact(stream, offset: int, size: int) -> bytes:
    stream.seek(offset)
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("executable has a truncated PE header")
    return data


def _read_uint16(data: bytes, offset: int) -> int:
    end = offset + 2
    if offset < 0 or end > len(data):
        raise ValueError("executable has a truncated PE header")
    return int.from_bytes(data[offset:end], "little")


def validate_executable(path: Path, *, expected_subsystem: int = WINDOWS_GUI_SUBSYSTEM) -> None:
    with path.open("rb") as stream:
        dos_header = _read_exact(stream, 0, 0x40)
        if dos_header[:2] != b"MZ":
            raise ValueError("executable is not a Windows PE image")

        pe_offset = int.from_bytes(dos_header[0x3C:0x40], "little")
        coff_header = _read_exact(stream, pe_offset, 24)
        if coff_header[:4] != b"PE\0\0":
            raise ValueError("executable has an invalid PE signature")

        machine = _read_uint16(coff_header, 4)
        if machine != EXPECTED_MACHINE:
            raise ValueError(
                f"expected x64 PE machine 0x{EXPECTED_MACHINE:04x}, got 0x{machine:04x}"
            )

        optional_size = _read_uint16(coff_header, 20)
        if optional_size < 70:
            raise ValueError("executable has a truncated optional PE header")
        optional_header = _read_exact(stream, pe_offset + 24, optional_size)
        if _read_uint16(optional_header, 0) != EXPECTED_PE32_PLUS:
            raise ValueError("executable is not a 64-bit PE32+ image")
        subsystem = _read_uint16(optional_header, 68)
        if subsystem != expected_subsystem:
            raise ValueError(f"expected Windows subsystem {expected_subsystem}, got {subsystem}")


def main() -> int:
    arguments = _parser().parse_args()
    path = arguments.executable
    expected_name = (
        "voiceink-shell.exe" if arguments.subsystem == "gui" else "voiceink-shell-smoke.exe"
    )
    if path.name.lower() != expected_name:
        raise SystemExit(f"frontend package smoke: expected {expected_name}")
    if not path.is_file():
        raise SystemExit(f"frontend package smoke: missing executable: {path}")
    try:
        expected_subsystem = (
            WINDOWS_GUI_SUBSYSTEM if arguments.subsystem == "gui" else WINDOWS_CONSOLE_SUBSYSTEM
        )
        validate_executable(path, expected_subsystem=expected_subsystem)
    except (OSError, ValueError) as error:
        raise SystemExit(f"frontend package smoke: {error}") from error
    print(f"frontend package smoke: passed ({path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
