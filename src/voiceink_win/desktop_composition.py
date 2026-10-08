"""No-resource production composition for the desktop shell."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from voiceink_win.application import ShellController


class DesktopComposition(Protocol):
    @property
    def controller(self) -> ShellController: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class _NoResourceDesktopComposition:
    controller: ShellController

    def close(self) -> None:
        """Close the no-resource composition; there is nothing to release."""
        return None


def build_desktop_composition() -> DesktopComposition:
    """Build the production shell without selecting a backend or resource."""
    return _NoResourceDesktopComposition(ShellController.unavailable())


__all__ = ["DesktopComposition", "build_desktop_composition"]
