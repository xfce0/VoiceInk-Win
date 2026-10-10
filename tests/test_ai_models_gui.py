from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    if os.environ.get("VOICEINK_GUI_TESTS") == "1":
        raise
    pytest.skip("PySide6 is required for presentation tests", allow_module_level=True)

from tests.support.fake_shell import FakeShellBackend
from voiceink_win.application import ShellController
from voiceink_win.domain import ModelMetadata
from voiceink_win.presentation.ai_models_page import AIModelsPage
from voiceink_win.presentation.localization import Locale, LocaleConfig
from voiceink_win.presentation.main_window import MainWindow


@pytest.fixture(scope="module")
def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _metadata(*, available: bool = True) -> ModelMetadata:
    return ModelMetadata(
        name="Parakeet TDT 0.6B V3",
        model_id="parakeet-v3",
        version="model-1",
        backend="cpu",
        trusted=True,
        available=available,
        safe_path="<runtime>/models/parakeet.gguf",
    )


def test_ai_models_page_renders_safe_metadata_and_unavailable_state(
    application: QApplication,
) -> None:
    del application
    page = AIModelsPage(_metadata(available=False))
    try:
        page.show()
        QApplication.processEvents()
        assert page._title.text() == "AI Models"
        assert page._values["model_id"].text() == "parakeet-v3"
        assert page._values["version"].text() == "model-1"
        assert page._values["path"].text() == "<runtime>/models/parakeet.gguf"
        assert page._availability.text() == "Unavailable"
        assert page._unavailable.isVisible()
    finally:
        page.close()


def test_ai_models_navigation_and_locale_are_wired(
    application: QApplication,
) -> None:
    config = LocaleConfig(Locale.RUSSIAN)
    window = MainWindow(
        ShellController(FakeShellBackend()),
        locale_config=config,
        model_metadata=_metadata(),
    )
    try:
        assert window._nav_buttons["AI Models"].isEnabled()
        assert window._ai_models_page._title.text() == "Модели ИИ"
        window._nav_buttons["AI Models"].click()
        application.processEvents()
        assert window._pages.currentWidget() is window._ai_models_page
        assert window._ai_models_page._availability.text() == "Доступна"
    finally:
        window.close()
