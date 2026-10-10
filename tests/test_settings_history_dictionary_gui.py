from __future__ import annotations

import os
import time
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QMessageBox
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
    TranscriptionSource,
)
from voiceink_win.infrastructure import SQLitePersistence
from voiceink_win.presentation.dictionary_page import DictionaryPage
from voiceink_win.presentation.history_page import HistoryPage as HistoryWidget
from voiceink_win.presentation.main_window import MainWindow


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
        for label, index in (("Modes", 2), ("History", 3), ("Dictionary", 4), ("Settings", 5)):
            window._nav_buttons[label].click()
            assert window._pages.currentIndex() == index
            assert window._nav_buttons[label].isChecked()
        _wait(application, lambda: not window._settings_page._loading)
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
        page._phrase.setText("Voice Ink")
        page._replacement.setText("VoiceInk")
        page._save.click()
        _wait(application, lambda: page._list.count() == 1)
        entry = store.list_dictionary().result(timeout=2)[0]
        assert entry.replacement == "VoiceInk"
        row = page._row_widgets[entry.id]
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
