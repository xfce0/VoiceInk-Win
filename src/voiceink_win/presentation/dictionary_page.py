"""Qt page for persisted dictionary replacement rules."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from voiceink_win.application import PersistenceService
from voiceink_win.domain import DictionaryEntry

from .async_tools import FutureBridge
from .localization import LocaleConfig, TranslationKey, translate


class DictionaryPage(QWidget):
    """Manage deterministic replacement rules; the transcription hook remains future work."""

    def __init__(
        self,
        persistence: PersistenceService | None,
        parent: QWidget | None = None,
        locale_config: LocaleConfig | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("dictionaryPage")
        self._persistence = persistence
        self._locale_config = locale_config or LocaleConfig(parent=self)
        self._locale_callback = self.apply_locale
        self._locale_config.locale_changed.connect(
            self._locale_callback, Qt.ConnectionType.AutoConnection
        )
        self._bridge = FutureBridge(self)
        self._entries: tuple[DictionaryEntry, ...] = ()
        self._editing_id: str | None = None
        self._generation = 0
        self._operation = 0
        self._disposed = False
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 28, 30, 24)
        root.setSpacing(14)
        self._title = QLabel(self)
        self._title.setObjectName("pageGreeting")
        root.addWidget(self._title)
        self._subtitle = QLabel(self)
        self._subtitle.setObjectName("heroSubtext")
        self._subtitle.setWordWrap(True)
        root.addWidget(self._subtitle)
        self._availability = QLabel(self)
        self._availability.setObjectName("pageUnavailable")
        self._availability.setWordWrap(True)
        root.addWidget(self._availability)
        body = QHBoxLayout()
        body.setSpacing(14)
        self._list = QListWidget(self)
        self._list.currentItemChanged.connect(self._select_item)
        body.addWidget(self._list, 1)
        editor = QVBoxLayout()
        form = QFormLayout()
        self._phrase_label = QLabel(self)
        self._phrase_label.setObjectName("metadata")
        self._phrase = QLineEdit(self)
        self._phrase.setAccessibleName("Dictionary phrase")
        form.addRow(self._phrase_label, self._phrase)
        self._replacement_label = QLabel(self)
        self._replacement_label.setObjectName("metadata")
        self._replacement = QLineEdit(self)
        self._replacement.setAccessibleName("Dictionary replacement")
        form.addRow(self._replacement_label, self._replacement)
        self._enabled = QCheckBox(self)
        self._enabled.setAccessibleName("Enable dictionary entry")
        form.addRow(self._enabled)
        editor.addLayout(form)
        buttons = QHBoxLayout()
        self._new = QPushButton(self)
        self._new.setObjectName("secondaryButton")
        self._new.clicked.connect(self._new_entry)
        buttons.addWidget(self._new)
        self._save = QPushButton(self)
        self._save.setObjectName("primaryButton")
        self._save.clicked.connect(self._save_entry)
        buttons.addWidget(self._save)
        self._delete = QPushButton(self)
        self._delete.setObjectName("secondaryButton")
        self._delete.clicked.connect(self._delete_entry)
        buttons.addWidget(self._delete)
        editor.addLayout(buttons)
        self._status = QLabel(self)
        self._status.setObjectName("metadata")
        editor.addWidget(self._status)
        self._error = QLabel(self)
        self._error.setObjectName("inlineError")
        self._error.setWordWrap(True)
        editor.addWidget(self._error)
        editor.addStretch(1)
        body.addLayout(editor, 2)
        root.addLayout(body, 1)
        self.apply_locale()

    def refresh(self) -> None:
        if self._disposed:
            return
        self._generation += 1
        self._operation += 1
        generation = self._generation
        self._availability.clear()
        self._availability.setVisible(False)
        self._error.clear()
        if self._persistence is None:
            self._render_entries((), unavailable=True)
            return
        self._set_loading(True)
        self._bridge.watch(
            self._persistence.list_dictionary(),
            lambda entries, error: self._loaded(generation, entries, error),
        )

    def _loaded(
        self,
        generation: int,
        entries: tuple[DictionaryEntry, ...] | None,
        error: BaseException | None,
    ) -> None:
        if self._disposed or generation != self._generation:
            return
        if error or entries is None:
            self._set_loading(False)
            self._show_unavailable()
            return
        self._entries = entries
        self._set_loading(False)
        self._render_entries(entries, unavailable=False)

    def _set_loading(self, loading: bool) -> None:
        self._list.setEnabled(not loading)
        self._set_editor_enabled(not loading)
        if loading:
            self._availability.clear()
            self._availability.setVisible(False)
            self._error.clear()
            self._status.setText(self._t(TranslationKey.COMMON_LOADING))

    def _render_entries(self, entries: tuple[DictionaryEntry, ...], *, unavailable: bool) -> None:
        selected_id = self._editing_id
        self._list.clear()
        for entry in entries:
            label = f"{entry.phrase} -> {entry.replacement}"
            item = QListWidgetItem(label, self._list)
            item.setData(Qt.ItemDataRole.UserRole, entry.id)
        self._status.setText("" if unavailable else self._t(TranslationKey.COMMON_READY))
        self._availability.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
        self._availability.setVisible(unavailable)
        self._error.clear()
        self._list.setEnabled(not unavailable)
        self._set_editor_enabled(not unavailable)
        self._delete.setEnabled(False)
        if selected_id is not None:
            for index in range(self._list.count()):
                if self._list.item(index).data(Qt.ItemDataRole.UserRole) == selected_id:
                    self._list.setCurrentRow(index)
                    break

    def _show_unavailable(self) -> None:
        self._status.clear()
        self._availability.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
        self._availability.setVisible(True)
        self._error.clear()
        self._list.setEnabled(False)
        self._set_editor_enabled(False)

    def _select_item(self, item: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        entry_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        entry = next((candidate for candidate in self._entries if candidate.id == entry_id), None)
        if entry is None:
            self._editing_id = None
            self._phrase.clear()
            self._replacement.clear()
            self._enabled.setChecked(True)
            self._delete.setEnabled(False)
            return
        self._editing_id = entry.id
        self._phrase.setText(entry.phrase)
        self._replacement.setText(entry.replacement)
        self._enabled.setChecked(entry.enabled)
        self._delete.setEnabled(True)

    def _new_entry(self) -> None:
        self._editing_id = None
        self._list.clearSelection()
        self._phrase.clear()
        self._replacement.clear()
        self._enabled.setChecked(True)
        self._delete.setEnabled(False)
        self._phrase.setFocus()

    def _save_entry(self) -> None:
        if self._persistence is None:
            return
        phrase = self._phrase.text().strip()
        if not phrase:
            self._error.setText(self._t(TranslationKey.DICTIONARY_PHRASE_REQUIRED))
            return
        existing = next((entry for entry in self._entries if entry.id == self._editing_id), None)
        now = datetime.now(UTC)
        entry = DictionaryEntry(
            id=existing.id if existing else uuid4().hex,
            phrase=phrase,
            replacement=self._replacement.text(),
            created_at=existing.created_at if existing else now,
            updated_at=now,
            enabled=self._enabled.isChecked(),
        )
        self._set_editor_enabled(False)
        self._status.setText(self._t(TranslationKey.COMMON_SAVING))
        generation = self._generation
        operation = self._next_operation()
        self._bridge.watch(
            self._persistence.upsert_dictionary(entry),
            lambda result, error: self._saved(generation, operation, result, error),
        )

    def _saved(
        self,
        generation: int,
        operation: int,
        _result: None,
        error: BaseException | None,
    ) -> None:
        if self._disposed or generation != self._generation or operation != self._operation:
            return
        self._set_editor_enabled(True)
        if error:
            self._status.setText(self._t(TranslationKey.COMMON_ERROR))
            self._error.setText(self._t(TranslationKey.DICTIONARY_SAVE_ERROR))
            return
        self._status.setText(self._t(TranslationKey.COMMON_SAVED))
        self.refresh()

    def _delete_entry(self) -> None:
        if self._persistence is None or self._editing_id is None:
            return
        answer = QMessageBox.question(
            self,
            self._t(TranslationKey.DICTIONARY_DELETE_TITLE),
            self._t(TranslationKey.DICTIONARY_DELETE_CONFIRM),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self._set_editor_enabled(False)
        self._status.setText(self._t(TranslationKey.COMMON_SAVING))
        generation = self._generation
        operation = self._next_operation()
        self._bridge.watch(
            self._persistence.delete_dictionary(self._editing_id),
            lambda result, error: self._deleted(generation, operation, result, error),
        )

    def _deleted(
        self,
        generation: int,
        operation: int,
        _result: None,
        error: BaseException | None,
    ) -> None:
        if self._disposed or generation != self._generation or operation != self._operation:
            return
        self._set_editor_enabled(True)
        if error:
            self._status.setText(self._t(TranslationKey.COMMON_ERROR))
            self._error.setText(self._t(TranslationKey.DICTIONARY_DELETE_ERROR))
            return
        self._editing_id = None
        self.refresh()

    def _set_editor_enabled(self, enabled: bool) -> None:
        for control in (
            self._phrase,
            self._replacement,
            self._enabled,
            self._new,
            self._save,
            self._delete,
        ):
            control.setEnabled(enabled)

    def apply_locale(self, _locale: str | None = None) -> None:
        del _locale
        self._title.setText(self._t(TranslationKey.DICTIONARY_TITLE))
        self._subtitle.setText(self._t(TranslationKey.DICTIONARY_SUBTITLE))
        self._availability.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
        self._phrase_label.setText(self._t(TranslationKey.DICTIONARY_PHRASE))
        self._replacement_label.setText(self._t(TranslationKey.DICTIONARY_REPLACEMENT))
        self._enabled.setText(self._t(TranslationKey.DICTIONARY_ENABLED))
        self._new.setText(self._t(TranslationKey.DICTIONARY_NEW))
        self._save.setText(self._t(TranslationKey.DICTIONARY_SAVE))
        self._delete.setText(self._t(TranslationKey.DICTIONARY_DELETE))

    def _t(self, key: TranslationKey, **values: object) -> str:
        return translate(key, self._locale_config.locale, **values)

    def _next_operation(self) -> int:
        self._operation += 1
        return self._operation

    def dispose(self) -> None:
        self._disposed = True
        self._generation += 1
        self._operation += 1
        try:
            self._locale_config.locale_changed.disconnect(self._locale_callback)
        except (RuntimeError, TypeError):
            pass
