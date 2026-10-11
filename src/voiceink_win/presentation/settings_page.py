"""Qt page for persisted user settings."""

from __future__ import annotations

import os
from collections.abc import Callable

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from voiceink_win.application import PersistenceService, ShortcutAvailability
from voiceink_win.domain import (
    GlobalShortcut,
    Settings,
    ShortcutValidationError,
    ShortcutValidationReason,
    ThemePreference,
)

from .async_tools import FutureBridge
from .localization import SUPPORTED_LOCALES, Locale, LocaleConfig, TranslationKey, translate
from .modes_page import MODE_IDS, MODE_NAMES

THEME_PREFERENCES = (
    ThemePreference.SYSTEM,
    ThemePreference.LIGHT,
    ThemePreference.DARK,
)
THEME_NAMES = {
    ThemePreference.SYSTEM: TranslationKey.SETTINGS_THEME_SYSTEM,
    ThemePreference.LIGHT: TranslationKey.SETTINGS_THEME_LIGHT,
    ThemePreference.DARK: TranslationKey.SETTINGS_THEME_DARK,
}


class HotkeyCapture(QLineEdit):
    """Capture one validated modifier-plus-key chord without registering it."""

    shortcut_captured = Signal(str)
    capture_rejected = Signal(object)
    capture_cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._committed = ""
        self._capture_submitted = False

    def set_committed(self, value: str) -> None:
        self._committed = value
        self.setText(value)
        self.setProperty("pending", False)

    @property
    def committed_value(self) -> str:
        return self._committed

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.set_committed(self._committed)
            self.capture_cancelled.emit()
            event.accept()
            return
        parts: list[str] = []
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            parts.append("Ctrl")
        if modifiers & Qt.KeyboardModifier.AltModifier:
            parts.append("Alt")
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            parts.append("Shift")
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            parts.append("Win")
        key = self._key_name(event.key())
        if key is None:
            if event.key() in {
                Qt.Key.Key_Control,
                Qt.Key.Key_Alt,
                Qt.Key.Key_Shift,
                Qt.Key.Key_Meta,
            }:
                error = ShortcutValidationError(
                    "A global shortcut requires a modifier and a key.",
                    ShortcutValidationReason.MODIFIER_REQUIRED,
                )
            else:
                error = ShortcutValidationError(
                    "The selected global shortcut key is unsupported.",
                    ShortcutValidationReason.UNSUPPORTED_KEY,
                )
            self.capture_rejected.emit(error)
            event.accept()
            return
        try:
            shortcut = GlobalShortcut("+".join((*parts, key)))
        except ShortcutValidationError as error:
            self.capture_rejected.emit(error)
            event.accept()
            return
        self.setText(shortcut.value)
        self.setProperty("pending", True)
        self._capture_submitted = True
        self.shortcut_captured.emit(shortcut.value)
        event.accept()

    def consume_capture_submission(self) -> bool:
        submitted = self._capture_submitted
        self._capture_submitted = False
        return submitted

    @staticmethod
    def _key_name(key: int) -> str | None:
        if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            return chr(key)
        if Qt.Key.Key_0 <= key <= Qt.Key.Key_9:
            return chr(key)
        if Qt.Key.Key_F1 <= key <= Qt.Key.Key_F24:
            return f"F{key - Qt.Key.Key_F1 + 1}"
        return {
            Qt.Key.Key_Space: "Space",
            Qt.Key.Key_Return: "Enter",
            Qt.Key.Key_Enter: "Enter",
            Qt.Key.Key_Tab: "Tab",
        }.get(key)


