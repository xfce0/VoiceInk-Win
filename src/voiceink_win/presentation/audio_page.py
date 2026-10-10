"""Qt page for native-audio preferences and unavailable device state."""

from __future__ import annotations

import math

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QListWidget,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from voiceink_win.application import PersistenceService
from voiceink_win.domain import Settings

from .async_tools import FutureBridge
from .localization import LocaleConfig, TranslationKey, translate

ROUTE_IDS = ("system_default", "selected_device", "priority_order")
SOUND_IDS = ("none", "built_in", "custom")
AUDIO_DEFAULTS = {
    "input_route": "system_default",
    "mute_while_recording": False,
    "pause_media_while_recording": False,
    "resume_delay_seconds": 0.0,
    "start_sound": "none",
    "stop_sound": "none",
}


class AudioPage(QWidget):
    """Display safe audio preferences without touching native audio APIs."""

    def __init__(
        self,
        persistence: PersistenceService | None,
        parent: QWidget | None = None,
        locale_config: LocaleConfig | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("audioPage")
        self._persistence = persistence
        self._locale_config = locale_config or LocaleConfig(parent=self)
        self._locale_callback = self.apply_locale
        self._locale_config.locale_changed.connect(
            self._locale_callback, Qt.ConnectionType.AutoConnection
        )
        self._bridge = FutureBridge(self)
        self._settings = Settings()
        self._loading = False
        self._generation = 0
        self._disposed = False
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(scroll)
        content.setObjectName("audioContent")
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

        device_card = QFrame(content)
        device_card.setObjectName("card")
        device_layout = QVBoxLayout(device_card)
        device_layout.setContentsMargins(18, 16, 18, 16)
        device_layout.setSpacing(10)
        self._device_section = QLabel(device_card)
        self._device_section.setObjectName("sectionTitle")
        device_layout.addWidget(self._device_section)
        route_form = QFormLayout()
        self._route_label = QLabel(device_card)
        self._route_label.setObjectName("metadata")
        self._input_route_combo = QComboBox(device_card)
        self._input_route_combo.setAccessibleName("Input route")
        for route_id in ROUTE_IDS:
            self._input_route_combo.addItem(route_id, route_id)
        route_form.addRow(self._route_label, self._input_route_combo)
        self._selected_device_label = QLabel(device_card)
        self._selected_device_label.setObjectName("metadata")
        self._selected_device_value = QLabel(device_card)
        self._selected_device_value.setObjectName("muted")
        route_form.addRow(self._selected_device_label, self._selected_device_value)
        device_layout.addLayout(route_form)
        self._device_list_label = QLabel(device_card)
        self._device_list_label.setObjectName("metadata")
        device_layout.addWidget(self._device_list_label)
        self._device_list = QListWidget(device_card)
        self._device_list.setAccessibleName("Available microphone devices")
        self._device_list.addItem("")
        self._device_list.setMaximumHeight(76)
        device_layout.addWidget(self._device_list)
        self._device_status = QLabel(device_card)
        self._device_status.setObjectName("pageError")
        self._device_status.setWordWrap(True)
        device_layout.addWidget(self._device_status)
        root.addWidget(device_card)

        behavior_card = QFrame(content)
        behavior_card.setObjectName("card")
        behavior_layout = QVBoxLayout(behavior_card)
        behavior_layout.setContentsMargins(18, 16, 18, 16)
        behavior_layout.setSpacing(10)
        self._behavior_title = QLabel(behavior_card)
        self._behavior_title.setObjectName("sectionTitle")
        behavior_layout.addWidget(self._behavior_title)
        behavior_form = QFormLayout()
        self._mute_while_recording = QCheckBox(behavior_card)
        self._mute_while_recording.setAccessibleName("Mute other audio while recording")
        behavior_form.addRow(self._mute_while_recording)
        self._pause_media_while_recording = QCheckBox(behavior_card)
        self._pause_media_while_recording.setAccessibleName("Pause media while recording")
        behavior_form.addRow(self._pause_media_while_recording)
        self._resume_delay_label = QLabel(behavior_card)
        self._resume_delay_label.setObjectName("metadata")
        self._resume_delay = QLineEdit(behavior_card)
        self._resume_delay.setAccessibleName("Resume delay in seconds")
        behavior_form.addRow(self._resume_delay_label, self._resume_delay)
        self._start_sound_label = QLabel(behavior_card)
        self._start_sound_label.setObjectName("metadata")
        self._start_sound_combo = self._sound_combo(behavior_card)
        behavior_form.addRow(self._start_sound_label, self._start_sound_combo)
        self._stop_sound_label = QLabel(behavior_card)
        self._stop_sound_label.setObjectName("metadata")
        self._stop_sound_combo = self._sound_combo(behavior_card)
        behavior_form.addRow(self._stop_sound_label, self._stop_sound_combo)
        self._format_label = QLabel(behavior_card)
        self._format_label.setObjectName("metadata")
        self._format_value = QLabel(behavior_card)
        self._format_value.setObjectName("muted")
        behavior_form.addRow(self._format_label, self._format_value)
        behavior_layout.addLayout(behavior_form)
        self._preferences_note = QLabel(behavior_card)
        self._preferences_note.setObjectName("muted")
        self._preferences_note.setWordWrap(True)
        behavior_layout.addWidget(self._preferences_note)
        root.addWidget(behavior_card)

        self._availability = QLabel(content)
        self._availability.setObjectName("pageError")
        self._availability.setWordWrap(True)
        root.addWidget(self._availability)
        self._status = QLabel(content)
        self._status.setObjectName("metadata")
        root.addWidget(self._status)
        self._error = QLabel(content)
        self._error.setObjectName("pageError")
        self._error.setWordWrap(True)
        root.addWidget(self._error)
        root.addStretch(1)
        scroll.setWidget(content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)
        self._native_controls = (
            self._input_route_combo,
            self._device_list,
            self._mute_while_recording,
            self._pause_media_while_recording,
            self._resume_delay,
            self._start_sound_combo,
            self._stop_sound_combo,
        )
        self._set_native_controls_enabled(False)
        self.apply_locale()

    def _sound_combo(self, parent: QWidget) -> QComboBox:
        combo = QComboBox(parent)
        combo.setAccessibleName("Recording sound")
        for sound_id in SOUND_IDS:
            combo.addItem(sound_id, sound_id)
        return combo

    def refresh(self) -> None:
        if self._disposed:
            return
        self._generation += 1
        generation = self._generation
        if self._persistence is None:
            self._apply_settings(Settings(), persistence_unavailable=True)
            return
        self._loading = True
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
            self._apply_settings(Settings(), persistence_unavailable=True)
            self._status.setText(self._t(TranslationKey.COMMON_ERROR))
            self._error.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
            return
        self._apply_settings(settings or Settings(), persistence_unavailable=False)

    def _apply_settings(self, settings: Settings, *, persistence_unavailable: bool) -> None:
        self._settings = settings
        preferences = {**AUDIO_DEFAULTS, **settings.audio_preferences}
        route = preferences["input_route"]
        if route not in ROUTE_IDS:
            route = AUDIO_DEFAULTS["input_route"]
        start_sound = preferences["start_sound"]
        if start_sound not in SOUND_IDS:
            start_sound = AUDIO_DEFAULTS["start_sound"]
        stop_sound = preferences["stop_sound"]
        if stop_sound not in SOUND_IDS:
            stop_sound = AUDIO_DEFAULTS["stop_sound"]
        delay = preferences["resume_delay_seconds"]
        if (
            isinstance(delay, bool)
            or not isinstance(delay, int | float)
            or not math.isfinite(delay)
        ):
            delay = AUDIO_DEFAULTS["resume_delay_seconds"]
        delay = max(0.0, delay)
        with (
            QSignalBlocker(self._input_route_combo),
            QSignalBlocker(self._mute_while_recording),
            QSignalBlocker(self._pause_media_while_recording),
            QSignalBlocker(self._resume_delay),
            QSignalBlocker(self._start_sound_combo),
            QSignalBlocker(self._stop_sound_combo),
        ):
            self._input_route_combo.setCurrentIndex(self._input_route_combo.findData(route))
            self._mute_while_recording.setChecked(preferences["mute_while_recording"] is True)
            self._pause_media_while_recording.setChecked(
                preferences["pause_media_while_recording"] is True
            )
            self._resume_delay.setText(f"{delay:g}")
            self._start_sound_combo.setCurrentIndex(self._start_sound_combo.findData(start_sound))
            self._stop_sound_combo.setCurrentIndex(self._stop_sound_combo.findData(stop_sound))
        self._status.setText(
            self._t(
                TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE
                if persistence_unavailable
                else TranslationKey.COMMON_READY
            )
        )
        self._error.setText(
            self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE)
            if persistence_unavailable
            else ""
        )

    def _set_native_controls_enabled(self, enabled: bool) -> None:
        for control in self._native_controls:
            control.setEnabled(enabled)

    def apply_locale(self, _locale: str | None = None) -> None:
        del _locale
        self._title.setText(self._t(TranslationKey.AUDIO_TITLE))
        self._subtitle.setText(self._t(TranslationKey.AUDIO_SUBTITLE))
        self._device_section.setText(self._t(TranslationKey.AUDIO_DEVICE_SECTION))
        self._route_label.setText(self._t(TranslationKey.AUDIO_DEVICE_ROUTE))
        self._selected_device_label.setText(self._t(TranslationKey.AUDIO_SELECTED_DEVICE))
        self._selected_device_value.setText(self._t(TranslationKey.AUDIO_NO_DEVICES))
        self._device_list_label.setText(self._t(TranslationKey.AUDIO_DEVICE_LIST))
        self._device_list.item(0).setText(self._t(TranslationKey.AUDIO_NO_DEVICES))
        self._device_status.setText(self._t(TranslationKey.AUDIO_DEVICE_UNAVAILABLE))
        self._behavior_title.setText(self._t(TranslationKey.AUDIO_RECORDING_BEHAVIOR))
        self._resume_delay_label.setText(self._t(TranslationKey.AUDIO_RESUME_DELAY))
        self._start_sound_label.setText(self._t(TranslationKey.AUDIO_START_SOUND))
        self._stop_sound_label.setText(self._t(TranslationKey.AUDIO_STOP_SOUND))
        self._format_label.setText(self._t(TranslationKey.AUDIO_FORMAT))
        self._format_value.setText(self._t(TranslationKey.AUDIO_FORMAT_VALUE))
        self._preferences_note.setText(self._t(TranslationKey.AUDIO_PREFERENCES_READ_ONLY))
        self._availability.setText(self._t(TranslationKey.AUDIO_BACKEND_UNAVAILABLE))
        self._mute_while_recording.setText(self._t(TranslationKey.AUDIO_MUTE_WHILE_RECORDING))
        self._pause_media_while_recording.setText(
            self._t(TranslationKey.AUDIO_PAUSE_MEDIA_WHILE_RECORDING)
        )
        for index, route_id in enumerate(ROUTE_IDS):
            key = {
                "system_default": TranslationKey.AUDIO_ROUTE_SYSTEM_DEFAULT,
                "selected_device": TranslationKey.AUDIO_ROUTE_SELECTED_DEVICE,
                "priority_order": TranslationKey.AUDIO_ROUTE_PRIORITY_ORDER,
            }[route_id]
            self._input_route_combo.setItemText(index, self._t(key))
        for combo in (self._start_sound_combo, self._stop_sound_combo):
            for index, sound_id in enumerate(SOUND_IDS):
                key = {
                    "none": TranslationKey.AUDIO_SOUND_NONE,
                    "built_in": TranslationKey.AUDIO_SOUND_BUILT_IN,
                    "custom": TranslationKey.AUDIO_SOUND_CUSTOM,
                }[sound_id]
                combo.setItemText(index, self._t(key))

    def _t(self, key: TranslationKey, **values: object) -> str:
        return translate(key, self._locale_config.locale, **values)

    def dispose(self) -> None:
        self._disposed = True
        self._generation += 1
        try:
            self._locale_config.locale_changed.disconnect(self._locale_callback)
        except (RuntimeError, TypeError):
            pass
