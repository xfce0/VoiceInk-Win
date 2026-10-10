from __future__ import annotations

import os
from string import Formatter
from threading import Thread

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
from voiceink_win.presentation.dictionary_page import DictionaryPage
from voiceink_win.presentation.localization import (
    CATALOG,
    Locale,
    LocaleConfig,
    TranslationKey,
    error_code_text,
    resolve_locale,
    translate,
)
from voiceink_win.presentation.main_window import MainWindow


@pytest.fixture(scope="module")
def application() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_english_is_the_default_locale_and_preserves_shipped_labels(
    application: QApplication,
) -> None:
    del application
    config = LocaleConfig()
    window = MainWindow(ShellController.unavailable(), locale_config=config)
    try:
        assert config.locale is Locale.ENGLISH
        assert window._nav_buttons["Dashboard"].text() == "Dashboard"
        assert window._nav_buttons["Transcribe"].text() == "Transcribe"
        assert window._state_pill.text() == "Recording unavailable"
        assert window._transcribe_page._choose_button.text() == "Choose Files"
    finally:
        window.close()


def test_russian_locale_translates_shell_and_transcribe_page(
    application: QApplication,
) -> None:
    del application
    config = LocaleConfig(Locale.RUSSIAN)
    window = MainWindow(ShellController.unavailable(), locale_config=config)
    try:
        assert window._nav_buttons["Dashboard"].text() == "Панель"
        assert window._nav_buttons["Transcribe"].text() == "Транскрибация"
        assert window._state_pill.text() == "Запись недоступна"
        assert window._transcribe_page._choose_button.text() == "Выбрать файлы"
        assert window._recorder._record_button.text() == "Недоступно"
    finally:
        window.close()


def test_dictionary_page_translates_header_editor_and_state_actions(
    application: QApplication,
) -> None:
    config = LocaleConfig()
    page = DictionaryPage(None, locale_config=config)
    try:
        assert page._title.text() == "Dictionary"
        assert page._new.text() == "Add rule"
        assert page._save.text() == "Save rule"
        page._show_state("Error", "Storage error", action=True)
        assert page._state_action.text() == "Try again"

        assert config.set_locale(Locale.RUSSIAN)
        application.processEvents()
        assert page._title.text() == "Словарь"
        assert page._new.text() == "Добавить правило"
        assert page._save.text() == "Сохранить правило"
        assert page._state_action.text() == "Повторить"
    finally:
        page.dispose()


def test_unsupported_locale_values_fall_back_to_english(
    application: QApplication,
) -> None:
    del application
    assert resolve_locale("de") is Locale.ENGLISH
    assert translate(TranslationKey.TRANSCRIBE_START, "de") == "Start"
    assert error_code_text("QueueFull", Locale.RUSSIAN) == "Очередь заполнена"
    assert LocaleConfig("de").locale is Locale.ENGLISH


def test_translation_catalog_has_both_locales_and_matching_placeholders() -> None:
    assert set(CATALOG) == set(TranslationKey)
    for key, messages in CATALOG.items():
        assert set(messages) == {Locale.ENGLISH, Locale.RUSSIAN}, key
        english_fields = {
            name
            for _, name, _, _ in Formatter().parse(messages[Locale.ENGLISH])
            if name is not None
        }
        russian_fields = {
            name
            for _, name, _, _ in Formatter().parse(messages[Locale.RUSSIAN])
            if name is not None
        }
        assert russian_fields == english_fields, key


def test_runtime_locale_change_updates_widgets_on_the_qt_event_loop(
    application: QApplication,
) -> None:
    config = LocaleConfig()
    window = MainWindow(ShellController(FakeShellBackend()), locale_config=config)
    try:
        assert config.set_locale(Locale.RUSSIAN)
        application.processEvents()
        assert window._nav_buttons["Dashboard"].text() == "Панель"
        assert window._transcribe_page._add_button.text() == "Добавить файлы"
        assert window._recorder._status.text() == "Готово"

        assert config.set_locale("unsupported")
        application.processEvents()
        assert config.locale is Locale.ENGLISH
        assert window._nav_buttons["Dashboard"].text() == "Dashboard"
        assert window._transcribe_page._add_button.text() == "Add Files"
    finally:
        window.close()


def test_locale_change_requested_from_worker_updates_only_after_gui_processing(
    application: QApplication,
) -> None:
    config = LocaleConfig()
    window = MainWindow(ShellController.unavailable(), locale_config=config)
    try:
        worker = Thread(target=lambda: config.set_locale(Locale.RUSSIAN))
        worker.start()
        worker.join(1)
        assert window._nav_buttons["Dashboard"].text() == "Dashboard"
        application.processEvents()
        assert window._nav_buttons["Dashboard"].text() == "Панель"
    finally:
        window.close()


def test_shell_snapshot_from_worker_is_marshaled_to_the_gui_thread(
    application: QApplication,
) -> None:
    controller = ShellController(FakeShellBackend())
    window = MainWindow(controller)
    try:
        worker = Thread(target=controller.start_recording)
        worker.start()
        worker.join(1)
        assert window._state_pill.text() == "Ready for your voice"
        application.processEvents()
        assert window._state_pill.text() == "Recording in progress"
    finally:
        window.close()
