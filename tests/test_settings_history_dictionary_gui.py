from __future__ import annotations

import os
import time
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton
except ImportError:
    if os.environ.get("VOICEINK_GUI_TESTS") == "1":
        raise
    pytest.skip("PySide6 is required for presentation tests", allow_module_level=True)

from voiceink_win.application import PersistenceService, ShellController
from voiceink_win.domain import (
    DictionaryEntry,
    HistoryPage,
    HistoryRecord,
    HistoryStatus,
    Settings,
    ThemePreference,
    TranscriptionSource,
)
from voiceink_win.infrastructure import SQLitePersistence
from voiceink_win.presentation.dictionary_page import DictionaryPage
from voiceink_win.presentation.history_page import HistoryPage as HistoryWidget
from voiceink_win.presentation.localization import Locale, LocaleConfig
from voiceink_win.presentation.main_window import MainWindow
from voiceink_win.presentation.theme import DARK_THEME, LIGHT_THEME, ThemeMode


@pytest.fixture(scope="module")
def application() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def persistence(tmp_path: Path):
    store = SQLitePersistence(tmp_path / "voiceink.sqlite3")
    store.ready().result(timeout=2)
    service = PersistenceService(store)
    yield service, store
    store.close().result(timeout=2)


def _wait(application: QApplication, predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    application.processEvents()
    assert predicate()


def _window(application: QApplication, service: PersistenceService) -> MainWindow:
    window = MainWindow(ShellController.unavailable(), persistence=service)
    window.show()
    application.processEvents()
    return window


def test_navigation_reaches_all_persisted_pages(application: QApplication, persistence) -> None:
    service, store = persistence
    window = _window(application, service)
    try:
        for label, index in (
            ("Modes", 2),
            ("History", 3),
            ("Dictionary", 4),
            ("AI Models", 5),
            ("Audio", 6),
            ("Settings", 7),
        ):
            window._nav_buttons[label].click()
            assert window._pages.currentIndex() == index
            assert window._nav_buttons[label].isChecked()
        _wait(application, lambda: not window._settings_page._loading)
    finally:
        window.close()
        store.close().result(timeout=2)


def test_unavailable_persistence_uses_neutral_page_states(application: QApplication) -> None:
    window = MainWindow(ShellController.unavailable())
    window.show()
    application.processEvents()
    try:
        settings = window._settings_page
        history = window._history_page
        dictionary = window._dictionary_page

        assert not settings._availability.isHidden()
        assert settings._availability.text() == "Local storage is unavailable."
        assert settings._model_value.text() == "Not available."
        assert settings._audio_value.text() == "Not available."
        assert settings._error.text() == ""
        assert not settings._language_combo.isEnabled()
        assert not settings._theme_combo.isEnabled()

        assert not history._availability.isHidden()
        assert history._availability.text() == "Local storage is unavailable."
        assert history._error.text() == ""
        assert not history._delete.isVisibleTo(history)
        assert not history.findChildren(QPushButton, "historyAction")

        assert not dictionary._state_panel.isHidden()
        assert dictionary._state_title.text() == "Could not complete the operation."
        assert dictionary._state_detail.text() == "Local storage is unavailable."
        assert dictionary._error.text() == "Local storage is unavailable."
        assert settings._error.objectName() == "inlineError"
        assert history._error.objectName() == "inlineError"
        assert dictionary._error.objectName() == "pageError"
    finally:
        window.close()


def test_settings_capability_placeholder_stays_inline_when_storage_is_available(
    application: QApplication, persistence
) -> None:
    service, store = persistence
    window = _window(application, service)
    try:
        window._select_page("Settings")
        page = window._settings_page
        _wait(application, lambda: not page._loading)
        assert page._availability.isHidden()
        assert page._model_value.text() == "Not available."
        assert page._audio_value.text() == "Not available."
        assert page._language_combo.isEnabled()
    finally:
        window.close()
        store.close().result(timeout=2)


def test_settings_roundtrip_reloads_language_mode_and_hotkeys(
    application: QApplication, persistence
) -> None:
    service, store = persistence
    window = _window(application, service)
    try:
        window._select_page("Settings")
        page = window._settings_page
        _wait(application, lambda: not page._loading)
        page._auto_copy.setChecked(True)
        page._mode_combo.setCurrentIndex(page._mode_combo.findData("meeting"))
        page._start_hotkey.setText("Ctrl+Space")
        page._start_hotkey.editingFinished.emit()
        page._language_combo.setCurrentIndex(page._language_combo.findData("ru"))
        _wait(application, lambda: page._status.text() in {"Saved", "Сохранено"})
        assert store.get_settings().result(timeout=2) == Settings(
            language="ru",
            selected_mode="meeting",
            hotkeys={"start_stop": "Ctrl+Space", "cancel": ""},
            auto_copy=True,
        )
    finally:
        window.close()
        store.close().result(timeout=2)


def test_settings_theme_selector_persists_and_refreshes_runtime_theme(
    application: QApplication, persistence
) -> None:
    service, store = persistence
    window = _window(application, service)
    try:
        window._select_page("Settings")
        page = window._settings_page
        _wait(application, lambda: not page._loading)

        page._theme_combo.setCurrentIndex(page._theme_combo.findData("dark"))
        _wait(application, lambda: page._status.text() in {"Saved", "Сохранено"})
        assert window.theme_preference is ThemeMode.DARK
        assert window._theme is DARK_THEME
        assert store.get_settings().result(timeout=2).theme_mode is ThemePreference.DARK

        window.apply_system_theme(ThemeMode.LIGHT)
        assert window._theme is DARK_THEME

        page._theme_combo.setCurrentIndex(page._theme_combo.findData("system"))
        _wait(application, lambda: page._status.text() in {"Saved", "Сохранено"})
        window.apply_system_theme(ThemeMode.DARK)
        assert window.theme_preference is ThemeMode.SYSTEM
        assert window._theme is DARK_THEME
        window.apply_system_theme(ThemeMode.LIGHT)
        assert window._theme is LIGHT_THEME
    finally:
        window.close()
        store.close().result(timeout=2)


def test_history_load_select_copy_delete_and_cleanup(
    application: QApplication, persistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store = persistence
    record = HistoryRecord(
        id="history-1",
        source=TranscriptionSource.IMPORTED_FILE,
        created_at=datetime.now(UTC),
        original_text="A stored transcript",
        status=HistoryStatus.COMPLETED,
        audio_artifact_path="history/history-1.wav",
    )
    store.upsert_history(record).result(timeout=2)
    cleaned: list[str] = []
    window = MainWindow(
        ShellController.unavailable(),
        persistence=service,
        artifact_cleanup=cleaned.append,
    )
    window.show()
    try:
        window._select_page("History")
        page = window._history_page
        _wait(application, lambda: page._list.count() == 1)
        page._list.setCurrentRow(0)
        assert page._rows[record.id].width() == page._list.viewport().width()
        assert page._text.toPlainText() == "A stored transcript"
        page._copy.click()
        _wait(application, lambda: page._status.text() in {"Copied", "Скопировано"})
        assert QApplication.clipboard().text() == "A stored transcript"
        monkeypatch.setattr(
            QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes
        )
        page._delete_selected()
        _wait(application, lambda: page._list.count() == 0 and cleaned == ["history/history-1.wav"])
        assert store.list_history().result(timeout=2).records == ()
    finally:
        window.close()
        store.close().result(timeout=2)


def test_history_page_keeps_newest_first_order_across_load_pagination_and_search(
    application: QApplication, persistence
) -> None:
    service, store = persistence
    created_at = datetime(2026, 1, 1, tzinfo=UTC)

    def record(record_id: str, timestamp: datetime, original: str, enhanced: str | None = None):
        return HistoryRecord(
            id=record_id,
            source=TranscriptionSource.IMPORTED_FILE,
            created_at=timestamp,
            original_text=original,
            enhanced_text=enhanced,
            status=HistoryStatus.COMPLETED,
        )

    for item in (
        record("old", created_at - timedelta(seconds=1), "older"),
        record("equal-a", created_at, "equal a", "needle equal a"),
        record("equal-b", created_at, "equal b", "needle equal b"),
        record("middle", created_at + timedelta(seconds=1), "middle", "needle middle"),
        record("newest", created_at + timedelta(seconds=2), "needle newest"),
    ):
        store.upsert_history(item).result(timeout=2)

    def visible_ids(page: HistoryWidget) -> list[str]:
        return [
            page._list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(page._list.count())
        ]

    page = HistoryWidget(service)
    page._limit = 2
    page.refresh()
    try:
        _wait(application, lambda: visible_ids(page) == ["newest", "middle"])
        page._next.click()
        _wait(application, lambda: visible_ids(page) == ["equal-b", "equal-a"])
        page._previous.click()
        _wait(application, lambda: visible_ids(page) == ["newest", "middle"])

        page._search.setText("needle")
        page._search_button.click()
        _wait(application, lambda: visible_ids(page) == ["newest", "middle"])
        page._next.click()
        _wait(application, lambda: visible_ids(page) == ["equal-b", "equal-a"])
    finally:
        page.dispose()


def test_dictionary_crud_is_async_and_validates_phrase(
    application: QApplication, persistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, store = persistence
    window = _window(application, service)
    try:
        window._select_page("Dictionary")
        page = window._dictionary_page
        _wait(application, lambda: page._status.text() != "Loading...")
        assert page._state_title.text() == "No replacement rules yet."
        page._save.click()
        assert page._error.text() == "Enter a phrase."
        assert page._error.objectName() == "pageError"
        page._phrase.setText("Voice Ink")
        page._replacement.setText("VoiceInk")
        page._save.click()
        _wait(application, lambda: page._list.count() == 1)
        entry = store.list_dictionary().result(timeout=2)[0]
        assert entry.replacement == "VoiceInk"
        row = page._row_widgets[entry.id]
        assert row.width() <= page._list.viewport().width()
        assert row._edit.text() == "Edit"
        assert row._delete.text() == "Delete"
        row._edit.click()
        assert page._phrase.hasFocus()
        page._list.setCurrentRow(0)
        page._replacement.setText("VoiceInk Win")
        page._save.click()
        _wait(application, lambda: page._entries and page._entries[0].replacement == "VoiceInk Win")
        assert page._delete.isEnabled()
        monkeypatch.setattr(
            QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes
        )
        page._row_widgets[entry.id]._delete.click()
        _wait(application, lambda: page._list.count() == 0)
    finally:
        window.close()
        store.close().result(timeout=2)


class _DeferredPersistence:
    def __init__(self) -> None:
        self.history_future: Future[HistoryPage] = Future()

    def ready(self) -> Future[None]:
        future: Future[None] = Future()
        future.set_result(None)
        return future

    def list_history(self, *, offset: int = 0, limit: int = 50) -> Future[HistoryPage]:
        del offset, limit
        return self.history_future


class _DeferredDictionaryPersistence:
    def __init__(self) -> None:
        self.dictionary_future: Future[tuple[DictionaryEntry, ...]] = Future()

    def list_dictionary(self) -> Future[tuple[DictionaryEntry, ...]]:
        return self.dictionary_future


def test_dictionary_page_renders_loading_and_error_states(application: QApplication) -> None:
    deferred = _DeferredDictionaryPersistence()
    page = DictionaryPage(PersistenceService(deferred))  # type: ignore[arg-type]
    try:
        application.processEvents()
        assert page._status.text() == "Loading..."
        assert not page._state_panel.isHidden()
        assert page._list.isHidden()
        deferred.dictionary_future.set_exception(RuntimeError("storage unavailable"))
        _wait(application, lambda: page._state_title.text() == "Could not complete the operation.")
        assert not page._state_action.isHidden()
        assert page._error.text() == "Local storage is unavailable."
    finally:
        page.dispose()


def test_history_page_does_not_wait_for_persistence_future(application: QApplication) -> None:
    deferred = _DeferredPersistence()
    page = HistoryWidget(PersistenceService(deferred))  # type: ignore[arg-type]
    started = time.monotonic()
    application.processEvents()
    assert time.monotonic() - started < 0.1
    assert page._status.text() == "Loading..."
    deferred.history_future.set_result(HistoryPage((), 0, 20, False))
    _wait(application, lambda: page._status.text() == "Ready")
    page.dispose()


def test_history_rows_preview_expand_copy_and_disable_unavailable_actions(
    application: QApplication, persistence
) -> None:
    service, store = persistence
    record = HistoryRecord(
        id="history-preview",
        created_at=datetime.now(UTC),
        original_text="First line\nSecond line\nThird line\nFourth line",
        source_metadata={"source_name": "meeting.wav"},
    )
    store.upsert_history(record).result(timeout=2)
    page = HistoryWidget(service)
    try:
        _wait(application, lambda: page._list.count() == 1)
        row = page._rows[record.id]
        assert row._preview.text() == "First line\nSecond line\n..."
        assert not row.audio_button.isEnabled()
        assert not row.folder_button.isEnabled()

        page._list.setCurrentRow(0)
        assert row.expanded
        assert row._full_text.text() == record.original_text
        page._copy.click()
        _wait(application, lambda: page._status.text() in {"Copied", "Скопировано"})
        assert QApplication.clipboard().text() == record.original_text
    finally:
        page.dispose()
        store.close().result(timeout=2)


def test_history_audio_and_folder_actions_use_injected_ports(
    application: QApplication, persistence
) -> None:
    service, store = persistence
    record = HistoryRecord(
        id="history-actions",
        created_at=datetime.now(UTC),
        original_text="Transcript",
        source_metadata={"folder_reference": "folder-token"},
        audio_artifact_path="history/history-actions.wav",
    )
    store.upsert_history(record).result(timeout=2)

    class AudioPort:
        def __init__(self) -> None:
            self.references: list[str] = []

        def play(self, artifact_reference: str) -> None:
            self.references.append(artifact_reference)

    class FolderPort:
        def __init__(self) -> None:
            self.references: list[str] = []

        def reveal(self, folder_reference: str) -> None:
            self.references.append(folder_reference)

    audio = AudioPort()
    folder = FolderPort()
    page = HistoryWidget(service, audio_port=audio, folder_port=folder)
    try:
        _wait(application, lambda: page._list.count() == 1)
        row = page._rows[record.id]
        assert row.audio_button.isEnabled()
        assert row.folder_button.isEnabled()
        row.audio_button.click()
        row.folder_button.click()
        assert audio.references == [record.audio_artifact_path]
        assert folder.references == ["folder-token"]
    finally:
        page.dispose()
        store.close().result(timeout=2)


def test_history_actions_stay_in_their_row_at_fixed_width_and_follow_locale(
    application: QApplication, persistence
) -> None:
    service, store = persistence
    record = HistoryRecord(
        id="history-action-layout",
        created_at=datetime.now(UTC),
        original_text="First line\nSecond line\nThird line",
        source_metadata={"source_name": "meeting.wav"},
    )
    store.upsert_history(record).result(timeout=2)
    page = HistoryWidget(service, locale_config=LocaleConfig(Locale.RUSSIAN))
    page.resize(592, 520)
    page.show()
    try:
        assert not page._delete.isVisibleTo(page)
        _wait(application, lambda: page._list.count() == 1)
        row = page._rows[record.id]
        action_buttons = row.findChildren(QPushButton, "historyAction")

        assert len(action_buttons) == 6
        assert all(button.parentWidget() is row for button in action_buttons)
        assert all(button.isVisibleTo(page) for button in action_buttons)

        page._list.setCurrentRow(0)
        application.processEvents()
        assert page._delete is row.delete_button
        assert page._delete.isVisibleTo(page)
        assert row._full_text.isVisibleTo(page)
        assert page._list.horizontalScrollBar().isVisible() is False
        assert all(row.rect().contains(button.geometry()) for button in action_buttons)
        assert row.delete_button.text() == "Удалить"
        assert row.copy_button.text() == "Копировать"
    finally:
        page.dispose()
        store.close().result(timeout=2)
