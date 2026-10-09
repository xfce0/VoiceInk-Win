"""Qt page for persisted user settings."""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt
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

from voiceink_win.application import PersistenceService
from voiceink_win.domain import Settings

from .async_tools import FutureBridge
from .localization import SUPPORTED_LOCALES, Locale, LocaleConfig, TranslationKey, translate
from .modes_page import MODE_IDS, MODE_NAMES


class SettingsPage(QWidget):
    """Edit settings through the asynchronous application persistence port."""

    def __init__(
        self,
        persistence: PersistenceService | None,
        parent: QWidget | None = None,
        locale_config: LocaleConfig | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self._persistence = persistence
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

        form = QFormLayout()
        self._language_label = self._form_label(form, TranslationKey.SETTINGS_LANGUAGE)
        self._language_combo = QComboBox(content)
        self._language_combo.setAccessibleName("Interface language")
        for locale in SUPPORTED_LOCALES:
            self._language_combo.addItem(locale.value, locale.value)
        self._language_combo.currentIndexChanged.connect(self._language_changed)
        form.setWidget(0, QFormLayout.ItemRole.LabelRole, self._language_label)
        form.setWidget(0, QFormLayout.ItemRole.FieldRole, self._language_combo)

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
        self._start_hotkey = QLineEdit(content)
        self._start_hotkey.setAccessibleName("Start and stop hotkey")
        self._start_hotkey.editingFinished.connect(
            lambda: self._save_field("hotkeys.start_stop", self._start_hotkey.text().strip())
        )
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
        self._error = QLabel(content)
        self._error.setObjectName("pageError")
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
            self._status.setText(self._t(TranslationKey.COMMON_ERROR))
            self._error.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
            return
        self._apply_settings(settings or Settings(), unavailable=False)

    def _apply_settings(self, settings: Settings, *, unavailable: bool) -> None:
        self._settings = settings
        with (
            QSignalBlocker(self._language_combo),
            QSignalBlocker(self._auto_copy),
            QSignalBlocker(self._mode_combo),
            QSignalBlocker(self._start_hotkey),
            QSignalBlocker(self._cancel_hotkey),
        ):
            self._language_combo.setCurrentIndex(
                max(0, self._language_combo.findData(settings.language))
            )
            self._auto_copy.setChecked(settings.auto_copy)
            self._mode_combo.setCurrentIndex(
                max(0, self._mode_combo.findData(settings.selected_mode))
            )
            self._start_hotkey.setText(str(settings.hotkeys.get("start_stop", "")))
            self._cancel_hotkey.setText(str(settings.hotkeys.get("cancel", "")))
        if not unavailable:
            self._locale_config.set_locale(settings.language)
        self._set_controls_enabled(not unavailable)
        self._model_value.setText(self._preference_text(settings.model_preferences))
        self._audio_value.setText(self._preference_text(settings.audio_preferences))
        self._status.setText(
            self._t(
                TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE
                if unavailable
                else TranslationKey.COMMON_READY
            )
        )
        self._error.clear()
        if unavailable:
            self._error.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))

    def _set_controls_enabled(self, enabled: bool) -> None:
        for control in (
            self._language_combo,
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

    def _save_current(self) -> None:
        if self._loading or self._disposed or self._persistence is None:
            return
        self._save_field("language", self._language_combo.currentData())

    def _save_field(self, field: str, value: object) -> None:
        if self._loading or self._disposed or self._persistence is None:
            return
        self._save_sequence += 1
        sequence = self._save_sequence
        generation = self._generation
        self._status.setText(self._t(TranslationKey.COMMON_SAVING))
        self._bridge.watch(
            self._persistence.update_settings({field: value}),
            lambda result, error: self._saved(generation, sequence, result, error),
        )

    def _saved(
        self,
        generation: int,
        sequence: int,
        result: Settings | None,
        error: BaseException | None,
    ) -> None:
        if self._disposed or generation != self._generation or sequence != self._save_sequence:
            return
        if error is not None:
            self._status.setText(self._t(TranslationKey.COMMON_ERROR))
            self._error.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
        else:
            if result is not None:
                self._settings = result
            self._status.setText(self._t(TranslationKey.COMMON_SAVED))

    def _preference_text(self, preferences: dict[str, object]) -> str:
        if not preferences:
            return self._t(TranslationKey.SETTINGS_BACKEND_UNAVAILABLE)
        return ", ".join(f"{key}: {value}" for key, value in sorted(preferences.items()))

    def apply_locale(self, _locale: str | None = None) -> None:
        del _locale
        self._title.setText(self._t(TranslationKey.SETTINGS_TITLE))
        self._subtitle.setText(self._t(TranslationKey.SETTINGS_SUBTITLE))
        self._language_label.setText(self._t(TranslationKey.SETTINGS_LANGUAGE))
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
