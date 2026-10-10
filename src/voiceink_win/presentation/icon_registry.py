"""Pure data registry for the expanded navigation sidebar."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SidebarItem:
    label: str
    icon_name: str
    tile_color: str
    icon_foreground: str
    enabled: bool


LUCIDE_PATHS = {
    "dashboard": (
        '<path d="M4 14a8 8 0 1 1 16 0"/><path d="m12 12 3-3"/><path d="M4.5 17h.01M19.5 17h.01"/>'
    ),
    "modes": (
        '<path d="m12 3-1.4 4.1L6.5 8.5l4.1 1.4L12 14l1.4-4.1 4.1-1.4-4.1-1.4z"/>'
        '<path d="m19 15-.7 2.3L16 18l2.3.7L19 21l.7-2.3L22 18l-2.3-.7z"/>'
    ),
    "transcribe": '<path d="M3 12h2l1.5-5L9 19l2-14 2.5 11 1.5-4H21"/>',
    "history": ('<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/><path d="M12 7v5l3 2"/>'),
    "dictionary": (
        '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v17H6.5A2.5 2.5 0 0 0 4 22z"/>'
        '<path d="M4 5.5v16M8 7h8M8 11h7"/>'
    ),
    "models": (
        '<rect x="6" y="6" width="12" height="12" rx="2"/>'
        '<path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>'
    ),
    "audio": (
        '<rect x="8" y="3" width="8" height="13" rx="4"/><path d="M5 11a7 7 0 0 0 14 0"/>'
        '<path d="M12 18v3M9 21h6"/>'
    ),
    "settings": (
        '<path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z"/>'
        '<path d="m19.4 15 .1.1a2 2 0 1 1-2.8 2.8l-.1-.1'
        "a2 2 0 0 0-3.4 1.4V19a2 2 0 1 1-4 0v-.2"
        "a2 2 0 0 0-3.4-1.4l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1"
        "A2 2 0 0 0 3.6 11H3a2 2 0 1 1 0-4h.6a2 2 0 0 0 1.4-3.4"
        "l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A2 2 0 0 0 11 2.2V2"
        "a2 2 0 1 1 4 0v.2a2 2 0 0 0 3.4 1.4l.1-.1a2 2 0 1 1 2.8 2.8"
        'l-.1.1A2 2 0 0 0 20.6 7h.4a2 2 0 1 1 0 4h-.4a2 2 0 0 0-1.2 4Z"/>'
    ),
    "license": (
        '<path d="M12 3 5 6v5c0 4.7 3 8.5 7 10 4-1.5 7-5.3 7-10V6z"/><path d="m9 12 2 2 4-4"/>'
    ),
}


SIDEBAR_ITEMS = (
    SidebarItem("Dashboard", "dashboard", "#e8892e", "#24170f", True),
    SidebarItem("Modes", "modes", "#6256c9", "#ffffff", True),
    SidebarItem("Transcribe", "transcribe", "#db594b", "#ffffff", True),
    SidebarItem("History", "history", "#df4f82", "#2a101d", True),
    SidebarItem("Dictionary", "dictionary", "#3478d4", "#ffffff", True),
    SidebarItem("AI Models", "models", "#986d4b", "#24150d", True),
    SidebarItem("Audio", "audio", "#0f766e", "#ffffff", True),
    SidebarItem("Settings", "settings", "#64748b", "#ffffff", True),
    SidebarItem("VoiceInk Pro", "license", "#4eaf6c", "#12351f", False),
)
