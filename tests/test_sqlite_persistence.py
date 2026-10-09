from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from voiceink_win.application import HistoryDeletionService, PersistenceService
from voiceink_win.domain import (
    DictionaryEntry,
    HistoryRecord,
    HistoryStatus,
    InvalidAudioArtifactPathError,
    InvalidInputError,
    PersistenceClosedError,
    PersistenceError,
    Settings,
    TranscriptionSource,
    TranscriptVariant,
)
from voiceink_win.infrastructure import (
    AudioArtifactStore,
    NativeWindowsMediaSecurityAdapter,
    SQLitePersistence,
    WindowsMediaSecurityAdapter,
    sqlite_executor,
)
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


class _TestArtifactSecurityAdapter:
    """Typed adapter for tests that exercise portable artifact semantics only."""

    def validate_source(self, path: Path) -> None:
        del path

    def cleanup_workspace(self, path: Path) -> None:
        del path

    def delete_artifact(self, root: Path, relative_path: str) -> None:
        root.joinpath(*relative_path.split("/")).unlink(missing_ok=True)


def _portable_artifacts(path: Path) -> AudioArtifactStore:
    adapter: WindowsMediaSecurityAdapter | None = _TestArtifactSecurityAdapter()
    return AudioArtifactStore(path, windows_adapter=adapter)


def _native_safety_artifacts(path: Path) -> AudioArtifactStore:
    adapter: WindowsMediaSecurityAdapter | None = (
        NativeWindowsMediaSecurityAdapter() if os.name == "nt" else None
    )
    return AudioArtifactStore(path, windows_adapter=adapter)


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
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2
        assert all(
            len(row[0]) == 64
            for row in connection.execute("SELECT checksum FROM schema_migrations")
        )
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


def test_field_level_settings_update_preserves_complete_hotkey_shape(
    store: SQLitePersistence,
) -> None:
    updated = store.update_settings({"hotkeys.start_stop": "Ctrl+Space"}).result(timeout=2)

    assert updated.hotkeys == {"start_stop": "Ctrl+Space", "cancel": ""}
    assert store.get_settings().result(timeout=2) == updated


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
    second_page = store.list_history(cursor=first_page.next_cursor, limit=2).result(timeout=2)
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
    artifacts = _portable_artifacts(paths.audio)
    with pytest.raises(InvalidAudioArtifactPathError):
        artifacts.resolve("../outside.wav")
    with pytest.raises(InvalidAudioArtifactPathError):
        normalise_relative_audio_path("C:/outside.wav")
    with pytest.raises(InvalidAudioArtifactPathError):
        artifacts.resolve("nested\\outside.wav")
    invalid = store.upsert_history(HistoryRecord(audio_artifact_path="../outside.wav"))
    with pytest.raises(InvalidAudioArtifactPathError):
        invalid.result(timeout=2)

    target = artifacts.write("history/sample.wav", b"new audio")
    assert target.read_bytes() == b"new audio"
    assert target.parent == paths.audio / "history"


def test_migration_checksums_detect_drift_and_missing_package_files(tmp_path: Path) -> None:
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    source_dir = (
        Path(__file__).parents[1] / "src" / "voiceink_win" / "infrastructure" / "migrations"
    )
    for path in source_dir.glob("*.sql"):
        (migration_dir / path.name).write_bytes(path.read_bytes())

    database = tmp_path / "drift.sqlite3"
    first = SQLitePersistence(database, migration_dir=migration_dir)
    first.ready().result(timeout=2)
    first.close().result(timeout=2)
    (migration_dir / "001_initial.sql").write_text(
        (migration_dir / "001_initial.sql").read_text(encoding="utf-8") + "\n-- drift\n",
        encoding="utf-8",
    )
    drifted = SQLitePersistence(database, migration_dir=migration_dir)
    with pytest.raises(PersistenceError, match="drift"):
        drifted.ready().result(timeout=2)
    drifted.close().result(timeout=2)

    missing = SQLitePersistence(tmp_path / "missing.sqlite3", migration_dir=tmp_path / "absent")
    with pytest.raises(PersistenceError, match="migrations are missing"):
        missing.ready().result(timeout=2)
    missing.close().result(timeout=2)


def test_null_migration_checksum_fails_closed_without_rebaselining(tmp_path: Path) -> None:
    database = tmp_path / "null-checksum.sqlite3"
    first = SQLitePersistence(database)
    first.ready().result(timeout=2)
    first.close().result(timeout=2)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE schema_migrations SET checksum = NULL WHERE version = 1")
        connection.commit()

    reopened = SQLitePersistence(database)
    with pytest.raises(PersistenceError, match="checksum is NULL"):
        reopened.ready().result(timeout=2)
    reopened.close().result(timeout=2)


def test_history_search_escapes_like_metacharacters(store: SQLitePersistence) -> None:
    store.upsert_history(_record("literal", datetime.now(UTC), "100%_literal")).result(timeout=2)
    store.upsert_history(_record("wildcard", datetime.now(UTC), "100Xyliteral")).result(timeout=2)

    page = store.list_history(search="100%_").result(timeout=2)

    assert [record.id for record in page.records] == ["literal"]


