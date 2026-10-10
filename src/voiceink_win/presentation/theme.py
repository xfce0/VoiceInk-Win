"""Theme tokens and stylesheet generation for the Qt shell."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ThemeMode(StrEnum):
    LIGHT = "light"
    DARK = "dark"


@dataclass(frozen=True)
class ThemeTokens:
    mode: ThemeMode
    window: str
    sidebar: str
    border: str
    text: str
    muted: str
    disabled: str
    nav_hover: str
    nav_selected: str
    nav_selected_border: str
    nav_focus_border: str
    card: str
    card_border: str
    empty_card: str
    empty_border: str
    hero: str
    hero_border: str
    hero_text: str
    hero_subtext: str
    accent: str
    accent_text: str
    accent_hover: str
    secondary_fill: str
    secondary_border: str
    secondary_text: str
    state_bg: str
    state_text: str
    state_recording_bg: str
    state_recording_text: str
    state_processing_bg: str
    state_processing_text: str
    error_bg: str
    error_text: str
    recorder: str
    recorder_border: str
    recorder_text: str
    recorder_muted: str
    recorder_control: str
    recorder_control_hover: str
    recorder_control_border: str
    recorder_recording: str
    recorder_recording_border: str
    waveform: str


LIGHT_THEME = ThemeTokens(
    mode=ThemeMode.LIGHT,
    window="#f4f4f6",
    sidebar="#e9e9ee",
    border="#d5d5dc",
    text="#202024",
    muted="#6d6d77",
    disabled="#62626e",
    nav_hover="#dedee5",
    nav_selected="#f0a568",
    nav_selected_border="#d9823b",
    nav_focus_border="#8c440e",
    card="#ffffff",
    card_border="#dfdfe5",
    empty_card="#eeeeF2",
    empty_border="#cfcfd8",
    hero="#f1bd84",
    hero_border="#e4aa70",
    hero_text="#211a15",
    hero_subtext="#6d5543",
    accent="#bf4d10",
    accent_text="#ffffff",
    accent_hover="#a9430c",
    secondary_fill="#fff7ef",
    secondary_border="#dc9c64",
    secondary_text="#594331",
    state_bg="#e8e8ed",
    state_text="#666672",
    state_recording_bg="#f9dfdf",
    state_recording_text="#b52e32",
    state_processing_bg="#eee6fc",
    state_processing_text="#7044aa",
    error_bg="#f9dfdf",
    error_text="#b52e32",
    recorder="#111113",
    recorder_border="#343439",
    recorder_text="#f3f3f4",
    recorder_muted="#94949e",
    recorder_control="#36363b",
    recorder_control_hover="#4b4b52",
    recorder_control_border="#4b4b52",
    recorder_recording="#c7373b",
    recorder_recording_border="#e16063",
    waveform="#f5f5f5",
)

DARK_THEME = ThemeTokens(
    mode=ThemeMode.DARK,
    window="#1f2023",
    sidebar="#25262a",
    border="#3a3b43",
    text="#f4f4f5",
    muted="#a2a3ad",
    disabled="#aeb0ba",
    nav_hover="#30323a",
    nav_selected="#4a3024",
    nav_selected_border="#a65a2b",
    nav_focus_border="#f4b27c",
    card="#2a2b30",
    card_border="#3a3b43",
    empty_card="#25262b",
    empty_border="#4b4d57",
    hero="#4a3024",
    hero_border="#765037",
    hero_text="#fff2e7",
    hero_subtext="#e3c1a9",
    accent="#d86820",
    accent_text="#29170b",
    accent_hover="#e4772d",
    secondary_fill="#302b27",
    secondary_border="#765037",
    secondary_text="#f0d8c6",
    state_bg="#34353d",
    state_text="#c1c2cb",
    state_recording_bg="#4c292d",
    state_recording_text="#ff9b9e",
    state_processing_bg="#3d3150",
    state_processing_text="#d4b7ff",
    error_bg="#4c292d",
    error_text="#ff9b9e",
    recorder="#111113",
    recorder_border="#343439",
    recorder_text="#f3f3f4",
    recorder_muted="#94949e",
    recorder_control="#36363b",
    recorder_control_hover="#4b4b52",
    recorder_control_border="#4b4b52",
    recorder_recording="#c7373b",
    recorder_recording_border="#e16063",
    waveform="#f5f5f5",
)


def theme_for(mode: ThemeMode) -> ThemeTokens:
    """Return immutable tokens for the requested system appearance."""

    return DARK_THEME if mode is ThemeMode.DARK else LIGHT_THEME


def mode_from_system_name(name: str | None) -> ThemeMode | None:
    """Convert Qt's color scheme name while keeping unknown versions safe."""

    normalized = (name or "").strip().lower()
    if normalized == ThemeMode.DARK:
        return ThemeMode.DARK
    if normalized == ThemeMode.LIGHT:
        return ThemeMode.LIGHT
    return None


