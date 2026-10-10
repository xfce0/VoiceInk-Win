"""Platform-independent global shortcut values and registration failures."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_GLOBAL_TOGGLE_SHORTCUT = "Ctrl+Alt+Space"


@dataclass(frozen=True, slots=True)
class GlobalShortcut:
    """A user-facing shortcut notation understood by an outer adapter."""

    value: str = DEFAULT_GLOBAL_TOGGLE_SHORTCUT

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("global shortcut must not be empty")
        object.__setattr__(self, "value", self.value.strip())


class ShortcutRegistrationError(RuntimeError):
    """Base error for a shortcut adapter that cannot register a shortcut."""


class ShortcutConflictError(ShortcutRegistrationError):
    """The requested shortcut is already owned by another application."""


class ShortcutUnavailableError(ShortcutRegistrationError):
    """The current platform or desktop session cannot register shortcuts."""
