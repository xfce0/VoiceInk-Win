"""Qt page for asynchronous paginated transcript history."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
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
from .localization import LocaleConfig, TranslationKey, translate


class HistoryPage(QWidget):
    """Load, inspect, copy, and delete records without waiting on the Qt thread."""

    def __init__(
        self,
        persistence: PersistenceService | None,
        parent: QWidget | None = None,
        locale_config: LocaleConfig | None = None,
        artifact_cleanup: Callable[[str], None] | None = None,
        history_deletion: HistoryDeletionService | None = None,
        text_files: TextFilePort | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("historyPage")
        self._persistence = persistence
        self._artifact_cleanup = artifact_cleanup
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
        self._export_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="history-export",
        )
        self._locale_config = locale_config or LocaleConfig(parent=self)
        self._locale_callback = self.apply_locale
        self._locale_config.locale_changed.connect(
            self._locale_callback, Qt.ConnectionType.AutoConnection
        )
        self._bridge = FutureBridge(self)
        self._copy_port = QtClipboardPort(self)
        self._records: tuple[HistoryRecord, ...] = ()
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
        search_row = QHBoxLayout()
        self._search = QLineEdit(self)
        self._search.setAccessibleName("Search transcript history")
        self._search.setPlaceholderText("Search source or transcript")
        self._search.returnPressed.connect(self._search_submitted)
        search_row.addWidget(self._search, 1)
        self._search_button = QPushButton("Search", self)
        self._search_button.setAccessibleName("Search transcript history")
        self._search_button.clicked.connect(self._search_submitted)
        search_row.addWidget(self._search_button)
        root.addLayout(search_row)
        body = QHBoxLayout()
        body.setSpacing(14)
        self._list = QListWidget(self)
        self._list.setObjectName("historyList")
        self._list.currentItemChanged.connect(self._select_item)
        body.addWidget(self._list, 1)
        detail = QVBoxLayout()
        self._metadata = QLabel(self)
        self._metadata.setObjectName("metadata")
        self._metadata.setWordWrap(True)
        detail.addWidget(self._metadata)
        self._text = QTextEdit(self)
        self._text.setReadOnly(True)
        detail.addWidget(self._text, 1)
        actions = QHBoxLayout()
        self._variant = QComboBox(self)
        self._variant.setAccessibleName("Transcript variant")
        self._variant.addItem("Original", "original")
        self._variant.addItem("Enhanced", "enhanced")
        self._variant.currentIndexChanged.connect(self._variant_changed)
        actions.addWidget(self._variant)
        self._copy = QPushButton(self)
        self._copy.setObjectName("secondaryButton")
        self._copy.clicked.connect(self._copy_selected)
        actions.addWidget(self._copy)
        self._export_txt = QPushButton("TXT", self)
        self._export_txt.setAccessibleName("Export selected transcript as TXT")
        self._export_txt.clicked.connect(lambda: self._export_selected("txt"))
        actions.addWidget(self._export_txt)
        self._export_markdown = QPushButton("Markdown", self)
        self._export_markdown.setAccessibleName("Export selected transcript as Markdown")
        self._export_markdown.clicked.connect(lambda: self._export_selected("markdown"))
        actions.addWidget(self._export_markdown)
        self._delete = QPushButton(self)
        self._delete.setObjectName("secondaryButton")
        self._delete.clicked.connect(self._delete_selected)
        actions.addWidget(self._delete)
        actions.addStretch(1)
        detail.addLayout(actions)
        body.addLayout(detail, 2)
        root.addLayout(body, 1)
        navigation = QHBoxLayout()
        self._previous = QPushButton(self)
        self._previous.clicked.connect(self._previous_page)
        navigation.addWidget(self._previous)
        self._next = QPushButton(self)
        self._next.clicked.connect(self._next_page)
        navigation.addWidget(self._next)
        navigation.addStretch(1)
        self._status = QLabel(self)
        self._status.setObjectName("metadata")
        navigation.addWidget(self._status)
        root.addLayout(navigation)
        self._error = QLabel(self)
        self._error.setObjectName("pageError")
        self._error.setWordWrap(True)
        root.addWidget(self._error)
        self.apply_locale()

    def refresh(self) -> None:
        self._cursor_stack.clear()
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
            self._error.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
            self._status.setText(self._t(TranslationKey.COMMON_ERROR))
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
            self._error.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
            self._status.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))
        else:
            self._error.clear()

    def _set_loading(self, loading: bool) -> None:
        self._status.setText(self._t(TranslationKey.COMMON_LOADING) if loading else "")
        self._list.setEnabled(not loading)
        self._previous.setEnabled(not loading and bool(self._cursor_stack))
        self._next.setEnabled(not loading and self._has_more)
        if loading:
            self._list.clear()
            self._text.clear()
            self._metadata.clear()
            self._variant.setEnabled(False)
            self._copy.setEnabled(False)
            self._export_txt.setEnabled(False)
            self._export_markdown.setEnabled(False)
            self._delete.setEnabled(False)
        else:
            self._variant.setEnabled(self._selected is not None)

    def _render_records(self) -> None:
        self._list.clear()
        self._selected = None
        self._text.clear()
        self._variant.setEnabled(False)
        self._metadata.setText(
            self._t(TranslationKey.HISTORY_EMPTY)
            if not self._records
            else self._t(TranslationKey.HISTORY_SELECT)
        )
        for record in self._records:
            item = QListWidgetItem(self._record_title(record), self._list)
            item.setData(Qt.ItemDataRole.UserRole, record.id)
        self._previous.setEnabled(bool(self._cursor_stack))
        self._next.setEnabled(self._has_more)
        self._status.setText(self._t(TranslationKey.COMMON_READY))
        self._error.clear()

    def _select_item(self, item: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        record_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._selected = next((record for record in self._records if record.id == record_id), None)
        if self._selected is None:
            self._text.clear()
            self._variant.setEnabled(False)
            self._metadata.setText(self._t(TranslationKey.HISTORY_SELECT))
            self._copy.setEnabled(False)
            self._export_txt.setEnabled(False)
            self._export_markdown.setEnabled(False)
            self._delete.setEnabled(False)
            return
        record = self._selected
        self._variant.setEnabled(True)
        self._variant.blockSignals(True)
        self._variant.setCurrentIndex(self._variant.findData(record.selected_variant.value))
        self._variant.blockSignals(False)
        self._metadata.setText(self._record_metadata(record))
        self._text.setPlainText(
            record.enhanced_text
            if record.selected_variant is TranscriptVariant.ENHANCED and record.enhanced_text
            else record.original_text
        )
        self._copy.setEnabled(bool(self._text.toPlainText()))
        self._export_txt.setEnabled(bool(self._text.toPlainText()))
        self._export_markdown.setEnabled(bool(self._text.toPlainText()))
        self._delete.setEnabled(True)

    def _copy_selected(self) -> None:
        if self._selected is None:
            return
        self._status.setText(self._t(TranslationKey.HISTORY_COPYING))
        operation = self._next_operation()
        self._copy_port.copy(
            self._text.toPlainText(),
            lambda error, request=operation: self._copy_finished(request, error),
        )

    def _search_submitted(self) -> None:
        self._cursor_stack.clear()
        self._load(None)

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
        self._text.setPlainText(
            updated.enhanced_text
            if variant is TranscriptVariant.ENHANCED and updated.enhanced_text
            else updated.original_text
        )
        self._copy.setEnabled(bool(self._text.toPlainText()))
        self._export_txt.setEnabled(bool(self._text.toPlainText()))
        self._export_markdown.setEnabled(bool(self._text.toPlainText()))
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
            self._error.setText(self._t(TranslationKey.COMMON_PERSISTENCE_UNAVAILABLE))

    def _next_operation(self) -> int:
        self._operation += 1
        return self._operation

    def _previous_page(self) -> None:
        if self._cursor_stack:
            self._load(self._cursor_stack.pop())

    def _next_page(self) -> None:
        if self._has_more and self._next_cursor:
            self._load(self._next_cursor, push_cursor=True)

    def _record_title(self, record: HistoryRecord) -> str:
        text = record.original_text.replace("\n", " ").strip()
        return text[:80] or self._t(TranslationKey.HISTORY_EMPTY_RECORD)

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

    def apply_locale(self, _locale: str | None = None) -> None:
        del _locale
        self._title.setText(self._t(TranslationKey.HISTORY_TITLE))
        self._subtitle.setText(self._t(TranslationKey.HISTORY_SUBTITLE))
        self._copy.setText(self._t(TranslationKey.HISTORY_COPY))
        self._variant.setItemText(0, self._t(TranslationKey.HISTORY_ORIGINAL))
        self._variant.setItemText(1, self._t(TranslationKey.HISTORY_ENHANCED))
        self._variant.setAccessibleName(self._t(TranslationKey.HISTORY_VARIANT))
        self._export_txt.setText(self._t(TranslationKey.HISTORY_EXPORT_TXT_SHORT))
        self._export_markdown.setText(self._t(TranslationKey.HISTORY_EXPORT_MARKDOWN_SHORT))
        self._delete.setText(self._t(TranslationKey.HISTORY_DELETE))
        self._previous.setText(self._t(TranslationKey.HISTORY_PREVIOUS))
        self._next.setText(self._t(TranslationKey.HISTORY_NEXT))
        if self._selected is not None:
            self._metadata.setText(self._record_metadata(self._selected))
        elif not self._records:
            self._metadata.setText(self._t(TranslationKey.HISTORY_EMPTY))
        else:
            self._metadata.setText(self._t(TranslationKey.HISTORY_SELECT))
        for index, record in enumerate(self._records):
            self._list.item(index).setText(self._record_title(record))

    def _t(self, key: TranslationKey, **values: object) -> str:
        return translate(key, self._locale_config.locale, **values)

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
