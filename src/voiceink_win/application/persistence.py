"""Application-facing persistence use cases."""

from __future__ import annotations

from concurrent.futures import Future

from voiceink_win.domain.persistence import (
    DictionaryEntry,
    DictionaryPort,
    HistoryPage,
    HistoryPort,
    HistoryRecord,
    PersistencePort,
    Settings,
    SettingsPort,
)


class PersistenceService(HistoryPort, DictionaryPort, SettingsPort):
    """Keep application code independent from the SQLite adapter."""

    def __init__(self, persistence: PersistencePort) -> None:
        self._persistence = persistence

    def upsert_history(self, record: HistoryRecord) -> Future[None]:
        return self._persistence.upsert_history(record)

    def list_history(self, *, offset: int = 0, limit: int = 50) -> Future[HistoryPage]:
        return self._persistence.list_history(offset=offset, limit=limit)

    def delete_history(self, record_id: str) -> Future[None]:
        return self._persistence.delete_history(record_id)

    def upsert_dictionary(self, entry: DictionaryEntry) -> Future[None]:
        return self._persistence.upsert_dictionary(entry)

    def list_dictionary(self) -> Future[tuple[DictionaryEntry, ...]]:
        return self._persistence.list_dictionary()

    def delete_dictionary(self, entry_id: str) -> Future[None]:
        return self._persistence.delete_dictionary(entry_id)

    def get_settings(self) -> Future[Settings | None]:
        return self._persistence.get_settings()

    def save_settings(self, settings: Settings) -> Future[None]:
        return self._persistence.save_settings(settings)

    def ready(self) -> Future[None]:
        return self._persistence.ready()

    def close(self) -> Future[None]:
        return self._persistence.close()
