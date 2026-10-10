"""Resolve the repository-owned icon for the running Qt application."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from PySide6.QtGui import QIcon

ICON_FILENAMES = ("voiceink-shell.ico", "voiceink-shell.svg")


def application_icon_paths(
    *,
    frozen_root: Path | None = None,
    executable: Path | None = None,
    source_file: Path | None = None,
) -> tuple[Path, ...]:
    """Return bundled, executable-adjacent, and source-checkout icon candidates."""
    if frozen_root is None:
        frozen_value = getattr(sys, "_MEIPASS", None)
        frozen_root = Path(frozen_value) if isinstance(frozen_value, str) else None
    if executable is None:
        executable = Path(sys.executable)
    if source_file is None:
        source_file = Path(__file__)

    roots = [source_file.resolve().parents[3] / "packaging" / "voiceink-shell-windows-x64"]
    if frozen_root is not None:
        roots.insert(0, frozen_root)
    roots.insert(1 if frozen_root is not None else 0, executable.resolve().parent)

    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for filename in ICON_FILENAMES:
            candidate = root / filename
            if candidate not in seen:
                candidates.append(candidate)
                seen.add(candidate)
    return tuple(candidates)


def load_application_icon(
    paths: Iterable[Path],
    *,
    icon_factory: Callable[[str], QIcon] = QIcon,
) -> QIcon:
    """Load the first usable icon, returning an empty icon when none is available."""
    for path in paths:
        try:
            if not path.is_file():
                continue
            icon = icon_factory(str(path))
        except OSError:
            continue
        if not icon.isNull():
            return icon
    return QIcon()


def application_icon() -> QIcon:
    """Load the runtime icon without making startup depend on optional assets."""
    return load_application_icon(application_icon_paths())