class SettingsPage(QWidget):
    """Edit settings through the asynchronous application persistence port."""

    theme_changed = Signal(str)

    def __init__(
        self,
        persistence: PersistenceService | None,
        parent: QWidget | None = None,
        locale_config: LocaleConfig | None = None,
        on_start_stop_hotkey_changed: Callable[[str], object] | None = None,
        on_start_stop_hotkey_loaded: Callable[[Settings], object] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self._persistence = persistence
        self._on_start_stop_hotkey_changed = on_start_stop_hotkey_changed
        self._on_start_stop_hotkey_loaded = on_start_stop_hotkey_loaded
        self._locale_config = locale_config or LocaleConfig(parent=self)
        self._locale_callback = self.apply_locale
        self._locale_config.locale_changed.connect(
            self._locale_callback, Qt.ConnectionType.AutoConnection
        )
        self._bridge = FutureBridge(self)
        self._settings = Settings()
        self._loading = False
        self._save_sequence = 0
        self._generation = 0
        self._disposed = False
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(scroll)
        content.setObjectName("settingsContent")
        root = QVBoxLayout(content)
        root.setContentsMargins(30, 28, 30, 24)
        root.setSpacing(14)
        self._title = QLabel(content)
        self._title.setObjectName("pageGreeting")
        root.addWidget(self._title)
        self._subtitle = QLabel(content)
        self._subtitle.setObjectName("heroSubtext")
        self._subtitle.setWordWrap(True)
        root.addWidget(self._subtitle)
        self._availability = QLabel(content)
        self._availability.setObjectName("pageUnavailable")
        self._availability.setWordWrap(True)
        self._availability.setVisible(False)
        root.addWidget(self._availability)

        form = QFormLayout()
        self._language_label = self._form_label(form, TranslationKey.SETTINGS_LANGUAGE)
        self._language_combo = QComboBox(content)
        self._language_combo.setAccessibleName("Interface language")
        for locale in SUPPORTED_LOCALES:
            self._language_combo.addItem(locale.value, locale.value)
        self._language_combo.currentIndexChanged.connect(self._language_changed)
        form.setWidget(0, QFormLayout.ItemRole.LabelRole, self._language_label)
        form.setWidget(0, QFormLayout.ItemRole.FieldRole, self._language_combo)

        self._theme_label = self._form_label(form, TranslationKey.SETTINGS_THEME)
        self._theme_combo = QComboBox(content)
        self._theme_combo.setAccessibleName("Dashboard theme")
        for preference in THEME_PREFERENCES:
            self._theme_combo.addItem(preference.value, preference.value)
        self._theme_combo.currentIndexChanged.connect(self._theme_changed)
        form.setWidget(1, QFormLayout.ItemRole.LabelRole, self._theme_label)
        form.setWidget(1, QFormLayout.ItemRole.FieldRole, self._theme_combo)

        self._auto_copy_label = QLabel(content)
        self._auto_copy_label.setObjectName("metadata")
        self._auto_copy = QCheckBox(content)
        self._auto_copy.setAccessibleName("Copy completed transcripts automatically")
        self._auto_copy.toggled.connect(lambda checked: self._save_field("auto_copy", checked))
        form.addRow(self._auto_copy_label, self._auto_copy)

        self._mode_label = QLabel(content)
        self._mode_label.setObjectName("metadata")
        self._mode_combo = QComboBox(content)
        self._mode_combo.setAccessibleName("Selected transcription mode")
        for mode_id in MODE_IDS:
            self._mode_combo.addItem(mode_id, mode_id)
        self._mode_combo.currentIndexChanged.connect(
            lambda index: self._save_field("selected_mode", self._mode_combo.itemData(index))
        )
        form.addRow(self._mode_label, self._mode_combo)

        self._start_hotkey_label = QLabel(content)
        self._start_hotkey_label.setObjectName("metadata")
        self._start_hotkey = HotkeyCapture(content)
        self._start_hotkey.setAccessibleName("Start and stop hotkey")
        self._start_hotkey.shortcut_captured.connect(self._hotkey_captured)
        self._start_hotkey.capture_rejected.connect(self._hotkey_rejected)
        self._start_hotkey.capture_cancelled.connect(self._hotkey_cancelled)
        self._start_hotkey.editingFinished.connect(self._start_hotkey_submitted)
        form.addRow(self._start_hotkey_label, self._start_hotkey)

        self._cancel_hotkey_label = QLabel(content)
        self._cancel_hotkey_label.setObjectName("metadata")
        self._cancel_hotkey = QLineEdit(content)
        self._cancel_hotkey.setAccessibleName("Cancel hotkey")
        self._cancel_hotkey.editingFinished.connect(
            lambda: self._save_field("hotkeys.cancel", self._cancel_hotkey.text().strip())
        )
        form.addRow(self._cancel_hotkey_label, self._cancel_hotkey)
        root.addLayout(form)

        self._model_label = QLabel(content)
        self._model_label.setObjectName("metadata")
        self._model_value = QLabel(content)
        self._model_value.setObjectName("muted")
        self._model_value.setWordWrap(True)
        root.addWidget(self._model_label)
        root.addWidget(self._model_value)
        self._audio_label = QLabel(content)
        self._audio_label.setObjectName("metadata")
        self._audio_value = QLabel(content)
        self._audio_value.setObjectName("muted")
        self._audio_value.setWordWrap(True)
        root.addWidget(self._audio_label)
        root.addStretch(1)
        self._status = QLabel(content)
        self._status.setObjectName("metadata")
        root.addWidget(self._status)
        self._hotkey_status = QLabel(content)
        self._hotkey_status.setObjectName("metadata")
        self._hotkey_status.setWordWrap(True)
        root.addWidget(self._hotkey_status)
        self._error = QLabel(content)
        self._error.setObjectName("inlineError")
        self._error.setWordWrap(True)
        root.addWidget(self._error)
        scroll.setWidget(content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)
        self.apply_locale()

    def _form_label(self, _form: QFormLayout, key: TranslationKey) -> QLabel:
        label = QLabel(self._t(key), self)
        label.setObjectName("metadata")
        return label

    def refresh(self) -> None:
        if self._disposed:
            return
        self._generation += 1
        self._save_sequence += 1
        generation = self._generation
        self._availability.clear()
        self._availability.setVisible(False)
        self._error.clear()
        if self._persistence is None:
            self._apply_settings(Settings(), unavailable=True)
            return
        self._loading = True
        self._set_controls_enabled(False)
        self._status.setText(self._t(TranslationKey.COMMON_LOADING))
        self._bridge.watch(
            self._persistence.get_settings(),
            lambda settings, error: self._loaded(generation, settings, error),
        )

    def _loaded(
        self, generation: int, settings: Settings | None, error: BaseException | None
    ) -> None:
        if self._disposed or generation != self._generation:
            return
        self._loading = False
        if error is not None:
            self._set_controls_enabled(False)
            self._show_unavailable()
            return
        self._apply_settings(settings or Settings(), unavailable=False)

    def _apply_settings(self, settings: Settings, *, unavailable: bool) -> None:
        self._settings = settings
        with (
            QSignalBlocker(self._language_combo),
            QSignalBlocker(self._theme_combo),
            QSignalBlocker(self._auto_copy),
            QSignalBlocker(self._mode_combo),
            QSignalBlocker(self._start_hotkey),
            QSignalBlocker(self._cancel_hotkey),
        ):
            self._language_combo.setCurrentIndex(
                max(0, self._language_combo.findData(settings.language))
            )
            self._theme_combo.setCurrentIndex(
                max(0, self._theme_combo.findData(settings.theme_mode.value))
            )
            self._auto_copy.setChecked(settings.auto_copy)
            self._mode_combo.setCurrentIndex(
                max(0, self._mode_combo.findData(settings.selected_mode))
            )
            self._start_hotkey.set_committed(str(settings.hotkeys.get("start_stop", "")))
            self._cancel_hotkey.setText(str(settings.hotkeys.get("cancel", "")))
        if not unavailable:
            self._locale_config.set_locale(settings.language)
            if self._on_start_stop_hotkey_loaded is not None:
                status = self._on_start_stop_hotkey_loaded(settings)
                self._render_hotkey_status(status)
        self.theme_changed.emit(settings.theme_mode.value)
        self._set_controls_enabled(not unavailable)
        self._start_hotkey.setEnabled(
            not unavailable
            and self._on_start_stop_hotkey_loaded is None
            or not unavailable
            and os.name == "nt"
        )
        self._start_hotkey.setReadOnly(
            self._on_start_stop_hotkey_loaded is not None and os.name != "nt"
        )
        self._model_value.setText(self._preference_text(settings.model_preferences))
        self._audio_value.setText(self._preference_text(settings.audio_preferences))
        self._status.setText(self._t(TranslationKey.COMMON_READY) if not unavailable else "")
        self._availability.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
        self._availability.setVisible(unavailable)
        self._error.clear()

    def _show_unavailable(self) -> None:
        self._status.clear()
        self._availability.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
        self._availability.setVisible(True)
        self._error.clear()

    def _set_controls_enabled(self, enabled: bool) -> None:
        for control in (
            self._language_combo,
            self._theme_combo,
            self._auto_copy,
            self._mode_combo,
            self._start_hotkey,
            self._cancel_hotkey,
        ):
            control.setEnabled(enabled)

    def _language_changed(self, index: int) -> None:
        if self._loading or self._disposed or index < 0:
            return
        self._locale_config.set_locale(self._language_combo.itemData(index))
        self._save_field("language", self._language_combo.itemData(index))

    def _theme_changed(self, index: int) -> None:
        if self._loading or self._disposed or index < 0:
            return
        theme_mode = self._theme_combo.itemData(index)
        self.theme_changed.emit(theme_mode)
        self._save_field("theme_mode", theme_mode)

    def _save_current(self) -> None:
        if self._loading or self._disposed or self._persistence is None:
            return
        self._save_field("language", self._language_combo.currentData())

    def _start_hotkey_submitted(self) -> None:
        if self._start_hotkey.consume_capture_submission():
            return
        if self._on_start_stop_hotkey_changed is None:
            self._save_field("hotkeys.start_stop", self._start_hotkey.text().strip())
            return
        self._submit_hotkey(self._start_hotkey.text().strip())

    def _hotkey_captured(self, shortcut: str) -> None:
        if self._on_start_stop_hotkey_changed is None:
            self._save_field("hotkeys.start_stop", shortcut)
            return
        self._submit_hotkey(shortcut)

    def _submit_hotkey(self, shortcut: str) -> None:
        if self._loading or self._disposed:
            return
        try:
            outcome = self._on_start_stop_hotkey_changed(shortcut)
        except BaseException as error:
            self._hotkey_rejected(error)
            return
        if hasattr(outcome, "add_done_callback"):
            self._bridge.watch(outcome, self._hotkey_updated)
        else:
            self._render_hotkey_status(outcome)

    def _hotkey_updated(self, result, error: BaseException | None) -> None:
        if self._disposed:
            return
        if error is not None:
            self._hotkey_rejected(error)
            return
        self._render_hotkey_status(result)

    def _hotkey_rejected(self, error: BaseException) -> None:
        self._start_hotkey.set_committed(self._start_hotkey.committed_value)
        if isinstance(error, ShortcutValidationError):
            self._hotkey_status.setText(self._t(TranslationKey.SETTINGS_HOTKEY_INVALID))
        else:
            self._hotkey_status.setText(self._t(TranslationKey.SETTINGS_HOTKEY_SAVE_ERROR))

    def _hotkey_cancelled(self) -> None:
        self._hotkey_status.clear()

    def _render_hotkey_status(self, status) -> None:
        if status is None:
            return
        self._start_hotkey.set_committed(status.shortcut.value)
        if status.availability is ShortcutAvailability.REGISTERED:
            message = self._t(
                TranslationKey.SETTINGS_HOTKEY_REGISTERED,
                shortcut=status.shortcut.value,
            )
            if status.message == "invalid_stored_value":
                message = f"{message} {self._t(TranslationKey.SETTINGS_HOTKEY_INVALID_STORED)}"
            elif status.message == "persistence_error":
                message = self._t(TranslationKey.SETTINGS_HOTKEY_SAVE_ERROR)
            self._hotkey_status.setText(message)
        elif status.availability is ShortcutAvailability.CONFLICT:
            self._hotkey_status.setText(self._t(TranslationKey.SETTINGS_HOTKEY_CONFLICT))
        elif status.availability is ShortcutAvailability.UNAVAILABLE:
            key = (
                TranslationKey.SETTINGS_HOTKEY_INVALID_STORED
                if status.message == "invalid_stored_value"
                else TranslationKey.SETTINGS_HOTKEY_WINDOWS_ONLY
                if os.name != "nt" and self._on_start_stop_hotkey_loaded is not None
                else TranslationKey.SETTINGS_HOTKEY_SAVE_ERROR
                if status.message == "restoration_error"
                else TranslationKey.SETTINGS_HOTKEY_UNAVAILABLE
            )
            self._hotkey_status.setText(self._t(key))
        else:
            self._hotkey_status.clear()

    def _save_field(self, field: str, value: object) -> None:
        if self._loading or self._disposed or self._persistence is None:
            return
        self._save_sequence += 1
        sequence = self._save_sequence
        generation = self._generation
        self._status.setText(self._t(TranslationKey.COMMON_SAVING))
        self._bridge.watch(
            self._persistence.update_settings({field: value}),
            lambda result, error: self._saved(generation, sequence, field, result, error),
        )

    def _saved(
        self,
        generation: int,
        sequence: int,
        field: str,
        result: Settings | None,
        error: BaseException | None,
    ) -> None:
        if self._disposed or generation != self._generation or sequence != self._save_sequence:
            return
        if error is not None:
            self._status.setText(self._t(TranslationKey.COMMON_ERROR))
            self._error.setText(self._t(TranslationKey.SETTINGS_SAVE_ERROR))
        else:
            if result is not None:
                self._settings = result
                if field == "hotkeys.start_stop" and self._on_start_stop_hotkey_changed is not None:
                    shortcut = str(result.hotkeys.get("start_stop", "")).strip()
                    if shortcut:
                        self._on_start_stop_hotkey_changed(shortcut)
            self._status.setText(self._t(TranslationKey.COMMON_SAVED))

    def _preference_text(self, preferences: dict[str, object]) -> str:
        if not preferences:
            return self._t(TranslationKey.SETTINGS_BACKEND_UNAVAILABLE)
        return ", ".join(f"{key}: {value}" for key, value in sorted(preferences.items()))

    def apply_locale(self, _locale: str | None = None) -> None:
        del _locale
        self._title.setText(self._t(TranslationKey.SETTINGS_TITLE))
        self._subtitle.setText(self._t(TranslationKey.SETTINGS_SUBTITLE))
        self._availability.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
        self._language_label.setText(self._t(TranslationKey.SETTINGS_LANGUAGE))
        self._theme_label.setText(self._t(TranslationKey.SETTINGS_THEME))
        self._auto_copy_label.setText(self._t(TranslationKey.SETTINGS_AUTO_COPY))
        self._mode_label.setText(self._t(TranslationKey.SETTINGS_MODE))
        self._start_hotkey_label.setText(self._t(TranslationKey.SETTINGS_START_STOP_HOTKEY))
        self._cancel_hotkey_label.setText(self._t(TranslationKey.SETTINGS_CANCEL_HOTKEY))
        self._model_label.setText(self._t(TranslationKey.SETTINGS_MODEL))
        self._audio_label.setText(self._t(TranslationKey.SETTINGS_AUDIO))
        self._start_hotkey.setPlaceholderText(self._t(TranslationKey.SETTINGS_HOTKEY_PLACEHOLDER))
        self._cancel_hotkey.setPlaceholderText(self._t(TranslationKey.SETTINGS_HOTKEY_PLACEHOLDER))
        self._model_value.setText(self._preference_text(self._settings.model_preferences))
        self._audio_value.setText(self._preference_text(self._settings.audio_preferences))
        if not self._hotkey_status.text():
            key = (
                TranslationKey.SETTINGS_HOTKEY_WINDOWS_ONLY
                if os.name != "nt" and self._on_start_stop_hotkey_loaded is not None
                else TranslationKey.SETTINGS_HOTKEY_HINT
            )
            self._hotkey_status.setText(self._t(key))
        for index, locale in enumerate(SUPPORTED_LOCALES):
            self._language_combo.setItemText(
                index,
                self._t(
                    TranslationKey.SETTINGS_ENGLISH
                    if locale is Locale.ENGLISH
                    else TranslationKey.SETTINGS_RUSSIAN
                ),
            )
        for index, mode_id in enumerate(MODE_IDS):
            self._mode_combo.setItemText(index, self._t(MODE_NAMES[mode_id]))
        for index, preference in enumerate(THEME_PREFERENCES):
            self._theme_combo.setItemText(index, self._t(THEME_NAMES[preference]))

    def _t(self, key: TranslationKey, **values: object) -> str:
        return translate(key, self._locale_config.locale, **values)

    def dispose(self) -> None:
        self._disposed = True
        self._generation += 1
        self._save_sequence += 1
        try:
            self._locale_config.locale_changed.disconnect(self._locale_callback)
        except (RuntimeError, TypeError):
            pass
