"""Application-facing persistence use cases."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor

from voiceink_win.domain.persistence import (
    DictionaryEntry,
    DictionaryPort,
    HistoryPage,
    HistoryPort,
    HistoryRecord,
    PendingHistoryDeletion,
    PersistencePort,
    Settings,
    SettingsPort,
)

logger = logging.getLogger(__name__)


class PersistenceService(HistoryPort, DictionaryPort, SettingsPort):
    """Keep application code independent from the SQLite adapter."""

    def __init__(self, persistence: PersistencePort) -> None:
        self._persistence = persistence

    def upsert_history(self, record: HistoryRecord) -> Future[None]:
        return self._persistence.upsert_history(record)

    def list_history(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        search: str | None = None,
        offset: int = 0,
    ) -> Future[HistoryPage]:
        try:
            return self._persistence.list_history(
                cursor=cursor, limit=limit, search=search, offset=offset
            )
        except TypeError:
            return self._persistence.list_history(offset=offset, limit=limit)

    def delete_history(self, record_id: str) -> Future[None]:
        return self._persistence.delete_history(record_id)

    def mark_history_deleting(self, record_id: str) -> Future[None]:
        return self._persistence.mark_history_deleting(record_id)

    def finalize_history_deletion(self, record_id: str) -> Future[None]:
        return self._persistence.finalize_history_deletion(record_id)

    def update_history_variant(self, record_id: str, selected_variant) -> Future[None]:
        return self._persistence.update_history_variant(record_id, selected_variant)

    def list_pending_history_deletions(self) -> Future[tuple[PendingHistoryDeletion, ...]]:
        return self._persistence.list_pending_history_deletions()

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

    def update_settings(self, changes: Mapping[str, object]) -> Future[Settings]:
        return self._persistence.update_settings(changes)

    def ready(self) -> Future[None]:
        return self._persistence.ready()

    def close(self) -> Future[None]:
        return self._persistence.close()


class HistoryDeletionService:
    """Own history tombstone cleanup independently from any presentation page."""

    def __init__(self, persistence: PersistenceService, artifact_cleanup=None) -> None:
        self._persistence = persistence
        self._artifact_cleanup = artifact_cleanup
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="history-artifact")
        self._closed = False

    def start(self) -> Future[None]:
        future = self._executor.submit(self._reconcile_pending)
        future.add_done_callback(self._report_reconciliation_failure)
        return future

    def delete(self, record_id: str, artifact_path: str | None) -> Future[None]:
        if self._closed:
            future: Future[None] = Future()
            future.set_exception(RuntimeError("history deletion service is closed"))
            return future
        return self._executor.submit(self._delete, record_id, artifact_path)

    def close(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _delete(self, record_id: str, artifact_path: str | None) -> None:
        self._persistence.mark_history_deleting(record_id).result()
        self._cleanup_artifact(artifact_path)
        self._persistence.finalize_history_deletion(record_id).result()

    def _reconcile_pending(self) -> None:
        self._persistence.ready().result()
        pending = self._persistence.list_pending_history_deletions().result()
        failures: list[BaseException] = []
        for deletion in pending:
            try:
                self._cleanup_artifact(deletion.audio_artifact_path)
                self._persistence.finalize_history_deletion(deletion.record_id).result()
            except BaseException as error:
                failures.append(error)
                logger.exception(
                    "pending history deletion remains for retry",
                    extra={"record_id": deletion.record_id},
                )
        if failures:
            raise RuntimeError("one or more pending history deletions failed") from failures[0]

    def _cleanup_artifact(self, artifact_path: str | None) -> None:
        if artifact_path and self._artifact_cleanup is not None:
            self._artifact_cleanup(artifact_path)

    @staticmethod
    def _report_reconciliation_failure(future: Future[None]) -> None:
        try:
            future.result()
        except BaseException:
            logger.exception("history deletion reconciliation failed")
