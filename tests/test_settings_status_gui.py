from __future__ import annotations

import os
import time
from concurrent.futures import Future

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QLabel
except ImportError:
    if os.environ.get("VOICEINK_GUI_TESTS") == "1":
        raise
    pytest.skip("PySide6 is required for presentation tests", allow_module_level=True)

from voiceink_win.application import PersistenceService
from voiceink_win.domain import Settings
from voiceink_win.presentation.localization import Locale, LocaleConfig
from voiceink_win.presentation.settings_page import SettingsPage


@pytest.fixture(scope="module")
def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait(application: QApplication, predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    application.processEvents()
    assert predicate()


class _DeferredSettingsPersistence:
    def __init__(self) -> None:
        self.load_future: Future[Settings] = Future()
        self.update_futures: list[Future[Settings]] = []

    def get_settings(self) -> Future[Settings]:
        return self.load_future

    def update_settings(self, changes: dict[str, object]) -> Future[Settings]:
        del changes
        future: Future[Settings] = Future()
        self.update_futures.append(future)
        return future


@pytest.mark.parametrize(
    ("locale", "capability_text"),
    ((Locale.ENGLISH, "Not available."), (Locale.RUSSIAN, "Недоступно.")),
)
def test_loaded_settings_have_no_idle_feedback_or_page_banner(
    application: QApplication, locale: Locale, capability_text: str
) -> None:
    config = LocaleConfig(locale)
    persistence = _DeferredSettingsPersistence()
    page = SettingsPage(PersistenceService(persistence), locale_config=config)
    page.show()
    persistence.load_future.set_result(Settings(language=locale.value))
    try:
        _wait(application, lambda: not page._loading)
        assert not page.findChildren(QLabel, "pageUnavailable")
        assert page._status.text() == ""
        assert page._status.isHidden()
        assert page._error.text() == ""
        assert page._error.isHidden()
        assert page._model_value.text() == capability_text
        assert page._audio_value.text() == capability_text
        assert page._language_combo.isEnabled()
    finally:
        page.dispose()
        page.close()


@pytest.mark.parametrize(
    ("locale", "loading_text", "unavailable_text"),
    (
        (Locale.ENGLISH, "Loading...", "Local storage is unavailable."),
        (Locale.RUSSIAN, "Загрузка...", "Локальное хранилище недоступно."),
    ),
)
def test_loading_and_failed_load_keep_feedback_inline(
    application: QApplication,
    locale: Locale,
    loading_text: str,
    unavailable_text: str,
) -> None:
    config = LocaleConfig(locale)
    persistence = _DeferredSettingsPersistence()
    page = SettingsPage(PersistenceService(persistence), locale_config=config)
    page.show()
    try:
        assert page._status.text() == loading_text
        assert not page._status.isHidden()
        assert not page._language_combo.isEnabled()
        assert not page.findChildren(QLabel, "pageUnavailable")

        persistence.load_future.set_exception(RuntimeError("storage unavailable"))
        _wait(application, lambda: page._status.text() == unavailable_text)
        assert not page._status.isHidden()
        assert page._error.text() == ""
        assert page._language_combo.isEnabled() is False
        assert "Ready" not in page._status.text()
        assert "Готово" not in page._status.text()
        assert "Saved" not in page._status.text()
        assert "Сохранено" not in page._status.text()
    finally:
        page.dispose()
        page.close()


@pytest.mark.parametrize(
    ("locale", "saving_text", "saved_text", "error_text"),
    (
        (
            Locale.ENGLISH,
            "Saving...",
            "Saved",
            "Could not save settings. Try again.",
        ),
        (
            Locale.RUSSIAN,
            "Сохранение...",
            "Сохранено",
            "Не удалось сохранить настройки. Попробуйте ещё раз.",
        ),
    ),
)
def test_save_feedback_remains_actionable_in_both_locales(
    application: QApplication,
    locale: Locale,
    saving_text: str,
    saved_text: str,
    error_text: str,
) -> None:
    config = LocaleConfig(locale)
    persistence = _DeferredSettingsPersistence()
    page = SettingsPage(PersistenceService(persistence), locale_config=config)
    page.show()
    persistence.load_future.set_result(Settings(language=locale.value))
    try:
        _wait(application, lambda: not page._loading)
        page._auto_copy.setChecked(True)
        assert page._status.text() == saving_text
        assert page._error.isHidden()
        persistence.update_futures[-1].set_result(Settings(language=locale.value, auto_copy=True))
        _wait(application, lambda: page._status.text() == saved_text)
        assert page._error.isHidden()
        assert "Ready" not in page._status.text()
        assert "Готово" not in page._status.text()

        page._auto_copy.setChecked(False)
        assert page._status.text() == saving_text
        persistence.update_futures[-1].set_exception(RuntimeError("write failed"))
        _wait(application, lambda: page._error.text() == error_text)
        assert page._status.text() == ("Error" if locale is Locale.ENGLISH else "Ошибка")
        assert not page._error.isHidden()
        assert not page.findChildren(QLabel, "pageUnavailable")
    finally:
        page.dispose()
        page.close()


def test_locale_change_repaints_active_feedback_and_capabilities(application: QApplication) -> None:
    config = LocaleConfig(Locale.ENGLISH)
    persistence = _DeferredSettingsPersistence()
    page = SettingsPage(PersistenceService(persistence), locale_config=config)
    page.show()
    persistence.load_future.set_result(Settings(language="en"))
    try:
        _wait(application, lambda: not page._loading)
        assert page._model_value.text() == "Not available."
        page._auto_copy.setChecked(True)
        assert page._status.text() == "Saving..."
        assert config.set_locale(Locale.RUSSIAN)
        application.processEvents()
        assert page._status.text() == "Сохранение..."
        assert page._model_value.text() == "Недоступно."
        assert config.set_locale(Locale.ENGLISH)
        application.processEvents()
        assert page._status.text() == "Saving..."
        persistence.update_futures[-1].set_exception(RuntimeError("write failed"))
        _wait(application, lambda: page._error.text() == "Could not save settings. Try again.")
    finally:
        page.dispose()
        page.close()
