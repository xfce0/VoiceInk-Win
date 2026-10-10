"""SQLite persistence adapter with migrations and serialized background access."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from voiceink_win.domain import TranscriptVariant, canonical_dictionary_key
from voiceink_win.domain.errors import InvalidInputError
from voiceink_win.domain.persistence import (
    DictionaryEntry,
    HistoryPage,
    HistoryRecord,
    HistoryStatus,
    PendingHistoryDeletion,
    PersistenceError,
    PersistencePort,
    Settings,
)

from .sqlite_executor import SerializedSQLiteExecutor
from .storage_paths import normalise_relative_audio_path

_MIGRATION_NAME = re.compile(r"^(\d+)_([a-z0-9_]+)\.sql$")
REQUIRED_MIGRATIONS = (
    "001_initial.sql",
    "002_persistence_hardening.sql",
    "003_theme_preference.sql",
)
EXPECTED_TABLES = {"schema_migrations", "history", "dictionary_entries", "settings"}
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


def _cursor_value(created_at: str, record_id: str) -> str:
    payload = json.dumps((created_at, record_id), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise InvalidInputError("history cursor is invalid") from error
    if (
        not isinstance(value, list | tuple)
        or len(value) != 2
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise InvalidInputError("history cursor is invalid")
    return value[0], value[1]


class SQLitePersistence(PersistencePort):
    """Asynchronous local store; callers never own a DB connection."""

    def __init__(
        self,
        database_path: Path,
        *,
        migration_dir: Path | None = None,
        reconciliation_hooks: Sequence[ReconciliationHook] = (),
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._migration_dir = (
            Path(migration_dir) if migration_dir else Path(__file__).with_name("migrations")
        )
        self._require_package_contract = migration_dir is None
        self._reconciliation_hooks = tuple(reconciliation_hooks)
        self._executor = SerializedSQLiteExecutor(
            Path(database_path), self._startup, busy_timeout_ms=busy_timeout_ms
        )

    def ready(self) -> Future[None]:
        return self._executor.ready()

    def upsert_history(self, record: HistoryRecord) -> Future[None]:
        def write(connection: sqlite3.Connection) -> None:
            if record.audio_artifact_path is not None:
                normalise_relative_audio_path(record.audio_artifact_path)
            connection.execute(
                """
                INSERT INTO history (
                    id, source, created_at, duration, original_text, enhanced_text,
                    selected_variant, status, error, failure_code, source_metadata_json,
                    audio_artifact_path, deletion_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source,
                    created_at = excluded.created_at,
                    duration = excluded.duration,
                    original_text = excluded.original_text,
                    enhanced_text = excluded.enhanced_text,
                    selected_variant = excluded.selected_variant,
                    status = excluded.status,
                    error = excluded.error,
                    failure_code = excluded.failure_code,
                    source_metadata_json = excluded.source_metadata_json,
                    audio_artifact_path = excluded.audio_artifact_path,
                    deletion_state = 'active'
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
                    record.failure_code,
                    json.dumps(record.source_metadata, sort_keys=True, separators=(",", ":")),
                    record.audio_artifact_path,
                ),
            )

        return self._executor.submit(write, transaction=True)

    def list_history(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        search: str | None = None,
        offset: int = 0,
    ) -> Future[HistoryPage]:
        def read(connection: sqlite3.Connection) -> HistoryPage:
            _validate_page(offset, limit)
            if cursor is not None and not isinstance(cursor, str):
                raise InvalidInputError("history cursor must be text or None")
            if search is not None and not isinstance(search, str):
                raise InvalidInputError("history search must be text or None")
            clauses: list[str] = []
            parameters: list[object] = []
            clauses.append("deletion_state = 'active'")
            if cursor is not None:
                created_at, record_id = _decode_cursor(cursor)
                clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
                parameters.extend((created_at, created_at, record_id))
            if search and search.strip():
                pattern = _like_pattern(search.strip())
                clauses.append(
                    "(source LIKE ? ESCAPE '\\' OR original_text LIKE ? ESCAPE '\\' "
                    "OR enhanced_text LIKE ? ESCAPE '\\' "
                    "OR source_metadata_json LIKE ? ESCAPE '\\')"
                )
                parameters.extend((pattern, pattern, pattern, pattern))
            rows = connection.execute(
                f"""
                SELECT id, source, created_at, duration, original_text, enhanced_text,
                       selected_variant, status, error, failure_code,
                       source_metadata_json, audio_artifact_path
                FROM history
                {"WHERE " + " AND ".join(clauses) if clauses else ""}
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit + 1, offset if cursor is None else 0),
            ).fetchall()
            records = tuple(_history_from_row(row) for row in rows[:limit])
            next_cursor = None
            if len(rows) > limit and records:
                last = records[-1]
                next_cursor = _cursor_value(_timestamp(last.created_at), last.id)
            return HistoryPage(records, offset, limit, len(rows) > limit, next_cursor)

        return self._executor.submit(read)

    def list_pending_history_deletions(self) -> Future[tuple[PendingHistoryDeletion, ...]]:
        def read(connection: sqlite3.Connection) -> tuple[PendingHistoryDeletion, ...]:
            rows = connection.execute(
                "SELECT id, audio_artifact_path FROM history "
                "WHERE deletion_state = 'pending' ORDER BY created_at ASC, id ASC"
            ).fetchall()
            return tuple(PendingHistoryDeletion(row[0], row[1]) for row in rows)

        return self._executor.submit(read)

    def delete_history(self, record_id: str) -> Future[None]:
        def delete(connection: sqlite3.Connection) -> None:
            _require_record_id(record_id)
            connection.execute("DELETE FROM history WHERE id = ?", (record_id,))

        return self._executor.submit(delete, transaction=True)

    def mark_history_deleting(self, record_id: str) -> Future[None]:
        def mark(connection: sqlite3.Connection) -> None:
            _require_record_id(record_id)
            connection.execute(
                "UPDATE history SET deletion_state = 'pending' WHERE id = ?", (record_id,)
            )

        return self._executor.submit(mark, transaction=True)

    def finalize_history_deletion(self, record_id: str) -> Future[None]:
        def finalize(connection: sqlite3.Connection) -> None:
            _require_record_id(record_id)
            connection.execute(
                "DELETE FROM history WHERE id = ? AND deletion_state = 'pending'",
                (record_id,),
            )

        return self._executor.submit(finalize, transaction=True)

    def update_history_variant(
        self, record_id: str, selected_variant: TranscriptVariant
    ) -> Future[None]:
        def update(connection: sqlite3.Connection) -> None:
            _require_record_id(record_id)
            if not isinstance(selected_variant, TranscriptVariant):
                raise InvalidInputError("selected variant must be a TranscriptVariant")
            connection.execute(
                "UPDATE history SET selected_variant = ? WHERE id = ?",
                (selected_variant.value, record_id),
            )

        return self._executor.submit(update, transaction=True)

    def upsert_dictionary(self, entry: DictionaryEntry) -> Future[None]:
        def write(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO dictionary_entries (
                    id, phrase, canonical_key, replacement, created_at, updated_at, enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    phrase = excluded.phrase,
                    canonical_key = excluded.canonical_key,
                    replacement = excluded.replacement,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    enabled = excluded.enabled
                """,
                (
                    entry.id,
                    entry.phrase,
                    canonical_dictionary_key(entry.phrase),
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
        def delete(connection: sqlite3.Connection) -> None:
            if not isinstance(entry_id, str) or not entry_id.strip():
                raise InvalidInputError("dictionary ID must not be empty")
            connection.execute("DELETE FROM dictionary_entries WHERE id = ?", (entry_id,))

        return self._executor.submit(delete, transaction=True)

    def get_settings(self) -> Future[Settings | None]:
        def read(connection: sqlite3.Connection) -> Settings | None:
            row = connection.execute(
                """
                SELECT language, selected_mode, hotkeys_json, auto_copy, theme_mode,
                       model_preferences_json, audio_preferences_json
                FROM settings WHERE singleton = 1
                """
            ).fetchone()
            return _settings_from_row(row) if row is not None else None

        return self._executor.submit(read)

    def save_settings(self, settings: Settings) -> Future[None]:
        def write(connection: sqlite3.Connection) -> None:
            _write_settings(connection, settings)

        return self._executor.submit(write, transaction=True)

    def update_settings(self, changes: Mapping[str, object]) -> Future[Settings]:
        def update(connection: sqlite3.Connection) -> Settings:
            if not isinstance(changes, Mapping) or not changes:
                raise InvalidInputError("settings changes must not be empty")
            allowed = {
                "language",
                "selected_mode",
                "theme_mode",
                "auto_copy",
                "hotkeys.start_stop",
                "hotkeys.cancel",
            }
            unknown = set(changes) - allowed
            if unknown:
                raise InvalidInputError(
                    f"unsupported settings fields: {', '.join(sorted(unknown))}"
                )
            row = connection.execute(
                "SELECT language, selected_mode, hotkeys_json, auto_copy, theme_mode, "
                "model_preferences_json, audio_preferences_json FROM settings WHERE singleton = 1"
            ).fetchone()
            current = _settings_from_row(row) if row is not None else Settings()
            hotkeys = {"start_stop": "", "cancel": ""}
            hotkeys.update(current.hotkeys)
            values: dict[str, object] = {
                "language": current.language,
                "selected_mode": current.selected_mode,
                "theme_mode": current.theme_mode,
                "hotkeys": hotkeys,
                "auto_copy": current.auto_copy,
                "model_preferences": current.model_preferences,
                "audio_preferences": current.audio_preferences,
            }
            for key, value in changes.items():
                if key.startswith("hotkeys."):
                    hotkeys[key.removeprefix("hotkeys.")] = value
                else:
                    values[key] = value
            updated = Settings(**values)
            _write_settings(connection, updated)
            return updated

        return self._executor.submit(update, transaction=True)

    def close(self) -> Future[None]:
        return self._executor.close()

    def _startup(self, connection: sqlite3.Connection) -> None:
        _apply_migrations(
            connection,
            self._migration_dir,
            required=REQUIRED_MIGRATIONS if self._require_package_contract else (),
        )
        if self._require_package_contract:
            _validate_schema_contract(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        try:
            _hydrate_dictionary_keys(connection)
            for hook in self._reconciliation_hooks:
                hook(connection)
            connection.commit()
        except BaseException as error:
            _rollback_safely(connection, error)
            raise


def _require_record_id(record_id: str) -> None:
    if not isinstance(record_id, str) or not record_id.strip():
        raise InvalidInputError("history ID must not be empty")


def _apply_migrations(
    connection: sqlite3.Connection, migration_dir: Path, *, required: Sequence[str] = ()
) -> None:
    if not migration_dir.is_dir():
        raise PersistenceError(f"SQLite migrations are missing: {migration_dir}")
    missing = [name for name in required if not (migration_dir / name).is_file()]
    if missing:
        raise PersistenceError("SQLite migrations are missing: " + ", ".join(missing))
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY NOT NULL, applied_at TEXT NOT NULL, checksum TEXT)"
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(schema_migrations)")}
    if "checksum" not in columns:
        connection.execute("ALTER TABLE schema_migrations ADD COLUMN checksum TEXT")
    applied = {
        row[0]: row[1]
        for row in connection.execute("SELECT version, checksum FROM schema_migrations")
    }
    migrations: list[tuple[int, Path]] = []
    for path in migration_dir.glob("*.sql"):
        match = _MIGRATION_NAME.match(path.name)
        if match is None:
            raise PersistenceError(f"invalid migration filename: {path.name}")
        migrations.append((int(match.group(1)), path))
    migrations.sort()
    versions = [version for version, _ in migrations]
    if not migrations:
        raise PersistenceError(f"SQLite migrations are missing: {migration_dir}")
    if len(versions) != len(set(versions)):
        raise PersistenceError("duplicate SQLite migration version")
    available = dict(migrations)
    unknown = sorted(version for version in applied if version not in available)
    if unknown:
        raise PersistenceError(
            "SQLite migration files are missing for applied versions: "
            + ", ".join(map(str, unknown))
        )
    for version, path in migrations:
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        if version in applied:
            if applied[version] is None:
                raise PersistenceError(
                    f"SQLite migration checksum is NULL for {path.name}; re-baselining is forbidden"
                )
            if applied[version] != checksum:
                raise PersistenceError(f"SQLite migration drift detected for {path.name}")
            continue
        script = path.read_text(encoding="utf-8")
        transaction = (
            "BEGIN IMMEDIATE;\n"
            + script
            + "\nINSERT INTO schema_migrations(version, applied_at, checksum) VALUES ("
            + str(version)
            + ", '"
            + _timestamp(datetime.now(UTC))
            + "', '"
            + checksum
            + "');\nCOMMIT;"
        )
        try:
            connection.executescript(transaction)
        except BaseException as error:
            _rollback_safely(connection, error)
            raise PersistenceError(f"SQLite migration {path.name} failed: {error}") from error


def _rollback_safely(connection: sqlite3.Connection, error: BaseException) -> None:
    try:
        connection.rollback()
    except BaseException as rollback_error:
        error.add_note(f"SQLite rollback failed during startup: {rollback_error}")


def _validate_schema_contract(connection: sqlite3.Connection) -> None:
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = EXPECTED_TABLES - tables
    if missing:
        raise PersistenceError(
            "SQLite schema contract is missing tables: " + ", ".join(sorted(missing))
        )
    expected_versions = {int(name.split("_", 1)[0]) for name in REQUIRED_MIGRATIONS}
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    if applied != expected_versions:
        raise PersistenceError("SQLite schema contract has an incomplete migration set")


def _hydrate_dictionary_keys(connection: sqlite3.Connection) -> None:
    for entry_id, phrase in connection.execute("SELECT id, phrase FROM dictionary_entries"):
        connection.execute(
            "UPDATE dictionary_entries SET canonical_key = ? WHERE id = ?",
            (canonical_dictionary_key(phrase), entry_id),
        )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS dictionary_canonical_key_idx "
        "ON dictionary_entries(canonical_key)"
    )


def _write_settings(connection: sqlite3.Connection, settings: Settings) -> None:
    connection.execute(
        """
        INSERT INTO settings (
            singleton, language, selected_mode, hotkeys_json, auto_copy,
            theme_mode, model_preferences_json, audio_preferences_json
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(singleton) DO UPDATE SET
            language = excluded.language,
            selected_mode = excluded.selected_mode,
            hotkeys_json = excluded.hotkeys_json,
            auto_copy = excluded.auto_copy,
            theme_mode = excluded.theme_mode,
            model_preferences_json = excluded.model_preferences_json,
            audio_preferences_json = excluded.audio_preferences_json
        """,
        (
            settings.language,
            settings.selected_mode,
            json.dumps(settings.hotkeys, sort_keys=True, separators=(",", ":")),
            int(settings.auto_copy),
            settings.theme_mode.value,
            json.dumps(settings.model_preferences, sort_keys=True, separators=(",", ":")),
            json.dumps(settings.audio_preferences, sort_keys=True, separators=(",", ":")),
        ),
    )


def _settings_from_row(row: sqlite3.Row | tuple[Any, ...]) -> Settings:
    return Settings(
        language=row[0],
        selected_mode=row[1],
        hotkeys=_decode_mapping(row[2], "hotkeys"),
        auto_copy=bool(row[3]),
        theme_mode=row[4],
        model_preferences=_decode_mapping(row[5], "model preferences"),
        audio_preferences=_decode_mapping(row[6], "audio preferences"),
    )


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
        failure_code=row[9],
        source_metadata=_decode_mapping(row[10], "history source metadata"),
        audio_artifact_path=row[11],
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


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
