"""Compact transcript row used by the History presentation page."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from voiceink_win.domain import HistoryRecord, TranscriptVariant

from .localization import LocaleConfig, TranslationKey, translate


class HistoryRow(QFrame):
    """A selectable macOS-style list row with a collapsed transcript preview."""

    clicked = Signal()

    def __init__(
        self,
        record: HistoryRecord,
        metadata: str,
        locale_config: LocaleConfig,
        *,
        audio_available: bool,
        folder_available: bool,
        audio_error: bool = False,
        folder_error: bool = False,
        parent: QFrame | None = None,
    ) -> None:
        super().__init__(parent)
        self.record = record
        self._locale_config = locale_config
        self._audio_available = audio_available
        self._folder_available = folder_available
        self._audio_error = audio_error
        self._folder_error = folder_error
        self._expanded = False
        self.setObjectName("historyRow")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._build_ui(metadata)
        self.apply_locale()

    @property
    def expanded(self) -> bool:
        return self._expanded

    @property
    def copy_button(self) -> QPushButton:
        return self._copy

    @property
    def audio_button(self) -> QPushButton:
        return self._audio

    @property
    def folder_button(self) -> QPushButton:
        return self._folder

    @property
    def variant_combo(self) -> QComboBox:
        return self._variant

    @property
    def export_txt_button(self) -> QPushButton:
        return self._export_txt

    @property
    def export_markdown_button(self) -> QPushButton:
        return self._export_markdown

    @property
    def delete_button(self) -> QPushButton:
        return self._delete

    def _build_ui(self, metadata: str) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self._title = QLabel(self._source_name(), self)
        self._title.setObjectName("historyTitle")
        self._title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        header.addWidget(self._title, 1)
        self._metadata = QLabel(metadata, self)
        self._metadata.setObjectName("metadata")
        self._metadata.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        header.addWidget(self._metadata)
        layout.addLayout(header)

        self._preview = QLabel(_preview_text(self._text()), self)
        self._preview.setObjectName("historyPreview")
        self._preview.setWordWrap(True)
        self._preview.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._preview)

        self._full_text = QLabel(self._text(), self)
        self._full_text.setObjectName("historyFullText")
        self._full_text.setWordWrap(True)
        self._full_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._full_text.setVisible(False)
        self._full_text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._full_text)

        actions = QVBoxLayout()
        actions.setSpacing(6)
        selector_row = QHBoxLayout()
        self._variant = QComboBox(self)
        self._variant.setObjectName("historyVariant")
        self._variant.addItem("Original", TranscriptVariant.ORIGINAL.value)
        self._variant.addItem("Enhanced", TranscriptVariant.ENHANCED.value)
        self._variant.setCurrentIndex(
            1 if self.record.selected_variant is TranscriptVariant.ENHANCED else 0
        )
        selector_row.addWidget(self._variant)
        selector_row.addStretch(1)
        actions.addLayout(selector_row)

        self._copy = self._action_button()
        self._audio = self._action_button()
        self._folder = self._action_button()
        self._export_txt = self._action_button()
        self._export_markdown = self._action_button()
        self._delete = self._action_button()

        action_row = QHBoxLayout()
        action_row.setSpacing(6)
        action_row.addStretch(1)
        for button in (self._copy, self._audio, self._folder):
            action_row.addWidget(button)
        actions.addLayout(action_row)

        export_row = QHBoxLayout()
        export_row.setSpacing(6)
        export_row.addStretch(1)
        for button in (self._export_txt, self._export_markdown, self._delete):
            export_row.addWidget(button)
        actions.addLayout(export_row)
        layout.addLayout(actions)

    def _action_button(self) -> QPushButton:
        button = QPushButton(self)
        button.setObjectName("historyAction")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def _source_name(self) -> str:
        value = self.record.source_metadata.get("source_name")
        return value.strip() if isinstance(value, str) and value.strip() else self.record.source

    def _text(self) -> str:
        if self.record.selected_variant is TranscriptVariant.ENHANCED and self.record.enhanced_text:
            return self.record.enhanced_text
        return self.record.original_text

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self._full_text.setVisible(expanded)
        self._preview.setVisible(not expanded)

    def set_record(self, record: HistoryRecord) -> None:
        self.record = record
        text = self._text()
        self._preview.setText(_preview_text(text))
        self._full_text.setText(text)

    def set_metadata(self, metadata: str) -> None:
        self._metadata.setText(metadata)

    def set_media_available(
        self,
        *,
        audio: bool | None = None,
        folder: bool | None = None,
        audio_error: bool | None = None,
        folder_error: bool | None = None,
    ) -> None:
        if audio is not None:
            self._audio_available = audio
        if folder is not None:
            self._folder_available = folder
        if audio_error is not None:
            self._audio_error = audio_error
        if folder_error is not None:
            self._folder_error = folder_error
        self.apply_locale()

    def apply_locale(self, _locale: str | None = None) -> None:
        del _locale
        locale = self._locale_config.locale
        self._variant.setItemText(0, translate(TranslationKey.HISTORY_ORIGINAL, locale))
        self._variant.setItemText(1, translate(TranslationKey.HISTORY_ENHANCED, locale))
        self._variant.setAccessibleName(translate(TranslationKey.HISTORY_VARIANT, locale))
        self._copy.setText(translate(TranslationKey.HISTORY_COPY, locale))
        self._copy.setAccessibleName(translate(TranslationKey.HISTORY_COPY, locale))
        self._audio.setText(translate(TranslationKey.HISTORY_AUDIO, locale))
        self._audio.setAccessibleName(translate(TranslationKey.HISTORY_AUDIO, locale))
        self._folder.setText(translate(TranslationKey.HISTORY_FOLDER, locale))
        self._folder.setAccessibleName(translate(TranslationKey.HISTORY_FOLDER, locale))
        self._export_txt.setText(translate(TranslationKey.HISTORY_EXPORT_TXT_SHORT, locale))
        self._export_markdown.setText(
            translate(TranslationKey.HISTORY_EXPORT_MARKDOWN_SHORT, locale)
        )
        self._delete.setText(translate(TranslationKey.HISTORY_DELETE, locale))
        self._audio.setToolTip(
            translate(
                TranslationKey.HISTORY_AUDIO_ERROR
                if self._audio_error
                else TranslationKey.HISTORY_AUDIO
                if self._audio_available
                else TranslationKey.HISTORY_AUDIO_UNAVAILABLE,
                locale,
            )
        )
        self._folder.setToolTip(
            translate(
                TranslationKey.HISTORY_FOLDER_ERROR
                if self._folder_error
                else TranslationKey.HISTORY_FOLDER
                if self._folder_available
                else TranslationKey.HISTORY_FOLDER_UNAVAILABLE,
                locale,
            )
        )
        self._audio.setEnabled(self._audio_available)
        self._folder.setEnabled(self._folder_available)

    def mousePressEvent(self, event) -> None:
        if event.button() is Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


def _preview_text(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    visible = lines[:2]
    if len(lines) > 2:
        visible.append("...")
    return "\n".join(visible) or "..."
