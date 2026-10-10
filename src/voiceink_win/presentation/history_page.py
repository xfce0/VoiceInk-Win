"""Qt presentation for asynchronous paginated transcript history."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from voiceink_win.application import PersistenceService
from voiceink_win.application.persistence import HistoryDeletionService
from voiceink_win.application.transcribe_output import (
    LocalTextFilePort,
    TextFilePort,
    serialize_markdown,
    serialize_txt,
)
from voiceink_win.domain import HistoryPage as DomainHistoryPage
from voiceink_win.domain import (
    HistoryRecord,
    HistoryStatus,
    TranscriptDocument,
    TranscriptionSource,
    TranscriptVariant,
)

from .async_tools import FutureBridge
from .clipboard import QtClipboardPort
from .history_row import HistoryRow
from .localization import LocaleConfig, TranslationKey, translate


class HistoryAudioPort(Protocol):
    """Play a validated opaque audio-artifact reference."""

    def play(self, artifact_reference: str) -> None: ...


class HistoryFolderPort(Protocol):
    """Reveal a validated opaque source-folder reference."""

    def reveal(self, folder_reference: str) -> None: ...


class HistoryPage(QWidget):
    """Load, inspect, and act on records without waiting on the Qt thread."""

    def __init__(
        self,
        persistence: PersistenceService | None,
        parent: QWidget | None = None,
        locale_config: LocaleConfig | None = None,
        artifact_cleanup: Callable[[str], None] | None = None,
        artifact_reveal: Callable[[str], Path] | None = None,
        artifact_folder: Path | None = None,
        history_deletion: HistoryDeletionService | None = None,
        text_files: TextFilePort | None = None,
        audio_port: HistoryAudioPort | None = None,
        folder_port: HistoryFolderPort | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("historyPage")
        self._persistence = persistence
        self._artifact_cleanup = artifact_cleanup
        self._artifact_reveal = artifact_reveal
        self._artifact_folder = artifact_folder
        self._history_deletion = history_deletion or (
            HistoryDeletionService(persistence, artifact_cleanup)
            if persistence is not None
            else None
        )
        self._owns_history_deletion = (
            history_deletion is None and self._history_deletion is not None
        )
        if self._owns_history_deletion:
            self._history_deletion.start()
        self._text_files = text_files or LocalTextFilePort()
        self._audio_port = audio_port
        self._folder_port = folder_port
        self._export_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="history-export"
        )
        self._locale_config = locale_config or LocaleConfig(parent=self)
        self._locale_callback = self.apply_locale
        self._locale_config.locale_changed.connect(
            self._locale_callback, Qt.ConnectionType.AutoConnection
        )
        self._bridge = FutureBridge(self)
        self._copy_port = QtClipboardPort(self)
        self._records: tuple[HistoryRecord, ...] = ()
        self._rows: dict[str, HistoryRow] = {}
        self._offset = 0
        self._limit = 20
        self._has_more = False
        self._selected: HistoryRecord | None = None
        self._cursor: str | None = None
        self._next_cursor: str | None = None
        self._cursor_stack: list[str | None] = []
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
        search_row = QHBoxLayout()
        self._search = QLineEdit(self)
        self._search.setAccessibleName(self._t(TranslationKey.HISTORY_SEARCH_ACCESSIBLE))
        self._search.returnPressed.connect(self._search_submitted)
        search_row.addWidget(self._search, 1)
        self._search_button = QPushButton(self)
        self._search_button.setObjectName("actionButton")
        self._search_button.setAccessibleName(self._t(TranslationKey.HISTORY_SEARCH_ACCESSIBLE))
        self._search_button.clicked.connect(self._search_submitted)
        search_row.addWidget(self._search_button)
        root.addLayout(search_row)

        self._metadata = QLabel(self)
        self._metadata.setObjectName("metadata")
        self._metadata.setWordWrap(True)
        root.addWidget(self._metadata)
        self._list = QListWidget(self)
        self._list.setObjectName("historyList")
        self._list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._list.setMinimumWidth(0)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setSpacing(8)
        self._list.setFrameShape(QListWidget.Shape.NoFrame)
        self._list.currentItemChanged.connect(self._select_item)
        root.addWidget(self._list, 1)

        navigation = QHBoxLayout()
        self._previous = QPushButton(self)
        self._previous.setObjectName("actionButton")
        self._previous.clicked.connect(self._previous_page)
        navigation.addWidget(self._previous)
        self._next = QPushButton(self)
        self._next.setObjectName("actionButton")
        self._next.clicked.connect(self._next_page)
        navigation.addWidget(self._next)
        navigation.addStretch(1)
        self._status = QLabel(self)
        self._status.setObjectName("metadata")
        navigation.addWidget(self._status)
        root.addLayout(navigation)

        self._error = QLabel(self)
        self._error.setObjectName("inlineError")
        self._error.setWordWrap(True)
        root.addWidget(self._error)

        # Keep these private handles for existing presentation tests and callers.
        self._copy = QPushButton(self)
        self._variant = QComboBox(self)
        self._text = QTextEdit(self)
        self._text.setReadOnly(True)
        self._text.setVisible(False)
        self._export_txt = QPushButton(self)
        self._export_markdown = QPushButton(self)
        self._delete = QPushButton(self)
        self._fallback_copy = self._copy
        self._fallback_variant = self._variant
        self._fallback_export_txt = self._export_txt
        self._fallback_export_markdown = self._export_markdown
        self._fallback_delete = self._delete
        for control in (
            self._copy,
            self._variant,
            self._export_txt,
            self._export_markdown,
            self._delete,
        ):
            control.setVisible(False)
        self.apply_locale()

    def refresh(self) -> None:
        self._cursor_stack.clear()
        self._cursor = None
        self._offset = 0
        self._next_cursor = None
        self._load(None)

    def _load(self, cursor: str | int | None, *, push_cursor: bool = False) -> None:
        if self._disposed:
            return
        self._operation += 1
        if push_cursor:
            self._cursor_stack.append(self._cursor)
        if isinstance(cursor, int):
            self._offset = max(0, cursor)
            self._cursor = None
        else:
            self._cursor = cursor
            self._offset = 0
        self._generation += 1
        generation = self._generation
        if self._persistence is None:
            self._render_page(None, unavailable=True)
            return
        self._set_loading(True)
        try:
            future = self._persistence.list_history(
                cursor=self._cursor,
                limit=self._limit,
                search=self._search.text().strip() or None,
                offset=self._offset,
            )
        except TypeError:
            future = self._persistence.list_history(offset=self._offset, limit=self._limit)
        self._bridge.watch(future, lambda page, error: self._loaded(generation, page, error))

    def _loaded(
        self, generation: int, page: DomainHistoryPage | None, error: BaseException | None
    ) -> None:
        if self._disposed or generation != self._generation:
            return
        if error is not None or page is None:
            self._set_loading(False)
            self._show_unavailable()
            return
        self._records = page.records
        self._has_more = page.has_more
        self._next_cursor = page.next_cursor
        self._set_loading(False)
        self._render_records()

    def _render_page(self, _page: DomainHistoryPage | None, *, unavailable: bool) -> None:
        self._records = ()
        self._has_more = False
        self._next_cursor = None
        self._set_loading(False)
        self._render_records()
        if unavailable:
            self._show_unavailable()

    def _set_loading(self, loading: bool) -> None:
        self._status.setText(self._t(TranslationKey.COMMON_LOADING) if loading else "")
        self._list.setEnabled(not loading)
        self._search.setEnabled(not loading)
        self._search_button.setEnabled(not loading)
        self._previous.setEnabled(not loading and bool(self._cursor_stack))
        self._next.setEnabled(not loading and self._has_more)
        if loading:
            self._availability.clear()
            self._availability.setVisible(False)
            self._error.clear()
            self._list.clear()
            self._rows.clear()
            self._selected = None
            self._text.clear()
            self._variant.setEnabled(False)
            self._copy.setEnabled(False)
            self._export_txt.setEnabled(False)
            self._export_markdown.setEnabled(False)
            self._delete.setEnabled(False)

    def _render_records(self) -> None:
        self._list.clear()
        self._rows.clear()
        self._selected = None
        self._text.clear()
        self._bind_action_handles(None)
        self._variant.setEnabled(False)
        self._metadata.setText(
            self._t(TranslationKey.HISTORY_EMPTY)
            if not self._records
            else self._t(TranslationKey.HISTORY_SELECT)
        )
        for record in self._records:
            item = QListWidgetItem(self._list)
            item.setData(Qt.ItemDataRole.UserRole, record.id)
            row = self._build_row(record)
            row.clicked.connect(lambda record_id=record.id: self._row_clicked(record_id))
            self._rows[record.id] = row
            self._list.setItemWidget(item, row)
        self._resize_rows()
        self._previous.setEnabled(bool(self._cursor_stack))
        self._next.setEnabled(self._has_more)
        self._status.setText(self._t(TranslationKey.COMMON_READY))
        self._availability.clear()
        self._availability.setVisible(False)
        self._error.clear()

    def _build_row(self, record: HistoryRecord) -> HistoryRow:
        row = HistoryRow(
            record,
            self._record_metadata(record),
            self._locale_config,
            audio_available=bool(record.audio_artifact_path and self._audio_port),
            folder_available=bool(self._folder_reference(record) and self._folder_port),
            parent=self._list,
        )
        row.copy_button.clicked.connect(lambda: self._copy_record(record))
        row.audio_button.clicked.connect(lambda: self._play_audio(record))
        row.folder_button.clicked.connect(lambda: self._reveal_folder(record))
        row.export_txt_button.clicked.connect(lambda: self._export_record(record, "txt"))
        row.export_markdown_button.clicked.connect(lambda: self._export_record(record, "markdown"))
        row.delete_button.clicked.connect(lambda: self._delete_record(record))
        row.variant_combo.currentIndexChanged.connect(
            lambda index: self._variant_changed_for(record.id, index)
        )
        return row

    def _row_clicked(self, record_id: str) -> None:
        item = next(
            (
                self._list.item(index)
                for index in range(self._list.count())
                if self._list.item(index).data(Qt.ItemDataRole.UserRole) == record_id
            ),
            None,
        )
        if item is None:
            return
        if self._selected is not None and self._selected.id == record_id:
            row = self._rows[record_id]
            row.set_expanded(not row.expanded)
            self._resize_rows()
            return
        self._list.setCurrentItem(item)

    def _resize_row(self, item: QListWidgetItem, row: HistoryRow) -> None:
        width = self._list.viewport().width()
        if width <= 0:
            return
        row.resize(width, max(1, row.height()))
        row.updateGeometry()
        height = row.heightForWidth(width)
        if height <= 0:
            height = row.sizeHint().height()
        item.setSizeHint(QSize(width, height))
        self._list.doItemsLayout()

    def _resize_rows(self) -> None:
        for _ in range(3):
            width = self._list.viewport().width()
            if width <= 0:
                return
            for index in range(self._list.count()):
                item = self._list.item(index)
                record_id = item.data(Qt.ItemDataRole.UserRole)
                row = self._rows.get(record_id)
                if row is not None:
                    self._resize_row(item, row)
            self._list.doItemsLayout()
            if self._list.viewport().width() == width:
                return

    def _show_unavailable(self) -> None:
        self._status.clear()
        self._availability.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
        self._availability.setVisible(True)
        self._error.clear()
        self._list.setEnabled(False)
        self._search.setEnabled(False)
        self._search_button.setEnabled(False)
        self._variant.setEnabled(False)
        self._copy.setEnabled(False)
        self._export_txt.setEnabled(False)
        self._export_markdown.setEnabled(False)
        self._delete.setEnabled(False)
        self._previous.setEnabled(False)
        self._next.setEnabled(False)

    def _select_item(self, item: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        record_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._selected = next((record for record in self._records if record.id == record_id), None)
        for row_id, row in self._rows.items():
            selected = row_id == record_id
            row.set_selected(selected)
            row.set_expanded(selected)
        self._resize_rows()
        if self._selected is None:
            self._text.clear()
            self._bind_action_handles(None)
            self._variant.setEnabled(False)
            self._metadata.setText(self._t(TranslationKey.HISTORY_SELECT))
            self._copy.setEnabled(False)
            self._export_txt.setEnabled(False)
            self._export_markdown.setEnabled(False)
            self._delete.setEnabled(False)
            return
        record = self._selected
        row = self._rows[record.id]
        self._bind_action_handles(row)
        self._variant.setEnabled(True)
        self._variant.blockSignals(True)
        self._variant.setCurrentIndex(self._variant.findData(record.selected_variant.value))
        self._variant.blockSignals(False)
        self._metadata.setText(self._record_metadata(record))
        self._text.setPlainText(self._text_for(record))
        self._copy.setEnabled(bool(self._text.toPlainText()))
        self._export_txt.setEnabled(bool(self._text.toPlainText()))
        self._export_markdown.setEnabled(bool(self._text.toPlainText()))
        self._delete.setEnabled(True)

    def _bind_action_handles(self, row: HistoryRow | None) -> None:
        if row is None:
            self._copy = self._fallback_copy
            self._variant = self._fallback_variant
            self._export_txt = self._fallback_export_txt
            self._export_markdown = self._fallback_export_markdown
            self._delete = self._fallback_delete
            return
        self._copy = row.copy_button
        self._variant = row.variant_combo
        self._export_txt = row.export_txt_button
        self._export_markdown = row.export_markdown_button
        self._delete = row.delete_button

    def _copy_record(self, record: HistoryRecord) -> None:
        self._select_record_if_needed(record.id)
        self._copy_selected()

    def _copy_selected(self) -> None:
        if self._selected is None:
            return
        self._status.setText(self._t(TranslationKey.HISTORY_COPYING))
        operation = self._next_operation()
        self._copy_port.copy(
            self._text_for(self._selected),
            lambda error, request=operation: self._copy_finished(request, error),
        )

    def _play_audio(self, record: HistoryRecord) -> None:
        if self._audio_port is None or not record.audio_artifact_path:
            return
        self._select_record_if_needed(record.id)
        try:
            self._audio_port.play(record.audio_artifact_path)
        except BaseException:
            self._status.setText(self._t(TranslationKey.COMMON_ERROR))
            self._error.setText(self._t(TranslationKey.HISTORY_AUDIO_ERROR))
        else:
            self._status.setText(self._t(TranslationKey.HISTORY_AUDIO_STARTED))

    def _reveal_folder(self, record: HistoryRecord) -> None:
        reference = self._folder_reference(record)
        if self._folder_port is None or reference is None:
            return
        self._select_record_if_needed(record.id)
        try:
            self._folder_port.reveal(reference)
        except BaseException:
            self._status.setText(self._t(TranslationKey.COMMON_ERROR))
            self._error.setText(self._t(TranslationKey.HISTORY_FOLDER_ERROR))
        else:
            self._status.setText(self._t(TranslationKey.HISTORY_FOLDER_OPENED))

    def _search_submitted(self) -> None:
        self._cursor_stack.clear()
        self._cursor = None
        self._offset = 0
        self._next_cursor = None
        self._load(None)

    def _export_record(self, record: HistoryRecord, format_name: str) -> None:
        self._select_record_if_needed(record.id)
        self._export_selected(format_name)

    def _export_selected(self, format_name: str) -> None:
        record = self._selected
        if record is None:
            return
        suffix = ".txt" if format_name == "txt" else ".md"
        title = (
            self._t(TranslationKey.HISTORY_EXPORT_TXT)
            if format_name == "txt"
            else self._t(TranslationKey.HISTORY_EXPORT_MARKDOWN)
        )
        target, _ = QFileDialog.getSaveFileName(self, title, f"history{suffix}")
        if not target:
            return
        variant = TranscriptVariant(self._variant.currentData())
        document = TranscriptDocument(
            str(record.source_metadata.get("source_name", record.source)),
            record.created_at.isoformat(),
            record.duration,
            record.original_text,
            record.enhanced_text,
            record.selected_variant,
        )
        content = (
            serialize_txt(document, variant)
            if format_name == "txt"
            else serialize_markdown(document, variant)
        )
        operation = self._next_operation()
        self._status.setText(self._t(TranslationKey.HISTORY_EXPORTING))
        future = self._export_executor.submit(self._text_files.write_atomic, Path(target), content)
        self._bridge.watch(
            future,
            lambda _result, error, request=operation: self._export_finished(request, error),
        )

    def _export_finished(self, operation: int, error: BaseException | None) -> None:
        if self._disposed or operation != self._operation:
            return
        self._status.setText(
            self._t(TranslationKey.HISTORY_EXPORT_ERROR)
            if error
            else self._t(TranslationKey.HISTORY_EXPORTED)
        )

    def _copy_finished(self, operation: int, error: BaseException | None) -> None:
        if self._disposed or operation != self._operation:
            return
        self._status.setText(
            self._t(TranslationKey.COMMON_ERROR if error else TranslationKey.HISTORY_COPIED)
        )
        if error:
            self._error.setText(self._t(TranslationKey.HISTORY_COPY_ERROR))

    def _delete_record(self, record: HistoryRecord) -> None:
        self._select_record_if_needed(record.id)
        self._delete_selected()

    def _delete_selected(self) -> None:
        record = self._selected
        if record is None or self._persistence is None:
            return
        answer = QMessageBox.question(
            self,
            self._t(TranslationKey.HISTORY_DELETE_TITLE),
            self._t(TranslationKey.HISTORY_DELETE_CONFIRM),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self._copy.setEnabled(False)
        self._delete.setEnabled(False)
        self._status.setText(self._t(TranslationKey.HISTORY_DELETING))
        operation = self._next_operation()
        if self._history_deletion is None:
            self._status.setText(self._t(TranslationKey.COMMON_ERROR))
            self._error.setText(self._t(TranslationKey.HISTORY_DELETE_ERROR))
            return
        self._bridge.watch(
            self._history_deletion.delete(record.id, record.audio_artifact_path),
            lambda _result, error, request=operation: self._deleted(request, error),
        )

    def _deleted(self, operation: int, error: BaseException | None) -> None:
        if self._disposed or operation != self._operation:
            return
        if error is not None:
            self._status.setText(self._t(TranslationKey.COMMON_ERROR))
            self._error.setText(self._t(TranslationKey.HISTORY_DELETE_ERROR))
            self._delete.setEnabled(True)
            return
        self._load(self._offset)

    def _variant_changed_for(self, record_id: str, index: int) -> None:
        if self._selected is None or self._selected.id != record_id or index < 0:
            return
        self._variant_changed(index)

    def _variant_changed(self, index: int) -> None:
        record = self._selected
        if self._disposed or record is None or index < 0:
            return
        variant = TranscriptVariant(self._variant.itemData(index))
        if variant is TranscriptVariant.ENHANCED and not record.enhanced_text:
            self._variant.blockSignals(True)
            self._variant.setCurrentIndex(self._variant.findData(TranscriptVariant.ORIGINAL.value))
            self._variant.blockSignals(False)
            return
        updated = replace(record, selected_variant=variant)
        self._selected = updated
        self._records = tuple(updated if item.id == record.id else item for item in self._records)
        self._text.setPlainText(self._text_for(updated))
        self._copy.setEnabled(bool(self._text.toPlainText()))
        self._export_txt.setEnabled(bool(self._text.toPlainText()))
        self._export_markdown.setEnabled(bool(self._text.toPlainText()))
        row = self._rows.get(record.id)
        if row is not None:
            row.set_record(updated)
            self._resize_rows()
        if self._persistence is not None:
            operation = self._next_operation()
            self._bridge.watch(
                self._persistence.update_history_variant(record.id, variant),
                lambda _result, error, request=operation: self._variant_saved(request, error),
            )

    def _variant_saved(self, operation: int, error: BaseException | None) -> None:
        if self._disposed or operation != self._operation:
            return
        if error is not None:
            self._error.setText(self._t(TranslationKey.HISTORY_VARIANT_ERROR))

    def _select_record_if_needed(self, record_id: str) -> None:
        if self._selected is not None and self._selected.id == record_id:
            return
        self._row_clicked(record_id)

    def _next_operation(self) -> int:
        self._operation += 1
        return self._operation

    def _previous_page(self) -> None:
        if self._cursor_stack:
            self._load(self._cursor_stack.pop())

    def _next_page(self) -> None:
        if self._has_more and self._next_cursor:
            self._load(self._next_cursor, push_cursor=True)

    def _text_for(self, record: HistoryRecord) -> str:
        return (
            record.enhanced_text
            if record.selected_variant is TranscriptVariant.ENHANCED and record.enhanced_text
            else record.original_text
        )

    def _record_metadata(self, record: HistoryRecord) -> str:
        status = self._t(
            {
                HistoryStatus.COMPLETED: TranslationKey.HISTORY_STATUS_COMPLETED,
                HistoryStatus.PENDING: TranslationKey.HISTORY_STATUS_PENDING,
                HistoryStatus.FAILED: TranslationKey.HISTORY_STATUS_FAILED,
                HistoryStatus.CANCELLED: TranslationKey.HISTORY_STATUS_FAILED,
            }[record.status]
        )
        return self._t(
            TranslationKey.HISTORY_METADATA,
            date=record.created_at.astimezone().strftime("%Y-%m-%d %H:%M"),
            source=self._source_text(record.source),
            duration=f"{record.duration:.1f}",
            status=status,
        )

    def _source_text(self, source: str) -> str:
        return self._t(
            {
                TranscriptionSource.MICROPHONE.value: TranslationKey.HISTORY_SOURCE_MICROPHONE,
                TranscriptionSource.IMPORTED_FILE.value: TranslationKey.HISTORY_SOURCE_IMPORTED,
                TranscriptionSource.PASTE.value: TranslationKey.HISTORY_SOURCE_PASTE,
            }.get(source, TranslationKey.HISTORY_SOURCE_OTHER)
        )

    @staticmethod
    def _folder_reference(record: HistoryRecord) -> str | None:
        value = record.source_metadata.get("folder_reference")
        return value.strip() if isinstance(value, str) and value.strip() else None

    def apply_locale(self, _locale: str | None = None) -> None:
        del _locale
        self._title.setText(self._t(TranslationKey.HISTORY_TITLE))
        self._subtitle.setText(self._t(TranslationKey.HISTORY_SUBTITLE))
        self._search.setPlaceholderText(self._t(TranslationKey.HISTORY_SEARCH_PLACEHOLDER))
        self._search_button.setText(self._t(TranslationKey.HISTORY_SEARCH))
        self._search.setAccessibleName(self._t(TranslationKey.HISTORY_SEARCH_ACCESSIBLE))
        self._search_button.setAccessibleName(self._t(TranslationKey.HISTORY_SEARCH_ACCESSIBLE))
        self._availability.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
        self._copy.setText(self._t(TranslationKey.HISTORY_COPY))
        self._variant.setAccessibleName(self._t(TranslationKey.HISTORY_VARIANT))
        self._export_txt.setText(self._t(TranslationKey.HISTORY_EXPORT_TXT_SHORT))
        self._export_markdown.setText(self._t(TranslationKey.HISTORY_EXPORT_MARKDOWN_SHORT))
        self._delete.setText(self._t(TranslationKey.HISTORY_DELETE))
        self._previous.setText(self._t(TranslationKey.HISTORY_PREVIOUS))
        self._next.setText(self._t(TranslationKey.HISTORY_NEXT))
        for record in self._records:
            row = self._rows.get(record.id)
            if row is None:
                continue
            row.set_metadata(self._record_metadata(record))
            row.apply_locale()
        if self._selected is not None:
            self._metadata.setText(self._record_metadata(self._selected))
        elif not self._records:
            self._metadata.setText(self._t(TranslationKey.HISTORY_EMPTY))
        else:
            self._metadata.setText(self._t(TranslationKey.HISTORY_SELECT))

    def _t(self, key: TranslationKey, **values: object) -> str:
        return translate(key, self._locale_config.locale, **values)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._resize_rows()
        QTimer.singleShot(0, self._resize_rows)

    def dispose(self) -> None:
        self._disposed = True
        self._generation += 1
        self._operation += 1
        if self._owns_history_deletion and self._history_deletion is not None:
            self._history_deletion.close()
        try:
            self._locale_config.locale_changed.disconnect(self._locale_callback)
        except (RuntimeError, TypeError):
            pass
        self._export_executor.shutdown(wait=False, cancel_futures=True)