def test_pending_history_deletion_reconciles_after_failure_and_restart(tmp_path: Path) -> None:
    database = tmp_path / "reconcile.sqlite3"
    artifact_root = tmp_path / "audio"
    artifacts = _portable_artifacts(artifact_root)
    artifacts.write("history/item.wav", b"audio")
    sqlite = SQLitePersistence(database)
    sqlite.ready().result(timeout=2)
    sqlite.upsert_history(
        HistoryRecord(
            id="pending",
            source=TranscriptionSource.IMPORTED_FILE,
            original_text="pending",
            audio_artifact_path="history/item.wav",
        )
    ).result(timeout=2)
    sqlite.mark_history_deleting("pending").result(timeout=2)
    sqlite.close().result(timeout=2)

    reopened = SQLitePersistence(database)

    def fail_cleanup(path: str | None) -> None:
        raise OSError("temporary cleanup failure")

    service = HistoryDeletionService(PersistenceService(reopened), fail_cleanup)
    with pytest.raises(RuntimeError, match="pending history deletions failed"):
        service.start().result(timeout=2)
    assert [
        item.record_id for item in reopened.list_pending_history_deletions().result(timeout=2)
    ] == ["pending"]
    service.close()
    reopened.close().result(timeout=2)

    restarted = SQLitePersistence(database)
    restarted.ready().result(timeout=2)
    recovery = HistoryDeletionService(PersistenceService(restarted), artifacts.delete)
    recovery.start().result(timeout=2)
    recovery.close()
    assert restarted.list_history().result(timeout=2).records == ()
    assert not (artifact_root / "history" / "item.wav").exists()
    restarted.close().result(timeout=2)


def test_audio_artifact_delete_rejects_symlinks_and_never_follows_outside_root(
    tmp_path: Path,
) -> None:
    artifacts = _native_safety_artifacts(tmp_path / "audio")
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"outside")
    link = artifacts.resolve("linked.wav")
    link.symlink_to(outside)

    with pytest.raises(InvalidAudioArtifactPathError):
        artifacts.delete("linked.wav")

    assert outside.read_bytes() == b"outside"
    assert link.is_symlink()


def test_migration_failure_rolls_back_the_failed_migration(tmp_path: Path) -> None:
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    (migration_dir / "001_base.sql").write_text(
        "CREATE TABLE base (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )
    (migration_dir / "002_broken.sql").write_text(
        "CREATE TABLE transient (id INTEGER PRIMARY KEY);\nINVALID SQL;",
        encoding="utf-8",
    )
    persistence = SQLitePersistence(tmp_path / "rollback.sqlite3", migration_dir=migration_dir)
    with pytest.raises(PersistenceError, match="002_broken.sql"):
        persistence.ready().result(timeout=2)
    persistence.close().result(timeout=2)

    with sqlite3.connect(tmp_path / "rollback.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'transient'"
            ).fetchone()
            is None
        )
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]


def test_dictionary_uniqueness_uses_unicode_canonical_key_but_keeps_display_phrase(
    store: SQLitePersistence,
) -> None:
    first = DictionaryEntry(id="first", phrase="Café", replacement="one")
    store.upsert_dictionary(first).result(timeout=2)
    duplicate = store.upsert_dictionary(
        DictionaryEntry(id="second", phrase="CAFE\u0301", replacement="two")
    )
    with pytest.raises(sqlite3.IntegrityError):
        duplicate.result(timeout=2)
    assert store.list_dictionary().result(timeout=2)[0].phrase == "Café"


def test_persistence_validation_completes_future_with_error(store: SQLitePersistence) -> None:
    invalid_page = store.list_history(limit=0)
    with pytest.raises(InvalidInputError):
        invalid_page.result(timeout=2)


def test_sqlite_worker_completes_future_when_rollback_also_fails(
    tmp_path: Path, monkeypatch
) -> None:
    class BrokenConnection:
        in_transaction = True

        def execute(self, statement: str):
            if statement == "BEGIN IMMEDIATE":
                raise RuntimeError("operation failed")
            return self

        def rollback(self) -> None:
            raise RuntimeError("rollback failed")

        def close(self) -> None:
            return None

    connection = BrokenConnection()
    monkeypatch.setattr(sqlite_executor.sqlite3, "connect", lambda *args, **kwargs: connection)
    executor = sqlite_executor.SerializedSQLiteExecutor(tmp_path / "worker.sqlite3", lambda _: None)
    executor.ready().result(timeout=2)

    failed = executor.submit(lambda _: None, transaction=True)

    with pytest.raises(RuntimeError, match="operation failed"):
        failed.result(timeout=2)
    executor.close().result(timeout=2)


def test_history_tombstone_survives_database_failure_until_finalized(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tombstone.sqlite3"
    persistence = SQLitePersistence(database)
    persistence.ready().result(timeout=2)
    persistence.upsert_history(_record("tombstone", datetime.now(UTC), "keep")).result(timeout=2)
    persistence.mark_history_deleting("tombstone").result(timeout=2)
    persistence.close().result(timeout=2)

    failed_finalize = persistence.finalize_history_deletion("tombstone")
    with pytest.raises(PersistenceClosedError):
        failed_finalize.result(timeout=2)

    reopened = SQLitePersistence(database)
    reopened.ready().result(timeout=2)
    assert reopened.list_history().result(timeout=2).records == ()
    assert [
        deletion.record_id
        for deletion in reopened.list_pending_history_deletions().result(timeout=2)
    ] == ["tombstone"]
    reopened.finalize_history_deletion("tombstone").result(timeout=2)
    assert reopened.list_history().result(timeout=2).records == ()
    reopened.close().result(timeout=2)
