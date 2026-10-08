from __future__ import annotations

from voiceink_win.presentation.icon_registry import LUCIDE_PATHS, SIDEBAR_ITEMS
from voiceink_win.presentation.theme import (
    DARK_THEME,
    LIGHT_THEME,
    ThemeMode,
    detect_system_theme,
    mode_from_system_name,
    stylesheet_for,
    theme_for,
)


def test_theme_selection_returns_complete_light_and_dark_palettes() -> None:
    assert theme_for(ThemeMode.LIGHT) is LIGHT_THEME
    assert theme_for(ThemeMode.DARK) is DARK_THEME
    assert LIGHT_THEME.window != DARK_THEME.window
    assert LIGHT_THEME.card != DARK_THEME.card
    assert LIGHT_THEME.accent == "#bf4d10"
    assert DARK_THEME.accent == "#d86820"


def test_stylesheet_contains_dark_surface_tokens_for_every_dashboard_layer() -> None:
    stylesheet = stylesheet_for(DARK_THEME)

    assert "QMainWindow" in stylesheet
    assert "QFrame#sidebar" in stylesheet
    assert "QScrollArea#dashboardScroll > QWidget#qt_scrollarea_viewport" in stylesheet
    assert "QWidget#dashboardContent" in stylesheet
    assert f"background: {DARK_THEME.card};" in stylesheet
    assert f"background: {DARK_THEME.empty_card};" in stylesheet
    assert f"background: {DARK_THEME.hero};" in stylesheet
    assert "#ffffff" not in stylesheet.split("QFrame#card", 1)[1].split("QFrame#heroCard", 1)[0]


def test_unknown_system_scheme_is_safe_and_defaults_to_light() -> None:
    assert mode_from_system_name("Dark") is ThemeMode.DARK
    assert mode_from_system_name("light") is ThemeMode.LIGHT
    assert mode_from_system_name("Unknown") is None
    assert mode_from_system_name(None) is None


def test_system_theme_prefers_qt_scheme_without_needing_a_gui_display() -> None:
    class FakeStyleHints:
        def colorScheme(self):
            return type("ColorScheme", (), {"name": "Dark"})()

    class FakeApplication:
        def styleHints(self):
            return FakeStyleHints()

    assert detect_system_theme(FakeApplication()) is ThemeMode.DARK


def test_sidebar_registry_matches_reference_order_and_has_unique_icons() -> None:
    assert [item.label for item in SIDEBAR_ITEMS] == [
        "Dashboard",
        "Modes",
        "Transcribe",
        "History",
        "Dictionary",
        "AI Models",
        "Audio",
        "Settings",
        "VoiceInk Pro",
    ]
    assert len({item.icon_name for item in SIDEBAR_ITEMS}) == len(SIDEBAR_ITEMS)
    assert all(item.icon_name in LUCIDE_PATHS for item in SIDEBAR_ITEMS)
    assert all("d=" in path or "x=" in path for path in LUCIDE_PATHS.values())
    assert SIDEBAR_ITEMS[0].enabled
    assert SIDEBAR_ITEMS[2].enabled
    assert not any(item.enabled for item in (*SIDEBAR_ITEMS[1:2], *SIDEBAR_ITEMS[3:]))