def detect_system_theme(application) -> ThemeMode:
    """Read Qt's system scheme and fall back to the effective window palette."""

    try:
        color_scheme = application.styleHints().colorScheme()
        mode = mode_from_system_name(getattr(color_scheme, "name", None))
        if mode is not None:
            return mode
    except (AttributeError, RuntimeError):
        pass

    try:
        from PySide6.QtGui import QPalette

        window_color = application.palette().color(QPalette.ColorRole.Window)
        if window_color.isValid():
            return ThemeMode.DARK if window_color.lightnessF() < 0.5 else ThemeMode.LIGHT
    except (AttributeError, ImportError, RuntimeError):
        pass

    return ThemeMode.LIGHT


def stylesheet_for(theme: ThemeTokens) -> str:
    """Build the complete stylesheet from one semantic palette."""

    return f"""
QMainWindow, QWidget#root, QWidget#dashboardContent, QWidget#transcribePage {{
    background: {theme.window};
    color: {theme.text};
}}
QFrame#sidebar {{
    background: {theme.sidebar};
    border-right: 1px solid {theme.border};
}}
QScrollArea#dashboardScroll, QScrollArea#dashboardScroll > QWidget#qt_scrollarea_viewport {{
    background: {theme.window};
    border: none;
}}
QScrollArea#transcribeQueueScroll,
QScrollArea#transcribeQueueScroll > QWidget#qt_scrollarea_viewport {{
    background: {theme.window};
    border: none;
}}
QListWidget#historyList {{
    background: transparent;
    border: none;
    outline: none;
    padding: 2px;
}}
QListWidget#historyList::item {{
    background: transparent;
    border: none;
    padding: 0;
}}
QFrame#historyRow {{
    background: {theme.card};
    border: 1px solid {theme.card_border};
    border-radius: 12px;
}}
QFrame#historyRow[selected="true"] {{
    background: {theme.secondary_fill};
    border-color: {theme.accent};
}}
QLabel#historyTitle {{
    color: {theme.text};
    font-size: 14px;
    font-weight: 700;
}}
QLabel#historyPreview, QLabel#historyFullText {{
    color: {theme.text};
    font-size: 13px;
}}
QLabel#historyFullText {{
    background: {theme.empty_card};
    border-radius: 8px;
    padding: 8px;
}}
QPushButton#historyAction {{
    border-radius: 8px;
    font-size: 11px;
    padding: 5px 9px;
}}
QComboBox#historyVariant {{
    border: 1px solid {theme.card_border};
    border-radius: 8px;
    color: {theme.muted};
    padding: 4px 7px;
}}
QFrame#transcribeDropZone {{
    background: {theme.empty_card};
    border: 2px dashed {theme.empty_border};
    border-radius: 14px;
}}
QFrame#transcribeDropZone:hover {{
    border-color: {theme.accent};
}}
QFrame#transcribeItem {{
    background: {theme.card};
    border: 1px solid {theme.card_border};
    border-radius: 12px;
}}
QLabel#pageError {{
    background: {theme.error_bg};
    color: {theme.error_text};
    border-radius: 8px;
    padding: 8px 10px;
}}
QTextEdit {{
    background: {theme.empty_card};
    color: {theme.text};
    border: 1px solid {theme.card_border};
    border-radius: 8px;
    padding: 8px;
}}
QTabWidget::pane {{
    border: none;
}}
QTabBar::tab {{
    color: {theme.muted};
    padding: 6px 10px;
}}
QTabBar::tab:selected {{
    color: {theme.accent};
    font-weight: 700;
}}
QProgressBar {{
    background: {theme.empty_card};
    border: 1px solid {theme.card_border};
    border-radius: 4px;
    min-height: 6px;
    max-height: 6px;
}}
QProgressBar::chunk {{
    background: {theme.accent};
    border-radius: 4px;
}}
QScrollBar:vertical {{
    background: {theme.window};
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {theme.border};
    border-radius: 5px;
    min-height: 28px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QLabel#pageGreeting {{
    color: {theme.text};
}}
QLabel#heroSubtext, QLabel#muted, QLabel#metadata {{
    color: {theme.muted};
}}
QLabel#heroDetail {{
    color: {theme.hero_subtext};
}}
QPushButton {{
    background: {theme.secondary_fill};
    border: 1px solid {theme.secondary_border};
    border-radius: 10px;
    color: {theme.secondary_text};
    font-size: 12px;
    font-weight: 600;
    padding: 7px 13px;
    min-height: 16px;
}}
QPushButton:hover {{
    background: {theme.nav_hover};
    border-color: {theme.accent};
}}
QPushButton:pressed {{
    background: {theme.accent};
    border-color: {theme.accent};
    color: {theme.accent_text};
}}
QPushButton:disabled {{
    background: {theme.window};
    border-color: {theme.border};
    color: {theme.disabled};
}}
QPushButton:focus {{
    border-color: {theme.nav_focus_border};
}}
QPushButton#actionButton {{
    border-radius: 9px;
    padding: 6px 11px;
}}
QPushButton#navButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 10px;
    color: {theme.text};
    padding: 6px 10px 6px 8px;
    text-align: left;
}}
QPushButton#navButton:hover {{
    background: {theme.nav_hover};
    color: {theme.text};
}}
QPushButton#navButton:checked {{
    background: {theme.nav_selected};
    border-color: {theme.nav_selected_border};
    color: {theme.text};
}}
QPushButton#navButton:checked:hover {{
    background: {theme.nav_selected};
    border-color: {theme.nav_selected_border};
}}
QPushButton#navButton:disabled {{
    background: transparent;
    color: {theme.disabled};
    border-color: transparent;
}}
QPushButton#navButton:disabled:hover {{
    background: transparent;
    color: {theme.disabled};
    border-color: transparent;
}}
QPushButton#navButton:focus {{
    border-color: {theme.nav_focus_border};
}}
QFrame#card, QFrame#transcriptCard {{
    background: {theme.card};
    border: 1px solid {theme.card_border};
    border-radius: 16px;
}}
QFrame#heroCard {{
    background: {theme.hero};
    border: 1px solid {theme.hero_border};
    border-radius: 16px;
}}
QLabel#heroHeadline {{
    color: {theme.hero_text};
    font-family: "Arial Rounded MT Bold", "Segoe UI";
    font-size: 23px;
    font-weight: 700;
}}
QLabel#heroAccent {{
    color: {theme.accent};
    font-family: "Arial Rounded MT Bold", "Segoe UI";
    font-size: 30px;
    font-weight: 900;
}}
QPushButton#primaryButton {{
    background: {theme.accent};
    border: none;
    border-radius: 10px;
    color: {theme.accent_text};
    font-size: 13px;
    font-weight: 700;
    padding: 10px 18px;
}}
QPushButton#primaryButton:hover {{
    background: {theme.accent_hover};
}}
QPushButton#primaryButton:focus {{
    border: 1px solid {theme.nav_focus_border};
    padding: 10px 18px;
}}
QPushButton#primaryButton:disabled {{
    background: {theme.window};
    border: 1px solid {theme.border};
    color: {theme.disabled};
}}
QPushButton#secondaryButton {{
    background: {theme.secondary_fill};
    border: 1px solid {theme.secondary_border};
    border-radius: 10px;
    color: {theme.secondary_text};
    font-size: 13px;
    font-weight: 600;
    padding: 10px 18px;
}}
QPushButton#secondaryButton:disabled {{
    background: {theme.window};
    color: {theme.disabled};
    border-color: {theme.border};
}}
QPushButton#actionButton:disabled {{
    background: {theme.window};
    border-color: {theme.border};
    color: {theme.disabled};
}}
QLabel#sectionTitle {{
    color: {theme.text};
    font-size: 18px;
    font-weight: 700;
}}
QLabel#statePill {{
    background: {theme.state_bg};
    border-radius: 10px;
    color: {theme.state_text};
    padding: 5px 10px;
    font-size: 11px;
    font-weight: 700;
}}
QLabel#statePill[role="recording"] {{
    background: {theme.state_recording_bg};
    color: {theme.state_recording_text};
}}
QLabel#statePill[role="error"] {{
    background: {theme.error_bg};
    color: {theme.error_text};
}}
QLabel#statePill[role="processing"] {{
    background: {theme.state_processing_bg};
    color: {theme.state_processing_text};
}}
QFrame#emptyCard {{
    background: {theme.empty_card};
    border: 1px dashed {theme.empty_border};
    border-radius: 12px;
}}
QLabel#transcriptText {{
    color: {theme.text};
    font-size: 13px;
}}
QFrame#recorder {{
    background: {theme.recorder};
    border: 1px solid {theme.recorder_border};
    border-radius: 14px;
}}
QLabel#recorderStatus, QLabel#recorderHint {{
    color: {theme.recorder_text};
}}
QLabel#recorderHint {{
    color: {theme.recorder_muted};
    font-size: 11px;
}}
QPushButton#recordButton, QPushButton#closeButton {{
    background: {theme.recorder_control};
    border: 1px solid {theme.recorder_control_border};
    border-radius: 17px;
    color: {theme.recorder_text};
    font-size: 12px;
    font-weight: 700;
    padding: 8px 13px;
}}
QPushButton#recordButton:hover, QPushButton#closeButton:hover {{
    background: {theme.recorder_control_hover};
}}
QPushButton#recordButton[recording="true"] {{
    background: {theme.recorder_recording};
    border-color: {theme.recorder_recording_border};
}}
QPushButton#recordButton:disabled {{
    color: {theme.disabled};
}}
"""
