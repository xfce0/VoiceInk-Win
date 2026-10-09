from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from voiceink_win.domain import (
    DictionaryEntry,
    HistoryRecord,
    HistoryStatus,
    InvalidAudioArtifactPathError,
    Settings,
    TranscriptionSource,
    TranscriptVariant,
)
from voiceink_win.infrastructure import AudioArtifactStore, SQLitePersistence
from voiceink_win.infrastructure.storage_paths import VoiceInkPaths, normalise_relative_audio_path


def _record(record_id: str, created_at: datetime, text: str) -> HistoryRecord:
    return HistoryRecord(
        id=record_id,
        source=TranscriptionSource.MICROPHONE,
        created_at=created_at,
        duration=1.25,
        original_text=text,
        enhanced_text=f"{text} enhanced",
        selected_variant=TranscriptVariant.ENHANCED,
        status=HistoryStatus.COMPLETED,
    )


@pytest.fixture
def store(tmp_path: Path):
    persistence = SQLitePersistence(tmp_path / "voiceink.sqlite3")
    persistence.ready().result(timeout=2)
    yield persistence
    persistence.close().result(timeout=2)


def test_startup_applies_migrations_and_enables_wal(tmp_path: Path) -> None:
    database = tmp_path / "voiceink.sqlite3"
    persistence = SQLitePersistence(database)
    persistence.ready().result(timeout=2)
    persistence.close().result(timeout=2)

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"schema_migrations", "history", "dictionary_entries", "settings"} <= tables
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_settings_roundtrip(store: SQLitePersistence) -> None:
    settings = Settings(
        language="ru",
        selected_mode="meeting",
        hotkeys={"toggle": "Ctrl+Space", "cancel": "Escape"},
        auto_copy=True,
        model_preferences={"model": "parakeet", "device": "cuda"},
        audio_preferences={"sample_rate": 16_000, "channels": 1},
    )

    assert store.get_settings().result(timeout=2) is None
    store.save_settings(settings).result(timeout=2)

    assert store.get_settings().result(timeout=2) == settings


def test_dictionary_crud_and_deterministic_order(store: SQLitePersistence) -> None:
    first = DictionaryEntry(id="first", phrase="zeta", replacement="Z")
    second = DictionaryEntry(id="second", phrase="alpha", replacement="A")
    store.upsert_dictionary(first).result(timeout=2)
    store.upsert_dictionary(second).result(timeout=2)

    assert [entry.id for entry in store.list_dictionary().result(timeout=2)] == [
        "second",
        "first",
    ]

    updated = DictionaryEntry(
        id="first",
        phrase="beta",
        replacement="B",
        created_at=first.created_at,
        updated_at=first.updated_at + timedelta(seconds=1),
    )
    store.upsert_dictionary(updated).result(timeout=2)
    assert store.list_dictionary().result(timeout=2)[1] == updated

    store.delete_dictionary("second").result(timeout=2)
    assert [entry.id for entry in store.list_dictionary().result(timeout=2)] == ["first"]


def test_history_pagination_is_newest_first_and_delete_is_atomic(
    store: SQLitePersistence,
) -> None:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    for record in (
        _record("older", created_at, "old"),
        _record("newest", created_at + timedelta(seconds=2), "new"),
        _record("middle", created_at + timedelta(seconds=1), "mid"),
    ):
        store.upsert_history(record).result(timeout=2)

    first_page = store.list_history(limit=2).result(timeout=2)
    second_page = store.list_history(offset=2, limit=2).result(timeout=2)
    assert [record.id for record in first_page.records] == ["newest", "middle"]
    assert first_page.has_more is True
    assert [record.id for record in second_page.records] == ["older"]
    assert second_page.has_more is False

    store.delete_history("middle").result(timeout=2)
    assert [record.id for record in store.list_history().result(timeout=2).records] == [
        "newest",
        "older",
    ]


def test_concurrent_background_submissions_are_serialized(store: SQLitePersistence) -> None:
    records = [
        _record(f"record-{index}", datetime(2026, 1, 1, tzinfo=UTC), str(index))
        for index in range(40)
    ]
    with ThreadPoolExecutor(max_workers=8) as callers:
        futures = [callers.submit(store.upsert_history, record) for record in records]
    for future in futures:
        future.result(timeout=2).result(timeout=2)

    page = store.list_history(limit=50).result(timeout=2)
    assert {record.id for record in page.records} == {record.id for record in records}


def test_reconciliation_failure_rolls_back_and_closes_cleanly(tmp_path: Path) -> None:
    def broken_reconciliation(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO dictionary_entries "
            "(id, phrase, replacement, created_at, updated_at, enabled) "
            "VALUES ('broken', 'before failure', 'x', 'now', 'now', 1)"
        )
        raise RuntimeError("reconciliation failed")

    persistence = SQLitePersistence(
        tmp_path / "voiceink.sqlite3",
        reconciliation_hooks=(broken_reconciliation,),
    )
    with pytest.raises(RuntimeError, match="reconciliation failed"):
        persistence.ready().result(timeout=2)
    persistence.close().result(timeout=2)

    with sqlite3.connect(tmp_path / "voiceink.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM dictionary_entries").fetchone()[0] == 0


def test_audio_paths_reject_traversal_and_writes_are_atomic(
    tmp_path: Path, store: SQLitePersistence
) -> None:
    paths = VoiceInkPaths.from_root(tmp_path / "AppData" / "Local" / "VoiceInk")
    artifacts = AudioArtifactStore(paths.audio)
    with pytest.raises(InvalidAudioArtifactPathError):
        artifacts.resolve("../outside.wav")
    with pytest.raises(InvalidAudioArtifactPathError):
        normalise_relative_audio_path("C:/outside.wav")
    with pytest.raises(InvalidAudioArtifactPathError):
        artifacts.resolve("nested\\outside.wav")
    with pytest.raises(InvalidAudioArtifactPathError):
        store.upsert_history(HistoryRecord(audio_artifact_path="../outside.wav"))

    target = artifacts.write("history/sample.wav", b"new audio")
    assert target.read_bytes() == b"new audio"
    assert target.parent == paths.audio / "history"
