"""Qt page for safe Parakeet runtime metadata."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
from typing import Protocol

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from voiceink_win.domain import ModelMetadata

from .async_tools import FutureBridge
from .localization import LocaleConfig, TranslationKey, translate


class ModelDownloadPort(Protocol):
    @property
    def has_file(self) -> bool: ...

    def download(self) -> Future: ...

    def delete(self) -> None: ...


class AIModelsPage(QWidget):
    """Show trusted model metadata and manage the pinned model artifact."""

    def __init__(
        self,
        metadata: ModelMetadata,
        parent: QWidget | None = None,
        locale_config: LocaleConfig | None = None,
        model_manager: ModelDownloadPort | None = None,
        on_model_changed: Callable[[], object] | None = None,
        on_model_delete: Callable[[], object] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("aiModelsPage")
        self._metadata = metadata
        self._model_manager = model_manager
        self._on_model_changed = on_model_changed
        self._on_model_delete = on_model_delete
        self._locale_config = locale_config or LocaleConfig(parent=self)
        self._locale_callback = self.apply_locale
        self._locale_config.locale_changed.connect(
            self._locale_callback, Qt.ConnectionType.AutoConnection
        )
        self._disposed = False
        self._bridge = FutureBridge(self)
        self._download_pending = False
        self._build_ui()
        self.apply_locale()

    def _build_ui(self) -> None:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(scroll)
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

        card = QFrame(content)
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 16, 18, 16)
        card_layout.setSpacing(12)
        self._model_heading = QLabel(card)
        self._model_heading.setObjectName("sectionTitle")
        card_layout.addWidget(self._model_heading)
        self._availability = QLabel(card)
        self._availability.setObjectName("statePill")
        self._availability.setTextFormat(Qt.TextFormat.PlainText)
        card_layout.addWidget(self._availability, 0, Qt.AlignmentFlag.AlignLeft)

        form = QFormLayout()
        form.setHorizontalSpacing(24)
        self._values: dict[str, QLabel] = {}
        for field, _key in (
            ("name", TranslationKey.AI_MODEL_NAME),
            ("version", TranslationKey.AI_MODEL_VERSION),
            ("model_id", TranslationKey.AI_MODEL_ID),
            ("backend", TranslationKey.AI_MODEL_BACKEND),
            ("trusted", TranslationKey.AI_MODEL_TRUSTED),
            ("path", TranslationKey.AI_MODEL_PATH),
        ):
            label = QLabel(card)
            label.setObjectName("metadata")
            value = QLabel(card)
            value.setObjectName("muted")
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setWordWrap(True)
            self._values[field] = value
            form.addRow(label, value)
            setattr(self, f"_{field}_label", label)
        card_layout.addLayout(form)
        root.addWidget(card)

        actions = QHBoxLayout()
        self._download_button = QPushButton(content)
        self._download_button.clicked.connect(self._download_model)
        actions.addWidget(self._download_button)
        self._delete_button = QPushButton(content)
        self._delete_button.clicked.connect(self._delete_model)
        actions.addWidget(self._delete_button)
        actions.addStretch(1)
        root.addLayout(actions)
        self._action_status = QLabel(content)
        self._action_status.setObjectName("metadata")
        self._action_status.setWordWrap(True)
        root.addWidget(self._action_status)

        self._unavailable = QLabel(content)
        self._unavailable.setObjectName("pageError")
        self._unavailable.setWordWrap(True)
        root.addWidget(self._unavailable)
        root.addStretch(1)
        scroll.setWidget(content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

    def apply_locale(self, _locale: str | None = None) -> None:
        del _locale
        self._title.setText(self._t(TranslationKey.AI_MODELS_TITLE))
        self._subtitle.setText(self._t(TranslationKey.AI_MODELS_SUBTITLE))
        self._model_heading.setText(self._metadata.name)
        for field, key in (
            ("name", TranslationKey.AI_MODEL_NAME),
            ("version", TranslationKey.AI_MODEL_VERSION),
            ("model_id", TranslationKey.AI_MODEL_ID),
            ("backend", TranslationKey.AI_MODEL_BACKEND),
            ("trusted", TranslationKey.AI_MODEL_TRUSTED),
            ("path", TranslationKey.AI_MODEL_PATH),
        ):
            getattr(self, f"_{field}_label").setText(self._t(key))
        self._values["name"].setText(self._metadata.name)
        self._values["version"].setText(self._metadata.version)
        self._values["model_id"].setText(self._metadata.model_id)
        self._values["backend"].setText(self._metadata.backend)
        self._values["trusted"].setText(
            self._t(
                TranslationKey.AI_MODEL_STATE_TRUSTED
                if self._metadata.trusted
                else TranslationKey.AI_MODEL_STATE_UNTRUSTED
            )
        )
        self._values["path"].setText(self._metadata.safe_path)
        self._availability.setText(
            self._t(
                TranslationKey.AI_MODEL_STATE_AVAILABLE
                if self._metadata.available
                else TranslationKey.AI_MODEL_STATE_UNAVAILABLE
            )
        )
        self._availability.setProperty("role", "ready" if self._metadata.available else "error")
        self._availability.style().unpolish(self._availability)
        self._availability.style().polish(self._availability)
        self._unavailable.setText(
            "" if self._metadata.available else self._t(TranslationKey.AI_MODEL_UNAVAILABLE)
        )
        self._unavailable.setVisible(not self._metadata.available)
        self._download_button.setText(self._t(TranslationKey.AI_MODEL_DOWNLOAD))
        self._delete_button.setText(self._t(TranslationKey.AI_MODEL_DELETE))
        self._sync_actions()

    def _sync_actions(self) -> None:
        available = self._model_manager is not None and self._model_manager.has_file
        enabled = not self._download_pending
        self._download_button.setVisible(self._model_manager is not None and not available)
        self._delete_button.setVisible(self._model_manager is not None and available)
        self._download_button.setEnabled(enabled)
        self._delete_button.setEnabled(enabled)

    def _download_model(self) -> None:
        if self._model_manager is None or self._download_pending:
            return
        self._download_pending = True
        self._action_status.setText(self._t(TranslationKey.AI_MODEL_DOWNLOADING))
        self._sync_actions()
        try:
            future = self._model_manager.download()
        except BaseException as error:
            self._download_finished(None, error)
            return
        self._bridge.watch(future, self._download_finished)

    def _download_finished(self, _result, error: BaseException | None) -> None:
        if self._disposed:
            return
        self._download_pending = False
        if error is not None:
            self._action_status.setText(
                f"{self._t(TranslationKey.AI_MODEL_DOWNLOAD_ERROR)}: {error}"
            )
            self._sync_actions()
            return
        if self._on_model_changed is None:
            self._model_ready(True, None)
            return
        try:
            outcome = self._on_model_changed()
        except BaseException as reload_error:
            self._model_ready(None, reload_error)
            return
        if hasattr(outcome, "add_done_callback"):
            self._bridge.watch(outcome, self._model_ready)
        else:
            self._model_ready(outcome is True, None)

    def _model_ready(self, result, error: BaseException | None) -> None:
        if self._disposed:
            return
        if error is not None or result is not True:
            self._metadata = replace(
                self._metadata,
                trusted=False,
                available=False,
                safe_path="<downloaded>/models/parakeet.gguf",
            )
            self._action_status.setText(self._t(TranslationKey.AI_MODEL_RUNTIME_ERROR))
        else:
            self._metadata = replace(
                self._metadata,
                trusted=True,
                available=True,
                safe_path="<downloaded>/models/parakeet.gguf",
            )
            self._action_status.setText(self._t(TranslationKey.AI_MODEL_DOWNLOADED))
        self.apply_locale()

    def _delete_model(self) -> None:
        if self._model_manager is None or self._download_pending:
            return
        if self._on_model_delete is None:
            self._delete_after_unload(True, None)
            return
        self._download_pending = True
        self._action_status.setText(self._t(TranslationKey.AI_MODEL_DELETING))
        self._sync_actions()
        try:
            outcome = self._on_model_delete()
        except BaseException as error:
            self._delete_after_unload(None, error)
            return
        if hasattr(outcome, "add_done_callback"):
            self._bridge.watch(outcome, self._delete_after_unload)
        else:
            self._delete_after_unload(outcome, None)

    def _delete_after_unload(self, result, error: BaseException | None) -> None:
        if self._disposed:
            return
        if error is not None or result is not True:
            self._download_pending = False
            self._action_status.setText(self._t(TranslationKey.AI_MODEL_RUNTIME_ERROR))
            self._sync_actions()
            return
        try:
            self._model_manager.delete()
        except BaseException as delete_error:
            self._download_pending = False
            self._action_status.setText(
                f"{self._t(TranslationKey.AI_MODEL_DOWNLOAD_ERROR)}: {delete_error}"
            )
            self._sync_actions()
            return
        self._download_pending = False
        self._metadata = replace(
            self._metadata,
            trusted=False,
            available=False,
            safe_path="<downloaded>/models/parakeet.gguf",
        )
        self._action_status.setText(self._t(TranslationKey.AI_MODEL_DELETED))
        self.apply_locale()

    def _t(self, key: TranslationKey, **values: object) -> str:
        return translate(key, self._locale_config.locale, **values)

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        try:
            self._locale_config.locale_changed.disconnect(self._locale_callback)
        except (RuntimeError, TypeError):
            pass


__all__ = ["AIModelsPage"]
