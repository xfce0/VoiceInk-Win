"""Qt presentation for the imported-media transcription workflow."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from voiceink_win.application import TranscribePageController
from voiceink_win.domain import (
    SUPPORTED_MEDIA_EXTENSIONS,
    SUPPORTED_MEDIA_FORMATS,
    OutputState,
    QueueState,
    TranscribeAvailability,
    TranscribePageSnapshot,
    TranscriptionQueueItemSnapshot,
    TranscriptVariant,
)

from .localization import (
    Locale,
    LocaleConfig,
    TranslationKey,
    error_code_text,
    translate,
    translate_message,
)


class _SnapshotBridge(QObject):
    changed = Signal(object)


class _DropZone(QFrame):
    paths_dropped = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("transcribeDropZone")

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.paths_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class TranscribePage(QWidget):
    """A non-blocking queue view backed by ``TranscribePageController``."""

    def __init__(
        self,
        controller: TranscribePageController,
        parent: QWidget | None = None,
        locale_config: LocaleConfig | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("transcribePage")
        self._controller = controller
        self._locale_config = locale_config or LocaleConfig(parent=self)
        self._locale_signal = self._locale_config.locale_changed
        self._locale_callback = self.apply_locale
        self._locale_signal.connect(self._locale_callback, Qt.ConnectionType.AutoConnection)
        self._locale_connected = True
        self._bridge = _SnapshotBridge(self)
        self._bridge.changed.connect(self._render)
        self._queue_layout: QVBoxLayout
        self._build_ui()
        self._unsubscribe = controller.subscribe(self._bridge.changed.emit)
        self._render(controller.snapshot)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(30, 28, 30, 24)
        root.setSpacing(16)

        self._title = QLabel(self._t(TranslationKey.TRANSCRIBE_TITLE), self)
        self._title.setObjectName("pageGreeting")
        root.addWidget(self._title)
        self._subtitle = QLabel(self._t(TranslationKey.TRANSCRIBE_SUBTITLE), self)
        self._subtitle.setObjectName("heroSubtext")
        self._subtitle.setWordWrap(True)
        root.addWidget(self._subtitle)

        self._drop_zone = _DropZone(self)
        self._drop_zone.paths_dropped.connect(self._controller.add_paths)
        drop_layout = QVBoxLayout(self._drop_zone)
        drop_layout.setContentsMargins(24, 24, 24, 24)
        drop_layout.setSpacing(8)
        drop_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drop_prompt = QLabel(self._t(TranslationKey.TRANSCRIBE_DROP_PROMPT), self._drop_zone)
        self._drop_prompt.setObjectName("sectionTitle")
        self._drop_prompt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_layout.addWidget(self._drop_prompt)
        self._or_label = QLabel(self._t(TranslationKey.TRANSCRIBE_OR), self._drop_zone)
        self._or_label.setObjectName("muted")
        self._or_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_layout.addWidget(self._or_label)
        self._choose_button = QPushButton(
            self._t(TranslationKey.TRANSCRIBE_CHOOSE_FILES), self._drop_zone
        )
        self._choose_button.setObjectName("primaryButton")
        self._choose_button.setAccessibleName(
            self._t(TranslationKey.TRANSCRIBE_CHOOSE_FILES_ACCESSIBLE)
        )
        self._choose_button.clicked.connect(self._choose_files)
        drop_layout.addWidget(self._choose_button, 0, Qt.AlignmentFlag.AlignCenter)
        self._drop_zone.setMinimumHeight(150)
        root.addWidget(self._drop_zone)

        self._formats_label = QLabel(self._formats_text(), self)
        self._formats_label.setObjectName("muted")
        self._formats_label.setWordWrap(True)
        self._formats_label.setAccessibleName(
            self._t(TranslationKey.TRANSCRIBE_SUPPORTED_FORMATS_ACCESSIBLE)
        )
        root.addWidget(self._formats_label)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self._add_button = QPushButton(self._t(TranslationKey.TRANSCRIBE_ADD_FILES), self)
        self._add_button.setObjectName("secondaryButton")
        self._add_button.setAccessibleName(self._t(TranslationKey.TRANSCRIBE_ADD_FILES_ACCESSIBLE))
        self._add_button.clicked.connect(self._choose_files)
        controls.addWidget(self._add_button)
        self._start_button = QPushButton(self._t(TranslationKey.TRANSCRIBE_START), self)
        self._start_button.setObjectName("primaryButton")
        self._start_button.setAccessibleName(self._t(TranslationKey.TRANSCRIBE_START_ACCESSIBLE))
        self._start_button.clicked.connect(self._controller.start_queue)
        controls.addWidget(self._start_button)
        self._cancel_button = QPushButton(self._t(TranslationKey.TRANSCRIBE_CANCEL_ALL), self)
        self._cancel_button.setObjectName("secondaryButton")
        self._cancel_button.setAccessibleName(
            self._t(TranslationKey.TRANSCRIBE_CANCEL_ALL_ACCESSIBLE)
        )
        self._cancel_button.clicked.connect(self._controller.cancel_all)
        controls.addWidget(self._cancel_button)
        self._clear_button = QPushButton(self._t(TranslationKey.TRANSCRIBE_CLEAR_FINISHED), self)
        self._clear_button.setObjectName("secondaryButton")
        self._clear_button.setAccessibleName(
            self._t(TranslationKey.TRANSCRIBE_CLEAR_FINISHED_ACCESSIBLE)
        )
        self._clear_button.clicked.connect(self._controller.clear_terminal_items)
        controls.addWidget(self._clear_button)
        controls.addStretch(1)
        self._count_label = QLabel(self)
        self._count_label.setObjectName("muted")
        controls.addWidget(self._count_label)
        self._output_label = QLabel(self)
        self._output_label.setObjectName("muted")
        self._output_label.setWordWrap(True)
        controls.addWidget(self._output_label)
        root.addLayout(controls)

        self._error_label = QLabel(self)
        self._error_label.setObjectName("pageError")
        self._error_label.setWordWrap(True)
        root.addWidget(self._error_label)

        scroll = QScrollArea(self)
        scroll.setObjectName("transcribeQueueScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        queue = QWidget(scroll)
        queue.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._queue_layout = QVBoxLayout(queue)
        self._queue_layout.setContentsMargins(0, 0, 0, 0)
        self._queue_layout.setSpacing(10)
        self._queue_layout.addStretch(1)
        scroll.setWidget(queue)
        root.addWidget(scroll, 1)

    def _choose_files(self) -> None:
        if self._controller.snapshot.availability is not TranscribeAvailability.AVAILABLE:
            return
        extensions = " ".join(f"*.{extension}" for extension in sorted(SUPPORTED_MEDIA_EXTENSIONS))
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            self._t(TranslationKey.TRANSCRIBE_FILE_DIALOG_TITLE),
            "",
            f"{self._t(TranslationKey.TRANSCRIBE_SUPPORTED_MEDIA_FILTER, extensions=extensions)};;"
            f"{self._t(TranslationKey.TRANSCRIBE_ALL_FILES_FILTER)}",
        )
        if paths:
            self._controller.add_paths(paths)

    def _render(self, snapshot: TranscribePageSnapshot) -> None:
        available = snapshot.availability is TranscribeAvailability.AVAILABLE
        self._drop_zone.setEnabled(available)
        self._drop_zone.setAcceptDrops(available)
        self._choose_button.setEnabled(available)
        self._add_button.setEnabled(snapshot.accepting_files)
        self._start_button.setEnabled(snapshot.can_start)
        self._cancel_button.setEnabled(snapshot.can_cancel_all)
        self._clear_button.setEnabled(
            bool(
                snapshot.aggregate.succeeded
                + snapshot.aggregate.failed
                + snapshot.aggregate.cancelled
                + snapshot.aggregate.rejected
            )
        )
        self._count_label.setText(
            self._t(TranslationKey.TRANSCRIBE_FILE_COUNT, count=snapshot.aggregate.total)
        )
        self._error_label.setText(
            translate_message(snapshot.page_error, self._locale_config.locale)
        )
        self._error_label.setVisible(bool(snapshot.page_error))
        self._output_label.setText(
            translate_message(snapshot.output_status.message, self._locale_config.locale)
        )
        self._output_label.setVisible(snapshot.output_status.state is not OutputState.IDLE)
        self._output_label.setProperty("failed", snapshot.output_status.state is OutputState.FAILED)
        self._output_label.style().unpolish(self._output_label)
        self._output_label.style().polish(self._output_label)
        self._rebuild_queue(snapshot.items)

    def _rebuild_queue(self, items: tuple[TranscriptionQueueItemSnapshot, ...]) -> None:
        while self._queue_layout.count() > 1:
            child = self._queue_layout.takeAt(0)
            if child.widget() is not None:
                child.widget().deleteLater()
        for item in items:
            self._queue_layout.insertWidget(self._queue_layout.count() - 1, self._build_item(item))

    def _build_item(self, item: TranscriptionQueueItemSnapshot) -> QFrame:
        frame = QFrame(self)
        frame.setObjectName("transcribeItem")
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        name = QLabel(item.source_name, frame)
        name.setObjectName("sectionTitle")
        name.setToolTip(item.source_name)
        name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        header.addWidget(name)
        hint = (
            item.source_format_hint.value
            if item.source_format_hint
            else self._t(TranslationKey.TRANSCRIBE_UNKNOWN_FORMAT)
        )
        format_label = QLabel(hint, frame)
        format_label.setObjectName("muted")
        header.addWidget(format_label)
        status = QLabel(_status_text(item.state, self._locale_config.locale), frame)
        status.setObjectName("statePill")
        header.addWidget(status)
        layout.addLayout(header)

        progress = QProgressBar(frame)
        progress.setTextVisible(False)
        if item.state in {
            QueueState.VALIDATING,
            QueueState.QUEUED,
            QueueState.NORMALIZING,
            QueueState.TRANSCRIBING,
            QueueState.RETRY_WAITING,
            QueueState.CLEANING_UP,
        }:
            if item.progress.fraction is None:
                progress.setRange(0, 0)
            else:
                progress.setRange(0, 100)
                progress.setValue(round(item.progress.fraction * 100))
            layout.addWidget(progress)

        if item.failure is not None:
            failure = QLabel(
                f"{error_code_text(item.failure.code.value, self._locale_config.locale)}: "
                f"{translate_message(item.failure.message, self._locale_config.locale)}",
                frame,
            )
            failure.setObjectName("pageError")
            failure.setWordWrap(True)
            layout.addWidget(failure)

        actions = QHBoxLayout()
        actions.addStretch(1)
        if item.can_remove:
            remove = QPushButton(self._t(TranslationKey.TRANSCRIBE_REMOVE), frame)
            remove.setObjectName("actionButton")
            remove.clicked.connect(
                lambda _checked=False, item_id=item.item_id: self._controller.remove_pending(
                    item_id
                )
            )
            actions.addWidget(remove)
        if item.can_cancel:
            cancel = QPushButton(self._t(TranslationKey.TRANSCRIBE_CANCEL), frame)
            cancel.setObjectName("actionButton")
            cancel.clicked.connect(
                lambda _checked=False, item_id=item.item_id: self._controller.cancel_item(item_id)
            )
            actions.addWidget(cancel)
        if item.can_retry:
            retry = QPushButton(self._t(TranslationKey.TRANSCRIBE_RETRY), frame)
            retry.setObjectName("actionButton")
            retry.clicked.connect(
                lambda _checked=False, item_id=item.item_id: self._controller.retry_item(item_id)
            )
            actions.addWidget(retry)
        if item.result is not None:
            copy = QPushButton(self._t(TranslationKey.TRANSCRIBE_COPY), frame)
            copy.setObjectName("actionButton")
            copy.clicked.connect(
                lambda _checked=False, item_id=item.item_id: self._controller.copy(item_id)
            )
            actions.addWidget(copy)
            save_txt = QPushButton(self._t(TranslationKey.TRANSCRIBE_TXT), frame)
            save_txt.setObjectName("actionButton")
            save_txt.clicked.connect(
                lambda _checked=False, item_id=item.item_id: self._save(item_id, "txt")
            )
            actions.addWidget(save_txt)
            save_md = QPushButton(self._t(TranslationKey.TRANSCRIBE_MARKDOWN), frame)
            save_md.setObjectName("actionButton")
            save_md.clicked.connect(
                lambda _checked=False, item_id=item.item_id: self._save(item_id, "md")
            )
            actions.addWidget(save_md)
        if actions.count() > 1:
            layout.addLayout(actions)

        if item.result is not None:
            tabs = QTabWidget(frame)
            tabs.setAccessibleName(
                self._t(TranslationKey.TRANSCRIBE_VARIANTS, name=item.source_name)
            )
            original = QTextEdit(tabs)
            original.setReadOnly(True)
            original.setPlainText(item.result.original_text)
            tabs.addTab(original, self._t(TranslationKey.TRANSCRIBE_ORIGINAL))
            if item.result.enhanced_text:
                enhanced = QTextEdit(tabs)
                enhanced.setReadOnly(True)
                enhanced.setPlainText(item.result.enhanced_text)
                tabs.addTab(enhanced, self._t(TranslationKey.TRANSCRIBE_ENHANCED))
            tabs.setCurrentIndex(1 if item.selected_variant is TranscriptVariant.ENHANCED else 0)
            tabs.currentChanged.connect(
                lambda index, item_id=item.item_id: self._controller.select_variant(
                    item_id,
                    TranscriptVariant.ENHANCED if index == 1 else TranscriptVariant.ORIGINAL,
                )
            )
            layout.addWidget(tabs)
        return frame

    def _save(self, item_id: str, format_name: str) -> None:
        item = next(
            (item for item in self._controller.snapshot.items if item.item_id == item_id), None
        )
        if item is None:
            return
        suffix = ".txt" if format_name == "txt" else ".md"
        title_key = (
            TranslationKey.TRANSCRIBE_SAVE_TXT
            if format_name == "txt"
            else TranslationKey.TRANSCRIBE_SAVE_MARKDOWN
        )
        target, _ = QFileDialog.getSaveFileName(
            self, self._t(title_key), f"{Path(item.source_name).stem}{suffix}"
        )
        if not target:
            return
        if format_name == "txt":
            self._controller.save_txt(item_id, Path(target))
        else:
            self._controller.save_markdown(item_id, Path(target))

    def closeEvent(self, event) -> None:
        self.dispose()
        event.accept()

    def dispose(self) -> None:
        if self._locale_connected:
            try:
                self._locale_signal.disconnect(self._locale_callback)
            except (RuntimeError, TypeError):
                pass
            self._locale_connected = False
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def apply_locale(self, _locale: str | None = None) -> None:
        del _locale
        self._title.setText(self._t(TranslationKey.TRANSCRIBE_TITLE))
        self._subtitle.setText(self._t(TranslationKey.TRANSCRIBE_SUBTITLE))
        self._drop_prompt.setText(self._t(TranslationKey.TRANSCRIBE_DROP_PROMPT))
        self._or_label.setText(self._t(TranslationKey.TRANSCRIBE_OR))
        self._choose_button.setText(self._t(TranslationKey.TRANSCRIBE_CHOOSE_FILES))
        self._choose_button.setAccessibleName(
            self._t(TranslationKey.TRANSCRIBE_CHOOSE_FILES_ACCESSIBLE)
        )
        self._formats_label.setText(self._formats_text())
        self._formats_label.setAccessibleName(
            self._t(TranslationKey.TRANSCRIBE_SUPPORTED_FORMATS_ACCESSIBLE)
        )
        for button, text_key, accessible_key in (
            (
                self._add_button,
                TranslationKey.TRANSCRIBE_ADD_FILES,
                TranslationKey.TRANSCRIBE_ADD_FILES_ACCESSIBLE,
            ),
            (
                self._start_button,
                TranslationKey.TRANSCRIBE_START,
                TranslationKey.TRANSCRIBE_START_ACCESSIBLE,
            ),
            (
                self._cancel_button,
                TranslationKey.TRANSCRIBE_CANCEL_ALL,
                TranslationKey.TRANSCRIBE_CANCEL_ALL_ACCESSIBLE,
            ),
            (
                self._clear_button,
                TranslationKey.TRANSCRIBE_CLEAR_FINISHED,
                TranslationKey.TRANSCRIBE_CLEAR_FINISHED_ACCESSIBLE,
            ),
        ):
            button.setText(self._t(text_key))
            button.setAccessibleName(self._t(accessible_key))
        self._render(self._controller.snapshot)

    def _formats_text(self) -> str:
        formats = ", ".join(format.value for format in SUPPORTED_MEDIA_FORMATS)
        return self._t(TranslationKey.TRANSCRIBE_SUPPORTED_FORMATS, formats=formats)

    def _t(self, key: TranslationKey, **values: object) -> str:
        return translate(key, self._locale_config.locale, **values)


def _status_text(state: QueueState, locale: Locale | str | None) -> str:
    return translate(
        {
            QueueState.PENDING: TranslationKey.QUEUE_WAITING,
            QueueState.VALIDATING: TranslationKey.QUEUE_CHECKING_MEDIA,
            QueueState.QUEUED: TranslationKey.QUEUE_QUEUED,
            QueueState.NORMALIZING: TranslationKey.QUEUE_CONVERTING_AUDIO,
            QueueState.TRANSCRIBING: TranslationKey.QUEUE_TRANSCRIBING,
            QueueState.RETRY_WAITING: TranslationKey.QUEUE_RETRYING,
            QueueState.CLEANING_UP: TranslationKey.QUEUE_FINISHING,
            QueueState.SUCCEEDED: TranslationKey.QUEUE_COMPLETED,
            QueueState.FAILED: TranslationKey.QUEUE_FAILED,
            QueueState.CANCELLED: TranslationKey.QUEUE_CANCELLED,
            QueueState.REJECTED: TranslationKey.QUEUE_REJECTED,
        }[state],
        locale,
    )
