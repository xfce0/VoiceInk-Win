"""Qt page for persisted transcription mode selection."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QFormLayout, QLabel, QVBoxLayout, QWidget

from voiceink_win.application import PersistenceService
from voiceink_win.domain import Settings

from .async_tools import FutureBridge
from .localization import LocaleConfig, TranslationKey, translate

MODE_IDS = ("default", "meeting", "focus")
MODE_NAMES = {
    "default": TranslationKey.MODE_DEFAULT,
    "meeting": TranslationKey.MODE_MEETING,
    "focus": TranslationKey.MODE_FOCUS,
}
MODE_DETAILS = {
    "default": TranslationKey.MODE_DEFAULT_DETAIL,
    "meeting": TranslationKey.MODE_MEETING_DETAIL,
    "focus": TranslationKey.MODE_FOCUS_DETAIL,
}


class ModesPage(QWidget):
    """Persist the selected mode without claiming unavailable runtime features."""

    def __init__(
        self,
        persistence: PersistenceService | None,
        parent: QWidget | None = None,
        locale_config: LocaleConfig | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("modesPage")
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
        self._save_sequence = 0
        self._disposed = False
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 24)
        layout.setSpacing(14)
        self._title = QLabel(self)
        self._title.setObjectName("pageGreeting")
        layout.addWidget(self._title)
        self._subtitle = QLabel(self)
        self._subtitle.setObjectName("heroSubtext")
        self._subtitle.setWordWrap(True)
        layout.addWidget(self._subtitle)

        form = QFormLayout()
        self._mode_combo = QComboBox(self)
        self._mode_combo.setAccessibleName("Selected transcription mode")
        for mode_id in MODE_IDS:
            self._mode_combo.addItem(mode_id, mode_id)
        self._mode_combo.currentIndexChanged.connect(self._mode_changed)
        form.addRow(self._label(TranslationKey.MODE_SELECTED), self._mode_combo)
        layout.addLayout(form)

        self._detail = QLabel(self)
        self._detail.setObjectName("heroSubtext")
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)
        self._availability = QLabel(self)
        self._availability.setObjectName("pageError")
        self._availability.setWordWrap(True)
        layout.addWidget(self._availability)
        self._status = QLabel(self)
        self._status.setObjectName("metadata")
        layout.addWidget(self._status)
        layout.addStretch(1)
        self.apply_locale()

    def _label(self, key: TranslationKey) -> QLabel:
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
        self._mode_combo.setEnabled(False)
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
            self._mode_combo.setEnabled(False)
            self._status.setText(self._t(TranslationKey.COMMON_ERROR))
            self._availability.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
            return
        self._apply_settings(settings or Settings(), unavailable=False)

    def _apply_settings(self, settings: Settings, *, unavailable: bool) -> None:
        self._settings = settings
        index = max(0, self._mode_combo.findData(settings.selected_mode))
        self._mode_combo.blockSignals(True)
        self._mode_combo.setCurrentIndex(index)
        self._mode_combo.blockSignals(False)
        self._mode_combo.setEnabled(not unavailable)
        self._availability.setText(self._t(TranslationKey.MODE_UNAVAILABLE))
        self._availability.setVisible(unavailable or self._persistence is None)
        self._status.setText(
            self._t(
                TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE
                if unavailable
                else TranslationKey.COMMON_READY
            )
        )
        self._render_detail()

    def _mode_changed(self, index: int) -> None:
        if self._loading or self._disposed or self._persistence is None or index < 0:
            return
        selected_mode = self._mode_combo.itemData(index)
        self._save_sequence += 1
        sequence = self._save_sequence
        generation = self._generation
        self._status.setText(self._t(TranslationKey.COMMON_SAVING))
        self._bridge.watch(
            self._persistence.update_settings({"selected_mode": selected_mode}),
            lambda result, error: self._saved(generation, sequence, result, error),
        )
        self._render_detail()

    def _saved(
        self,
        generation: int,
        sequence: int,
        result: Settings | None,
        error: BaseException | None,
    ) -> None:
        if self._disposed or generation != self._generation or sequence != self._save_sequence:
            return
        if result is not None:
            self._settings = result
        self._status.setText(
            self._t(TranslationKey.COMMON_ERROR if error else TranslationKey.COMMON_SAVED)
        )
        if error:
            self._availability.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))

    def _render_detail(self) -> None:
        mode_id = self._mode_combo.currentData()
        self._detail.setText(self._t(MODE_DETAILS[mode_id]))

    def apply_locale(self, _locale: str | None = None) -> None:
        del _locale
        self._title.setText(self._t(TranslationKey.MODES_TITLE))
        self._subtitle.setText(self._t(TranslationKey.MODES_SUBTITLE))
        self._availability.setText(self._t(TranslationKey.MODE_UNAVAILABLE))
        self._status.setText(self._t(TranslationKey.COMMON_READY))
        self._mode_combo.setItemText(0, self._t(TranslationKey.MODE_DEFAULT))
        self._mode_combo.setItemText(1, self._t(TranslationKey.MODE_MEETING))
        self._mode_combo.setItemText(2, self._t(TranslationKey.MODE_FOCUS))
        self._render_detail()

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
