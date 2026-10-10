"""Qt page for safe Parakeet runtime metadata."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFormLayout, QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget

from voiceink_win.domain import ModelMetadata

from .localization import LocaleConfig, TranslationKey, translate


class AIModelsPage(QWidget):
    """Show installed model metadata without exposing filesystem details."""

    def __init__(
        self,
        metadata: ModelMetadata,
        parent: QWidget | None = None,
        locale_config: LocaleConfig | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("aiModelsPage")
        self._metadata = metadata
        self._locale_config = locale_config or LocaleConfig(parent=self)
        self._locale_callback = self.apply_locale
        self._locale_config.locale_changed.connect(
            self._locale_callback, Qt.ConnectionType.AutoConnection
        )
        self._disposed = False
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
