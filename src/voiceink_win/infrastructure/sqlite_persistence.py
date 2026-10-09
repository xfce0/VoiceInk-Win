"""SQLite persistence adapter with migrations and serialized background access."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from voiceink_win.domain import TranscriptVariant
from voiceink_win.domain.errors import InvalidInputError
from voiceink_win.domain.persistence import (
    DictionaryEntry,
    HistoryPage,
    HistoryRecord,
    HistoryStatus,
    PersistenceError,
    PersistencePort,
    Settings,
)

from .sqlite_executor import SerializedSQLiteExecutor
from .storage_paths import normalise_relative_audio_path

_MIGRATION_NAME = re.compile(r"^(\d+)_([a-z0-9_]+)\.sql$")
ReconciliationHook = Callable[[sqlite3.Connection], None]


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _validate_page(offset: int, limit: int) -> None:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidInputError("history offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise InvalidInputError("history limit must be between 1 and 500")


class SQLitePersistence(PersistencePort):
    """Asynchronous local store; callers receive futures and never own a DB connection."""

    def __init__(
        self,
        database_path: Path,
        *,
        migration_dir: Path | None = None,
        reconciliation_hooks: Sequence[ReconciliationHook] = (),
        busy_timeout_ms: int = 5_000,
    ) -> None:
        migrations = (
            Path(migration_dir) if migration_dir else Path(__file__).with_name("migrations")
        )
        self._migration_dir = migrations
        self._reconciliation_hooks = tuple(reconciliation_hooks)
        self._executor = SerializedSQLiteExecutor(
            Path(database_path),
            self._startup,
            busy_timeout_ms=busy_timeout_ms,
        )

    def ready(self) -> Future[None]:
        return self._executor.ready()

    def upsert_history(self, record: HistoryRecord) -> Future[None]:
        if record.audio_artifact_path is not None:
            normalise_relative_audio_path(record.audio_artifact_path)

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO history (
                    id, source, created_at, duration, original_text, enhanced_text,
                    selected_variant, status, error, audio_artifact_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source,
                    created_at = excluded.created_at,
                    duration = excluded.duration,
                    original_text = excluded.original_text,
                    enhanced_text = excluded.enhanced_text,
                    selected_variant = excluded.selected_variant,
                    status = excluded.status,
                    error = excluded.error,
                    audio_artifact_path = excluded.audio_artifact_path
                """,
                (
                    record.id,
                    record.source,
                    _timestamp(record.created_at),
                    record.duration,
                    record.original_text,
                    record.enhanced_text,
                    record.selected_variant.value,
                    record.status.value,
                    record.error,
                    record.audio_artifact_path,
                ),
            )

        return self._executor.submit(write, transaction=True)

    def list_history(self, *, offset: int = 0, limit: int = 50) -> Future[HistoryPage]:
        _validate_page(offset, limit)

        def read(connection: sqlite3.Connection) -> HistoryPage:
            rows = connection.execute(
                """
                SELECT id, source, created_at, duration, original_text, enhanced_text,
                       selected_variant, status, error, audio_artifact_path
                FROM history
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (limit + 1, offset),
            ).fetchall()
            records = tuple(_history_from_row(row) for row in rows[:limit])
            return HistoryPage(records, offset, limit, len(rows) > limit)

        return self._executor.submit(read)

    def delete_history(self, record_id: str) -> Future[None]:
        if not isinstance(record_id, str) or not record_id.strip():
            raise InvalidInputError("history ID must not be empty")

        def delete(connection: sqlite3.Connection) -> None:
            connection.execute("DELETE FROM history WHERE id = ?", (record_id,))

        return self._executor.submit(delete, transaction=True)

    def upsert_dictionary(self, entry: DictionaryEntry) -> Future[None]:
        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO dictionary_entries (
                    id, phrase, replacement, created_at, updated_at, enabled
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    phrase = excluded.phrase,
                    replacement = excluded.replacement,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    enabled = excluded.enabled
                """,
                (
                    entry.id,
                    entry.phrase,
                    entry.replacement,
                    _timestamp(entry.created_at),
                    _timestamp(entry.updated_at),
                    int(entry.enabled),
                ),
            )

        return self._executor.submit(write, transaction=True)

    def list_dictionary(self) -> Future[tuple[DictionaryEntry, ...]]:
        def read(connection: sqlite3.Connection) -> tuple[DictionaryEntry, ...]:
            rows = connection.execute(
                """
                SELECT id, phrase, replacement, created_at, updated_at, enabled
                FROM dictionary_entries
                ORDER BY phrase COLLATE NOCASE ASC, id ASC
                """
            ).fetchall()
            return tuple(_dictionary_from_row(row) for row in rows)

        return self._executor.submit(read)

    def delete_dictionary(self, entry_id: str) -> Future[None]:
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise InvalidInputError("dictionary ID must not be empty")

        def delete(connection: sqlite3.Connection) -> None:
            connection.execute("DELETE FROM dictionary_entries WHERE id = ?", (entry_id,))

        return self._executor.submit(delete, transaction=True)

    def get_settings(self) -> Future[Settings | None]:
        def read(connection: sqlite3.Connection) -> Settings | None:
            row = connection.execute(
                """
                SELECT language, selected_mode, hotkeys_json, auto_copy,
                       model_preferences_json, audio_preferences_json
                FROM settings WHERE singleton = 1
                """
            ).fetchone()
            if row is None:
                return None
            return Settings(
                language=row[0],
                selected_mode=row[1],
                hotkeys=_decode_mapping(row[2], "hotkeys"),
                auto_copy=bool(row[3]),
                model_preferences=_decode_mapping(row[4], "model preferences"),
                audio_preferences=_decode_mapping(row[5], "audio preferences"),
            )

        return self._executor.submit(read)

    def save_settings(self, settings: Settings) -> Future[None]:
        payload = (
            settings.language,
            settings.selected_mode,
            json.dumps(settings.hotkeys, sort_keys=True, separators=(",", ":")),
            int(settings.auto_copy),
            json.dumps(settings.model_preferences, sort_keys=True, separators=(",", ":")),
            json.dumps(settings.audio_preferences, sort_keys=True, separators=(",", ":")),
        )

        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO settings (
                    singleton, language, selected_mode, hotkeys_json, auto_copy,
                    model_preferences_json, audio_preferences_json
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    language = excluded.language,
                    selected_mode = excluded.selected_mode,
                    hotkeys_json = excluded.hotkeys_json,
                    auto_copy = excluded.auto_copy,
                    model_preferences_json = excluded.model_preferences_json,
                    audio_preferences_json = excluded.audio_preferences_json
                """,
                payload,
            )

        return self._executor.submit(write, transaction=True)

    def close(self) -> Future[None]:
        return self._executor.close()

    def _startup(self, connection: sqlite3.Connection) -> None:
        _apply_migrations(connection, self._migration_dir)
        for hook in self._reconciliation_hooks:
            connection.execute("BEGIN IMMEDIATE")
            try:
                hook(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise


def _apply_migrations(connection: sqlite3.Connection, migration_dir: Path) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY NOT NULL, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    migrations: list[tuple[int, Path]] = []
    for path in migration_dir.glob("*.sql"):
        match = _MIGRATION_NAME.match(path.name)
        if match is None:
            raise PersistenceError(f"invalid migration filename: {path.name}")
        migrations.append((int(match.group(1)), path))
    versions = [version for version, _ in sorted(migrations)]
    if len(versions) != len(set(versions)):
        raise PersistenceError("duplicate SQLite migration version")
    for version, path in sorted(migrations):
        if version in applied:
            continue
        script = path.read_text(encoding="utf-8")
        transaction = (
            "BEGIN IMMEDIATE;\n"
            + script
            + "\nINSERT INTO schema_migrations(version, applied_at) VALUES ("
            + str(version)
            + ", '"
            + _timestamp(datetime.now(UTC))
            + "');\nCOMMIT;"
        )
        try:
            connection.executescript(transaction)
        except BaseException:
            connection.rollback()
            raise


def _history_from_row(row: sqlite3.Row | tuple[Any, ...]) -> HistoryRecord:
    return HistoryRecord(
        id=row[0],
        source=row[1],
        created_at=_parse_timestamp(row[2]),
        duration=row[3],
        original_text=row[4],
        enhanced_text=row[5],
        selected_variant=TranscriptVariant(row[6]),
        status=HistoryStatus(row[7]),
        error=row[8],
        audio_artifact_path=row[9],
    )


def _dictionary_from_row(row: sqlite3.Row | tuple[Any, ...]) -> DictionaryEntry:
    return DictionaryEntry(
        id=row[0],
        phrase=row[1],
        replacement=row[2],
        created_at=_parse_timestamp(row[3]),
        updated_at=_parse_timestamp(row[4]),
        enabled=bool(row[5]),
    )


def _decode_mapping(value: str, name: str) -> dict[str, object]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise PersistenceError(f"stored {name} are not valid JSON") from error
    if not isinstance(decoded, dict):
        raise PersistenceError(f"stored {name} are not a JSON object")
    return decoded
