from __future__ import annotations

from voiceink_win.presentation.icon_registry import LUCIDE_PATHS, SIDEBAR_ITEMS
from voiceink_win.presentation.theme import (
    DARK_THEME,
    DASHBOARD_GOOD_MORNING_TYPOGRAPHY,
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


def test_typography_tokens_raise_shared_weights_without_changing_sizes() -> None:
    typography = DASHBOARD_GOOD_MORNING_TYPOGRAPHY

    assert typography.body_size == 13
    assert typography.sidebar_size == 13
    assert typography.body_weight > 500
    assert typography.sidebar_weight > 600
    assert typography.control_weight > 600
    assert typography.strong_weight > typography.control_weight
    assert typography.display_weight > typography.strong_weight


def test_stylesheet_contains_dark_surface_tokens_for_every_dashboard_layer() -> None:
    stylesheet = stylesheet_for(DARK_THEME)

    assert "QMainWindow" in stylesheet
    assert "QFrame#sidebar" in stylesheet
    assert "QScrollArea#dashboardScroll > QWidget#qt_scrollarea_viewport" in stylesheet
    assert "QWidget#dashboardContent" in stylesheet
    assert "QPushButton#actionButton" in stylesheet
    assert "border-radius: 9px;" in stylesheet
    assert "QPushButton:pressed" in stylesheet
    assert "QPushButton#primaryButton:focus" in stylesheet
    assert "QPushButton#primaryButton:disabled" in stylesheet
    assert "QPushButton#actionButton:disabled" in stylesheet
    assert f'font-family: "{DASHBOARD_GOOD_MORNING_TYPOGRAPHY.body_family}";' in stylesheet
    assert f"font-weight: {DASHBOARD_GOOD_MORNING_TYPOGRAPHY.sidebar_weight};" in stylesheet
    assert "QComboBox:focus, QLineEdit:focus, QCheckBox:focus" in stylesheet
    assert f"background: {DARK_THEME.card};" in stylesheet
    assert f"background: {DARK_THEME.empty_card};" in stylesheet
    assert f"background: {DARK_THEME.hero};" in stylesheet
    assert "#ffffff" not in stylesheet.split("QFrame#card", 1)[1].split("QFrame#heroCard", 1)[0]


def test_stylesheet_distinguishes_neutral_unavailability_from_inline_errors() -> None:
    stylesheet = stylesheet_for(LIGHT_THEME)

    assert "QLabel#pageUnavailable" in stylesheet
    assert f"background: {LIGHT_THEME.state_bg};" in stylesheet
    assert "QLabel#inlineError" in stylesheet
    inline_error = stylesheet.split("QLabel#inlineError", 1)[1].split("}", 1)[0]
    assert "background:" not in inline_error


def test_light_theme_uses_light_recorder_surfaces() -> None:
    stylesheet = stylesheet_for(LIGHT_THEME)

    assert LIGHT_THEME.recorder != DARK_THEME.recorder
    assert LIGHT_THEME.recorder_control != DARK_THEME.recorder_control
    assert f"background: {LIGHT_THEME.recorder};" in stylesheet
    assert f"background: {LIGHT_THEME.recorder_control};" in stylesheet
    assert f"color: {LIGHT_THEME.recorder_text};" in stylesheet
    assert f"background: {DARK_THEME.recorder};" not in stylesheet
    assert f"background: {DARK_THEME.recorder_control};" not in stylesheet


def test_unknown_system_scheme_is_safe_and_defaults_to_light() -> None:
    assert mode_from_system_name("Dark") is ThemeMode.DARK
    assert mode_from_system_name("light") is ThemeMode.LIGHT
    assert mode_from_system_name("Unknown") is None
    assert mode_from_system_name(None) is None


def test_theme_preference_mode_defaults_to_system_but_resolves_explicit_modes() -> None:
    assert theme_for(ThemeMode.SYSTEM) is LIGHT_THEME
    assert theme_for(ThemeMode.LIGHT) is LIGHT_THEME
    assert theme_for(ThemeMode.DARK) is DARK_THEME


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
    assert len({item.tile_color for item in SIDEBAR_ITEMS}) == len(SIDEBAR_ITEMS)
    assert all(item.icon_name in LUCIDE_PATHS for item in SIDEBAR_ITEMS)
    assert all(item.icon_foreground for item in SIDEBAR_ITEMS)
    assert all("d=" in path or "x=" in path for path in LUCIDE_PATHS.values())
    assert SIDEBAR_ITEMS[0].enabled
    assert SIDEBAR_ITEMS[2].enabled
    assert all(item.enabled for item in (*SIDEBAR_ITEMS[1:2], *SIDEBAR_ITEMS[3:5]))
    assert SIDEBAR_ITEMS[5].enabled
    assert SIDEBAR_ITEMS[6].enabled
    assert not SIDEBAR_ITEMS[8].enabled
