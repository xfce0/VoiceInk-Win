"""Qt page for persisted dictionary replacement rules."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from voiceink_win.application import PersistenceService
from voiceink_win.domain import DictionaryEntry

from .async_tools import FutureBridge
from .localization import LocaleConfig, TranslationKey, translate


class _DictionaryRow(QFrame):
    edit_requested = Signal(str)
    delete_requested = Signal(str)

    def __init__(self, entry: DictionaryEntry, parent: QWidget) -> None:
        super().__init__(parent)
        self._entry_id = entry.id
        self.setObjectName("dictionaryRow")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setProperty("ruleEnabled", entry.enabled)
        self.setAccessibleName(entry.phrase)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 9, 10, 9)
        layout.setSpacing(12)

        text = QVBoxLayout()
        text.setSpacing(2)
        self._phrase = QLabel(entry.phrase, self)
        self._phrase.setObjectName("dictionaryPhrase")
        self._phrase.setWordWrap(True)
        self._phrase.setMinimumWidth(0)
        text.addWidget(self._phrase)
        self._replacement = QLabel(f"→ {entry.replacement}", self)
        self._replacement.setObjectName("dictionaryReplacement")
        self._replacement.setWordWrap(True)
        self._replacement.setMinimumWidth(0)
        text.addWidget(self._replacement)
        layout.addLayout(text, 1)

        self._edit = QPushButton(self)
        self._edit.setObjectName("dictionaryAction")
        self._edit.clicked.connect(lambda: self.edit_requested.emit(self._entry_id))
        layout.addWidget(self._edit)
        self._delete = QPushButton(self)
        self._delete.setObjectName("dictionaryAction")
        self._delete.clicked.connect(lambda: self.delete_requested.emit(self._entry_id))
        layout.addWidget(self._delete)

    def sizeHint(self) -> QSize:
        return QSize(0, max(66, super().sizeHint().height()))

    def heightForWidth(self, width: int) -> int:
        layout = self.layout()
        return max(66, layout.heightForWidth(width)) if layout is not None else 66

    def apply_locale(self, edit_label: str, delete_label: str, edit_description: str) -> None:
        self._edit.setText(edit_label)
        self._edit.setAccessibleName(f"{edit_description}: {self._phrase.text()}")
        self._delete.setText(delete_label)
        self._delete.setAccessibleName(f"{delete_label}: {self._phrase.text()}")

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)


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
        self._row_widgets: dict[str, _DictionaryRow] = {}
        self._editing_id: str | None = None
        self._generation = 0
        self._operation = 0
        self._loading = False
        self._disposed = False
        self._build_ui()
        self.refresh()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 28, 30, 24)
        root.setSpacing(14)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(4)
        self._title = QLabel(self)
        self._title.setObjectName("pageGreeting")
        heading.addWidget(self._title)
        self._subtitle = QLabel(self)
        self._subtitle.setObjectName("heroSubtext")
        self._subtitle.setWordWrap(True)
        heading.addWidget(self._subtitle)
        header.addLayout(heading, 1)
        self._new = QPushButton(self)
        self._new.setObjectName("primaryButton")
        self._new.setAccessibleName("Add dictionary rule")
        self._new.clicked.connect(self._new_entry)
        header.addWidget(self._new, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(header)

        self._state_panel = QFrame(self)
        self._state_panel.setObjectName("dictionaryState")
        state_layout = QVBoxLayout(self._state_panel)
        state_layout.setContentsMargins(18, 14, 18, 14)
        state_layout.setSpacing(5)
        self._state_title = QLabel(self._state_panel)
        self._state_title.setObjectName("sectionTitle")
        state_layout.addWidget(self._state_title)
        self._state_detail = QLabel(self._state_panel)
        self._state_detail.setObjectName("metadata")
        self._state_detail.setWordWrap(True)
        state_layout.addWidget(self._state_detail)
        self._state_action = QPushButton(self._state_panel)
        self._state_action.setObjectName("actionButton")
        self._state_action.clicked.connect(self.refresh)
        state_layout.addWidget(self._state_action, 0, Qt.AlignmentFlag.AlignLeft)
        root.addWidget(self._state_panel)
        self._list = QListWidget(self)
        self._list.setObjectName("dictionaryList")
        self._list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._list.setMinimumWidth(0)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setSpacing(6)
        self._list.setMinimumHeight(110)
        self._list.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._list.currentItemChanged.connect(self._select_item)
        self._list.itemActivated.connect(self._activate_item)
        root.addWidget(self._list, 1)

        editor = QFrame(self)
        editor.setObjectName("dictionaryEditor")
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(18, 14, 18, 14)
        editor_layout.setSpacing(10)
        self._editor_title = QLabel(editor)
        self._editor_title.setObjectName("sectionTitle")
        editor_layout.addWidget(self._editor_title)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        self._phrase_label = QLabel(editor)
        self._phrase_label.setObjectName("metadata")
        self._phrase = QLineEdit(editor)
        self._phrase.setAccessibleName("Dictionary phrase")
        form.addRow(self._phrase_label, self._phrase)
        self._replacement_label = QLabel(editor)
        self._replacement_label.setObjectName("metadata")
        self._replacement = QLineEdit(editor)
        self._replacement.setAccessibleName("Dictionary replacement")
        form.addRow(self._replacement_label, self._replacement)
        self._enabled = QCheckBox(editor)
        self._enabled.setAccessibleName("Enable dictionary entry")
        form.addRow(self._enabled)
        editor_layout.addLayout(form)

        actions = QHBoxLayout()
        self._save = QPushButton(editor)
        self._save.setObjectName("primaryButton")
        self._save.clicked.connect(self._save_entry)
        actions.addWidget(self._save)
        self._delete = QPushButton(editor)
        self._delete.setObjectName("secondaryButton")
        self._delete.clicked.connect(self._delete_entry)
        actions.addWidget(self._delete)
        actions.addStretch(1)
        editor_layout.addLayout(actions)
        self._status = QLabel(editor)
        self._status.setObjectName("metadata")
        editor_layout.addWidget(self._status)
        self._error = QLabel(editor)
        self._error.setObjectName("pageError")
        self._error.setWordWrap(True)
        self._error.setVisible(False)
        editor_layout.addWidget(self._error)
        root.addWidget(editor)

        self.setTabOrder(self._new, self._phrase)
        self.setTabOrder(self._phrase, self._replacement)
        self.setTabOrder(self._replacement, self._enabled)
        self.setTabOrder(self._enabled, self._save)
        self.setTabOrder(self._save, self._delete)
        self.apply_locale()

    def refresh(self) -> None:
        if self._disposed:
            return
        self._generation += 1
        self._operation += 1
        generation = self._generation
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
            self._loading = False
            self._set_editor_enabled(False)
            self._show_state(
                self._t(TranslationKey.COMMON_ERROR),
                self._t(TranslationKey.DICTIONARY_ERROR_DETAIL),
                action=True,
            )
            self._status.setText(self._t(TranslationKey.COMMON_ERROR))
            self._show_error(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
            return
        self._entries = entries
        self._set_loading(False)
        self._render_entries(entries, unavailable=False)

    def _set_loading(self, loading: bool) -> None:
        self._loading = loading
        if loading:
            self._list.clear()
            self._row_widgets.clear()
            self._list.setVisible(False)
            self._show_state(
                self._t(TranslationKey.COMMON_LOADING),
                self._t(TranslationKey.DICTIONARY_LOADING_DETAIL),
                action=False,
            )
            self._set_editor_enabled(False)
            self._status.setText(self._t(TranslationKey.COMMON_LOADING))

    def _render_entries(self, entries: tuple[DictionaryEntry, ...], *, unavailable: bool) -> None:
        selected_id = self._editing_id
        self._list.clear()
        self._row_widgets.clear()
        self._clear_error()
        for entry in entries:
            item = QListWidgetItem(self._list)
            item.setData(Qt.ItemDataRole.UserRole, entry.id)
            row = _DictionaryRow(entry, self)
            row.edit_requested.connect(self._edit_entry)
            row.delete_requested.connect(self._delete_entry)
            row.apply_locale(
                self._t(TranslationKey.DICTIONARY_EDIT),
                self._t(TranslationKey.DICTIONARY_DELETE),
                self._t(TranslationKey.DICTIONARY_EDIT_ACCESSIBLE),
            )
            self._row_widgets[entry.id] = row
            self._list.setItemWidget(item, row)
        self._resize_rows()
        self._list.setVisible(bool(entries) and not unavailable)
        self._set_editor_enabled(not unavailable)
        if unavailable:
            self._show_state(
                self._t(TranslationKey.COMMON_ERROR),
                self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE),
                action=False,
            )
            self._show_error(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
            self._status.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
        elif entries:
            self._state_panel.setVisible(False)
            self._status.setText(self._t(TranslationKey.COMMON_READY))
        else:
            self._show_state(
                self._t(TranslationKey.DICTIONARY_EMPTY),
                self._t(TranslationKey.DICTIONARY_EMPTY_DETAIL),
                action=False,
            )
            self._status.setText(self._t(TranslationKey.COMMON_READY))
        if selected_id is not None:
            for index in range(self._list.count()):
                if self._list.item(index).data(Qt.ItemDataRole.UserRole) == selected_id:
                    self._list.setCurrentRow(index)
                    break

    def _show_unavailable(self) -> None:
        self._status.clear()
        self._show_state(
            self._t(TranslationKey.COMMON_ERROR),
            self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE),
            action=False,
        )
        self._show_error(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))

    def _select_item(self, item: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        entry_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        entry = next((candidate for candidate in self._entries if candidate.id == entry_id), None)
        self._set_selected_row(entry_id)
        if entry is None:
            self._editing_id = None
            self._phrase.clear()
            self._replacement.clear()
            self._enabled.setChecked(True)
            self._delete.setEnabled(False)
            self._set_editor_title()
            return
        self._editing_id = entry.id
        self._phrase.setText(entry.phrase)
        self._replacement.setText(entry.replacement)
        self._enabled.setChecked(entry.enabled)
        self._delete.setEnabled(True)
        self._set_editor_title()
        self._clear_error()

    def _activate_item(self, item: QListWidgetItem, _column: int = 0) -> None:
        self._edit_entry(item.data(Qt.ItemDataRole.UserRole))

    def _edit_entry(self, entry_id: str) -> None:
        for index in range(self._list.count()):
            if self._list.item(index).data(Qt.ItemDataRole.UserRole) == entry_id:
                self._list.setCurrentRow(index)
                self._phrase.setFocus()
                return

    def _new_entry(self) -> None:
        self._editing_id = None
        self._list.clearSelection()
        self._phrase.clear()
        self._replacement.clear()
        self._enabled.setChecked(True)
        self._delete.setEnabled(False)
        self._set_editor_title()
        self._clear_error()
        self._phrase.setFocus()

    def _save_entry(self) -> None:
        if self._persistence is None:
            return
        phrase = self._phrase.text().strip()
        if not phrase:
            self._show_error(self._t(TranslationKey.DICTIONARY_PHRASE_REQUIRED))
            self._phrase.setFocus()
            return
        self._clear_error()
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
        self._set_busy(False)
        self._status.setText(self._t(TranslationKey.COMMON_SAVING))
        generation = self._generation
        operation = self._next_operation()
        self._bridge.watch(
            self._persistence.upsert_dictionary(entry),
            lambda result, error, entry_id=entry.id: self._saved(
                generation, operation, entry_id, result, error
            ),
        )

    def _saved(
        self,
        generation: int,
        operation: int,
        entry_id: str,
        _result: None,
        error: BaseException | None,
    ) -> None:
        if self._disposed or generation != self._generation or operation != self._operation:
            return
        self._set_busy(True)
        if error:
            self._status.setText(self._t(TranslationKey.COMMON_ERROR))
            self._show_error(self._t(TranslationKey.DICTIONARY_SAVE_ERROR))
            return
        self._editing_id = entry_id
        self._status.setText(self._t(TranslationKey.COMMON_SAVED))
        self.refresh()

    def _delete_entry(self, entry_id: str | None = None) -> None:
        selected_id = entry_id or self._editing_id
        if self._persistence is None or selected_id is None:
            return
        self._editing_id = selected_id
        answer = QMessageBox.question(
            self,
            self._t(TranslationKey.DICTIONARY_DELETE_TITLE),
            self._t(TranslationKey.DICTIONARY_DELETE_CONFIRM),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self._set_busy(False)
        self._status.setText(self._t(TranslationKey.COMMON_SAVING))
        generation = self._generation
        operation = self._next_operation()
        self._bridge.watch(
            self._persistence.delete_dictionary(selected_id),
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
        self._set_busy(True)
        if error:
            self._status.setText(self._t(TranslationKey.COMMON_ERROR))
            self._show_error(self._t(TranslationKey.DICTIONARY_DELETE_ERROR))
            return
        self._editing_id = None
        self.refresh()

    def _set_busy(self, enabled: bool) -> None:
        self._list.setEnabled(enabled)
        self._set_editor_enabled(enabled)

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

    def _show_state(self, title: str, detail: str, *, action: bool) -> None:
        self._state_title.setText(title)
        self._state_detail.setText(detail)
        self._state_action.setText(self._t(TranslationKey.DICTIONARY_RETRY) if action else "")
        self._state_action.setVisible(action)
        self._state_panel.setVisible(True)

    def _show_error(self, text: str) -> None:
        self._error.setText(text)
        self._error.setVisible(bool(text))

    def _clear_error(self) -> None:
        self._error.clear()
        self._error.setVisible(False)

    def _set_selected_row(self, entry_id: str | None) -> None:
        for row_id, row in self._row_widgets.items():
            row.set_selected(row_id == entry_id)

    def _resize_row(self, item: QListWidgetItem, row: _DictionaryRow) -> None:
        width = self._list.viewport().width()
        if width <= 0:
            return
        row.resize(width, max(1, row.height()))
        row.updateGeometry()
        height = row.heightForWidth(width)
        item.setSizeHint(QSize(width, height))
        self._list.doItemsLayout()

    def _resize_rows(self) -> None:
        for _ in range(3):
            width = self._list.viewport().width()
            if width <= 0:
                return
            for index in range(self._list.count()):
                item = self._list.item(index)
                entry_id = item.data(Qt.ItemDataRole.UserRole)
                row = self._row_widgets.get(entry_id)
                if row is not None:
                    self._resize_row(item, row)
            self._list.doItemsLayout()
            if self._list.viewport().width() == width:
                return

    def _set_editor_title(self) -> None:
        self._editor_title.setText(
            self._t(
                TranslationKey.DICTIONARY_EDITOR_EDIT
                if self._editing_id is not None
                else TranslationKey.DICTIONARY_EDITOR_NEW
            )
        )

    def apply_locale(self, _locale: str | None = None) -> None:
        del _locale
        self._title.setText(self._t(TranslationKey.DICTIONARY_TITLE))
        self._subtitle.setText(self._t(TranslationKey.DICTIONARY_SUBTITLE))
        self._phrase_label.setText(self._t(TranslationKey.DICTIONARY_PHRASE))
        self._replacement_label.setText(self._t(TranslationKey.DICTIONARY_REPLACEMENT))
        self._enabled.setText(self._t(TranslationKey.DICTIONARY_ENABLED))
        self._new.setText(self._t(TranslationKey.DICTIONARY_NEW))
        self._new.setAccessibleName(self._t(TranslationKey.DICTIONARY_NEW))
        self._save.setText(self._t(TranslationKey.DICTIONARY_SAVE))
        self._delete.setText(self._t(TranslationKey.DICTIONARY_DELETE))
        self._delete.setAccessibleName(self._t(TranslationKey.DICTIONARY_DELETE_ACCESSIBLE))
        if not self._state_action.isHidden():
            self._state_action.setText(self._t(TranslationKey.DICTIONARY_RETRY))
        for row in self._row_widgets.values():
            row.apply_locale(
                self._t(TranslationKey.DICTIONARY_EDIT),
                self._t(TranslationKey.DICTIONARY_DELETE),
                self._t(TranslationKey.DICTIONARY_EDIT_ACCESSIBLE),
            )
        self._set_editor_title()
        self._resize_rows()

    def _t(self, key: TranslationKey, **values: object) -> str:
        return translate(key, self._locale_config.locale, **values)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._resize_rows()
        QTimer.singleShot(0, self._resize_rows)

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
