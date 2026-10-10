from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    if os.environ.get("VOICEINK_GUI_TESTS") == "1":
        raise
    pytest.skip("PySide6 is required for presentation tests", allow_module_level=True)

from voiceink_win.application import PersistenceService, ShellController
from voiceink_win.domain import Settings
from voiceink_win.infrastructure import SQLitePersistence
from voiceink_win.presentation.localization import Locale, LocaleConfig
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


def test_audio_page_loads_preferences_and_keeps_native_controls_unavailable(
    application: QApplication, persistence
) -> None:
    service, store = persistence
    store.save_settings(
        Settings(
            audio_preferences={
                "input_route": "selected_device",
                "mute_while_recording": True,
                "pause_media_while_recording": True,
                "resume_delay_seconds": 1.5,
                "start_sound": "built_in",
                "stop_sound": "custom",
            }
        )
    ).result(timeout=2)
    window = MainWindow(ShellController.unavailable(), persistence=service)
    window.show()
    try:
        window._select_page("Audio")
        page = window._audio_page
        _wait(application, lambda: not page._loading)
        assert window._pages.currentIndex() == 5
        assert page._input_route_combo.currentData() == "selected_device"
        assert page._mute_while_recording.isChecked()
        assert page._pause_media_while_recording.isChecked()
        assert page._resume_delay.text() == "1.5"
        assert page._start_sound_combo.currentData() == "built_in"
        assert page._stop_sound_combo.currentData() == "custom"
        assert page._device_list.count() == 1
        assert page._selected_device_value.text() == "No input devices are available in this build."
        assert not page._device_list.isEnabled()
        assert not page._input_route_combo.isEnabled()
        assert not page._start_sound_combo.isEnabled()
        assert "native" in page._availability.text().lower()
    finally:
        window.close()
        store.close().result(timeout=2)


def test_audio_page_localizes_unavailable_state(application: QApplication) -> None:
    config = LocaleConfig(Locale.RUSSIAN)
    window = MainWindow(ShellController.unavailable(), locale_config=config)
    try:
        page = window._audio_page
        assert page._title.text() == "Аудио"
        assert page._device_status.text().startswith("Перечень микрофонов")
        assert page._availability.text().startswith("Нативные захват")
        assert page._device_list.item(0).text().endswith("недоступны.")
    finally:
        window.close()
