"""Platform-independent global shortcut values and registration failures."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

DEFAULT_GLOBAL_TOGGLE_SHORTCUT = "Ctrl+Space"


class ShortcutValidationReason(StrEnum):
    EMPTY = "empty"
    MODIFIER_REQUIRED = "modifier_required"
    DUPLICATE_MODIFIER = "duplicate_modifier"
    UNSUPPORTED_MODIFIER = "unsupported_modifier"
    UNSUPPORTED_KEY = "unsupported_key"


class ShortcutValidationError(ValueError):
    """The user-facing shortcut notation is not supported."""

    def __init__(self, message: str, reason: ShortcutValidationReason) -> None:
        super().__init__(message)
        self.reason = reason


_MODIFIER_NAMES = {
    "CTRL": "Ctrl",
    "CONTROL": "Ctrl",
    "ALT": "Alt",
    "SHIFT": "Shift",
    "WIN": "Win",
    "WINDOWS": "Win",
    "META": "Win",
}
_MODIFIER_ORDER = ("Ctrl", "Alt", "Shift", "Win")
_KEY_ALIASES = {
    "ESC": "Escape",
    "ESCAPE": "Escape",
    "SPACE": "Space",
    "ENTER": "Enter",
    "RETURN": "Enter",
    "TAB": "Tab",
}


def _canonical_key(value: str) -> str:
    upper = value.upper()
    if upper in _KEY_ALIASES:
        return _KEY_ALIASES[upper]
    if len(value) == 1 and value.isascii() and value.isalnum():
        return value.upper()
    if upper.startswith("F") and upper[1:].isdigit():
        number = int(upper[1:])
        if 1 <= number <= 24:
            return f"F{number}"
    raise ShortcutValidationError(
        "The global shortcut key is unsupported.", ShortcutValidationReason.UNSUPPORTED_KEY
    )


def canonicalize_global_shortcut(value: str) -> str:
    """Validate and canonicalize the supported single-chord grammar."""
    if not isinstance(value, str) or not value.strip():
        raise ShortcutValidationError(
            "A global shortcut must not be empty.", ShortcutValidationReason.EMPTY
        )
    parts = [part.strip() for part in value.split("+")]
    if any(not part for part in parts) or len(parts) < 2:
        raise ShortcutValidationError(
            "A global shortcut requires a modifier and a key.",
            ShortcutValidationReason.MODIFIER_REQUIRED,
        )
    modifiers: list[str] = []
    for part in parts[:-1]:
        modifier = _MODIFIER_NAMES.get(part.upper())
        if modifier is None:
            raise ShortcutValidationError(
                "The global shortcut modifier is unsupported.",
                ShortcutValidationReason.UNSUPPORTED_MODIFIER,
            )
        if modifier in modifiers:
            raise ShortcutValidationError(
                "Global shortcut modifiers must be unique.",
                ShortcutValidationReason.DUPLICATE_MODIFIER,
            )
        modifiers.append(modifier)
    try:
        key = _canonical_key(parts[-1])
    except ShortcutValidationError:
        if parts[-1].upper() in _MODIFIER_NAMES:
            raise ShortcutValidationError(
                "A global shortcut requires a non-modifier key.",
                ShortcutValidationReason.MODIFIER_REQUIRED,
            ) from None
        raise
    ordered = [modifier for modifier in _MODIFIER_ORDER if modifier in modifiers]
    return "+".join((*ordered, key))


@dataclass(frozen=True, slots=True)
class GlobalShortcut:
    """A user-facing shortcut notation understood by an outer adapter."""

    value: str = DEFAULT_GLOBAL_TOGGLE_SHORTCUT

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", canonicalize_global_shortcut(self.value))


class ShortcutRegistrationError(RuntimeError):
    """Base error for a shortcut adapter that cannot register a shortcut."""


class ShortcutConflictError(ShortcutRegistrationError):
    """The requested shortcut is already owned by another application."""


class ShortcutUnavailableError(ShortcutRegistrationError):
    """The current platform or desktop session cannot register shortcuts."""
