"""Application controller for the imported-media transcription page."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock, Thread, current_thread
from typing import Protocol
from uuid import uuid4

from voiceink_win.domain import (
    Cancelled,
    EndOfStream,
    ErrorCode,
    Failed,
    ImportFailure,
    ImportObservation,
    ImportOptions,
    JobId,
    MediaFormat,
    OutputState,
    OutputStatus,
    ProgressSnapshot,
    QueueAggregate,
    QueueState,
    RejectedRequestError,
    Stage,
    Success,
    TerminalResult,
    TranscribeAvailability,
    TranscribePageSnapshot,
    TranscriptDocument,
    TranscriptionQueueItemSnapshot,
    TranscriptVariant,
    media_format_hint,
    safe_basename,
)

from .transcribe_output import ClipboardPort, TextFilePort, serialize_markdown, serialize_txt

logger = logging.getLogger(__name__)


class ObservationSubscription(Protocol):
    def next(self, timeout: float | None = None): ...

    def close(self) -> None: ...


class ImportedMediaPort(Protocol):
    def submit(self, path: str, options: ImportOptions | None = None) -> JobId: ...

    def observe(self, job_id: JobId) -> ObservationSubscription: ...

    def wait(self, job_id: JobId, timeout: float | None = None) -> TerminalResult: ...

    def cancel(self, job_id: JobId) -> bool: ...


@dataclass(slots=True)
class _Item:
    item_id: str
    path: Path
    source_name: str
    format_hint: MediaFormat | None
    state: QueueState = QueueState.PENDING
    job_id: JobId | None = None
    attempt: int = 1
    progress: ProgressSnapshot = ProgressSnapshot(Stage.ACCEPTED)
    result: TranscriptDocument | None = None
    failure: ImportFailure | None = None
    subscription: ObservationSubscription | None = None


class TranscribePageController:
    """Own page state while delegating all media work to the imported service."""

    def __init__(
        self,
        imported_media: ImportedMediaPort | None,
        *,
        clipboard: ClipboardPort | None = None,
        text_files: TextFilePort | None = None,
        unavailable_message: str = "Imported media transcription is unavailable in this build.",
        availability: TranscribeAvailability | None = None,
    ) -> None:
        self._imported_media = imported_media
        self._clipboard = clipboard
        self._text_files = text_files
        self._availability = availability or (
            TranscribeAvailability.AVAILABLE
            if imported_media is not None
            else TranscribeAvailability.UNAVAILABLE
        )
        self._items: list[_Item] = []
        self._listeners: list[Callable[[TranscribePageSnapshot], None]] = []
        self._lock = RLock()
        self._closed = False
        self._admission_thread: Thread | None = None
        self._watchers: set[Thread] = set()
        self._cancel_threads: set[Thread] = set()
        self._output_threads: set[Thread] = set()
        self._snapshot = TranscribePageSnapshot(
            accepting_files=(
                imported_media is not None
                and self._availability is TranscribeAvailability.AVAILABLE
            ),
            availability=self._availability,
            page_error=(
                None
                if self._availability is TranscribeAvailability.AVAILABLE
                else unavailable_message
            ),
        )

    @property
    def snapshot(self) -> TranscribePageSnapshot:
        with self._lock:
            return self._snapshot

    def subscribe(self, listener: Callable[[TranscribePageSnapshot], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)
            snapshot = self._snapshot
        listener(snapshot)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def set_output_ports(
        self,
        *,
        clipboard: ClipboardPort | None,
        text_files: TextFilePort | None,
    ) -> None:
        with self._lock:
            self._clipboard = clipboard
            self._text_files = text_files

    def attach_backend(self, imported_media: ImportedMediaPort) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._imported_media = imported_media
            self._availability = TranscribeAvailability.AVAILABLE
            self._snapshot = replace(
                self._snapshot,
                accepting_files=True,
                availability=self._availability,
                page_error=None,
            )
            self._publish_locked()
            return True

    def mark_unavailable(self, message: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._imported_media = None
            self._availability = TranscribeAvailability.UNAVAILABLE
            self._snapshot = replace(
                self._snapshot,
                accepting_files=False,
                availability=self._availability,
                page_error=message,
            )
            self._publish_locked()

    def add_paths(self, paths: list[str | Path]) -> None:
        with self._lock:
            if self._closed or self._imported_media is None:
                return
            existing = {item.path.absolute() for item in self._items if item.state not in _TERMINAL}
            for raw_path in paths:
                path = Path(raw_path)
                absolute = path.absolute()
                if absolute in existing:
                    continue
                existing.add(absolute)
                self._items.append(
                    _Item(
                        str(uuid4()), absolute, safe_basename(absolute), media_format_hint(absolute)
                    )
                )
            self._publish_locked()

    def remove_pending(self, item_id: str) -> bool:
        with self._lock:
            index = self._index(item_id)
            if index is None or self._items[index].state not in {
                QueueState.PENDING,
                QueueState.REJECTED,
            }:
                return False
            self._items.pop(index)
            self._publish_locked()
            return True

    def start_queue(self) -> bool:
        with self._lock:
            if self._closed or self._imported_media is None or self._admission_active_locked():
                return False
            ids = tuple(item.item_id for item in self._items if item.state is QueueState.PENDING)
            if not ids:
                return False
            self._admission_thread = Thread(
                target=self._admit_items,
                args=(ids,),
                name="transcribe-page-admission",
                daemon=True,
            )
            self._admission_thread.start()
            self._publish_locked()
            return True

    def cancel_item(self, item_id: str) -> bool:
        with self._lock:
            item = self._item(item_id)
            if item is None or item.state not in _ACTIVE:
                return False
            job_id = item.job_id
            media = self._imported_media
        if media is None or job_id is None:
            return False
        thread = Thread(
            target=self._cancel_job,
            args=(item_id, job_id, media),
            name="transcribe-page-cancel",
            daemon=True,
        )
        with self._lock:
            self._cancel_threads.add(thread)
        thread.start()
        return True

    def cancel_all(self) -> None:
        with self._lock:
            ids = tuple(
                (item.item_id, item.job_id)
                for item in self._items
                if item.state in _ACTIVE and item.job_id is not None
            )
            media = self._imported_media
        if media is None:
            return
        for item_id, job_id in ids:
            self._start_cancel(item_id, job_id, media)

    def retry_item(self, item_id: str) -> bool:
        with self._lock:
            item = self._item(item_id)
            if (
                item is None
                or item.state not in {QueueState.FAILED, QueueState.CANCELLED, QueueState.REJECTED}
                or self._imported_media is None
            ):
                return False
            item.state = QueueState.PENDING
            item.job_id = None
            item.progress = ProgressSnapshot(Stage.ACCEPTED)
            item.result = None
            item.failure = None
            self._publish_locked()
        with self._lock:
            current_admission = self._admission_thread
            if current_admission is not None and current_admission.is_alive():
                target = self._retry_after_admission
                args = (item_id, current_admission)
            else:
                target = self._admit_items
                args = ((item_id,),)
            self._admission_thread = Thread(
                target=target,
                args=args,
                name="transcribe-page-retry",
                daemon=True,
            )
            self._admission_thread.start()
        return True

    def clear_terminal_items(self) -> None:
        with self._lock:
            self._items = [item for item in self._items if item.state not in _TERMINAL]
            self._publish_locked()

    def select_variant(self, item_id: str, variant: TranscriptVariant) -> bool:
        with self._lock:
            item = self._item(item_id)
            if item is None or item.result is None:
                return False
            item.result = replace(item.result, selected_variant=variant)
            if (
                item.result.selected_variant is TranscriptVariant.ENHANCED
                and not item.result.has_enhanced
            ):
                item.result = replace(item.result, selected_variant=TranscriptVariant.ORIGINAL)
            self._publish_locked()
            return True

    def copy(self, item_id: str, variant: TranscriptVariant | None = None) -> bool:
        with self._lock:
            item = self._item(item_id)
            document = item.result if item else None
            clipboard = self._clipboard
        if document is None or clipboard is None:
            self._set_output_status(OutputState.FAILED, "Clipboard is unavailable.")
            return False
        text = document.text_for(variant or document.selected_variant)
        self._set_output_status(OutputState.COPYING, "Copying transcript...")

        def copy_text() -> None:
            def complete(error: BaseException | None) -> None:
                if error is not None:
                    logger.error(
                        "clipboard completion failed",
                        extra={"item_id": item_id, "error": str(error)},
                    )
                    self._set_output_status(OutputState.FAILED, "Could not copy the transcript.")
                else:
                    self._set_output_status(OutputState.SUCCEEDED, "Transcript copied.")

            try:
                clipboard.copy(text, complete)
            except Exception:
                logger.exception("clipboard worker failed", extra={"item_id": item_id})
                self._set_output_status(OutputState.FAILED, "Could not copy the transcript.")
            finally:
                with self._lock:
                    self._output_threads.discard(current_thread())

        thread = Thread(target=copy_text, name="transcribe-page-clipboard", daemon=True)
        with self._lock:
            self._output_threads.add(thread)
        thread.start()
        return True

    def save_txt(
        self, item_id: str, target: Path, variant: TranscriptVariant | None = None
    ) -> bool:
        return self._save(item_id, target, variant, serialize_txt)

    def save_markdown(
        self, item_id: str, target: Path, variant: TranscriptVariant | None = None
    ) -> bool:
        return self._save(item_id, target, variant, serialize_markdown)

    def close(self, timeout: float = 5.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            admission = self._admission_thread
            subscriptions = tuple(item.subscription for item in self._items)
            jobs = tuple(
                (item.item_id, item.job_id)
                for item in self._items
                if item.job_id is not None and item.state in _ACTIVE
            )
            watchers = tuple(self._watchers)
            cancellations = tuple(self._cancel_threads)
            output_threads = tuple(self._output_threads)
            media = self._imported_media
            self._listeners.clear()
        for subscription in subscriptions:
            if subscription is not None:
                subscription.close()
        if media is not None:
            for item_id, job_id in jobs:
                self._start_cancel(item_id, job_id, media)

        deadline = time.monotonic() + max(0.0, timeout)
        threads = tuple(
            thread
            for thread in (admission, *watchers, *cancellations, *output_threads)
            if thread is not None
        )
        while threads and time.monotonic() < deadline:
            for thread in threads:
                if thread is not current_thread():
                    thread.join(max(0.0, deadline - time.monotonic()))
            with self._lock:
                threads = tuple(
                    thread
                    for thread in (
                        self._admission_thread,
                        *self._watchers,
                        *self._cancel_threads,
                        *self._output_threads,
                    )
                    if thread is not None and thread is not current_thread() and thread.is_alive()
                )
        if threads:
            logger.error(
                "transcribe controller shutdown timed out",
                extra={"alive_threads": [thread.name for thread in threads]},
            )

    def _save(
        self, item_id: str, target: Path, variant: TranscriptVariant | None, serializer
    ) -> bool:
        with self._lock:
            item = self._item(item_id)
            document = item.result if item else None
            text_files = self._text_files
        if document is None or text_files is None:
            self._set_output_status(OutputState.FAILED, "File export is unavailable.")
            return False
        try:
            content = serializer(document, variant)
        except (OSError, ValueError):
            logger.exception("transcript export serialization failed", extra={"item_id": item_id})
            self._set_output_status(OutputState.FAILED, "Could not prepare the transcript export.")
            return False
        self._set_output_status(OutputState.EXPORTING, "Exporting transcript...")

        def write() -> None:
            try:
                text_files.write_atomic(Path(target), content)
            except Exception:
                logger.exception("transcript export worker failed", extra={"item_id": item_id})
                self._set_output_status(OutputState.FAILED, "Could not save the transcript export.")
            else:
                self._set_output_status(OutputState.SUCCEEDED, f"Transcript exported to {target}.")
            finally:
                with self._lock:
                    self._output_threads.discard(current_thread())

        thread = Thread(target=write, name="transcribe-page-export", daemon=True)
        with self._lock:
            self._output_threads.add(thread)
        thread.start()
        return True

    def _admit_items(self, item_ids: tuple[str, ...]) -> None:
        try:
            for item_id in item_ids:
                with self._lock:
                    item = self._item(item_id)
                    media = self._imported_media
                    if self._closed or item is None or item.state is not QueueState.PENDING:
                        continue
                    item.state = QueueState.VALIDATING
                    self._publish_locked()
                if media is None:
                    self._reject(
                        item_id, ErrorCode.RUNTIME_UNAVAILABLE, "Imported media is unavailable."
                    )
                    continue
                job_id = None
                subscription = None
                try:
                    if not item.path.is_file():
                        raise RejectedRequestError("source is not a regular file")
                    job_id = media.submit(str(item.path), ImportOptions())
                    subscription = media.observe(job_id)
                except RejectedRequestError as error:
                    self._cancel_admitted_job(media, job_id)
                    self._reject(item_id, error.code, _safe_rejection_message(error))
                    continue
                except Exception:
                    logger.exception(
                        "transcribe admission worker failed", extra={"item_id": item_id}
                    )
                    self._cancel_admitted_job(media, job_id)
                    self._reject(
                        item_id, ErrorCode.RUNTIME_UNAVAILABLE, "Imported media is unavailable."
                    )
                    continue
                with self._lock:
                    item = self._item(item_id)
                    closing = item is None or self._closed
                    if not closing:
                        item.job_id = job_id
                        item.attempt = 1
                        item.subscription = subscription
                        item.state = QueueState.QUEUED
                        item.progress = ProgressSnapshot(Stage.QUEUED)
                        self._publish_locked()
                if closing:
                    subscription.close()
                    self._start_cancel(item_id, job_id, media)
                    continue
                watcher = Thread(
                    target=self._watch_item,
                    args=(item_id, subscription),
                    name="transcribe-page-observer",
                    daemon=True,
                )
                with self._lock:
                    self._watchers.add(watcher)
                watcher.start()
        finally:
            with self._lock:
                if self._admission_thread is current_thread():
                    self._admission_thread = None

    def _retry_after_admission(self, item_id: str, previous: Thread) -> None:
        if previous is not current_thread():
            previous.join()
        with self._lock:
            if self._closed or self._item(item_id) is None:
                if self._admission_thread is current_thread():
                    self._admission_thread = None
                return
        self._admit_items((item_id,))

    def _watch_item(self, item_id: str, subscription: ObservationSubscription) -> None:
        try:
            while True:
                observation = subscription.next(timeout=0.2)
                if observation is None:
                    continue
                if isinstance(observation, EndOfStream):
                    self._fail_observer(item_id)
                    return
                self._apply_observation(item_id, observation)
                if observation.terminal is not None:
                    return
        except Exception:
            logger.exception("transcribe observer worker failed", extra={"item_id": item_id})
            self._fail_observer(item_id)
        finally:
            with self._lock:
                self._watchers.discard(current_thread())

    def _apply_observation(self, item_id: str, observation: ImportObservation) -> None:
        with self._lock:
            item = self._item(item_id)
            if item is None:
                return
            item.attempt = observation.attempt.value
            item.progress = observation.progress
            item.state = _queue_state(observation.progress.stage)
            if observation.terminal is not None:
                self._apply_terminal(item, observation.terminal)
            self._publish_locked()

    def _apply_terminal(self, item: _Item, terminal) -> None:
        if isinstance(terminal, Success):
            transcript = terminal.transcription.transcription
            if transcript.text.strip():
                item.result = TranscriptDocument(
                    item.source_name,
                    datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    transcript.duration,
                    transcript.text,
                    getattr(transcript, "enhanced_text", None),
                )
                item.state = QueueState.SUCCEEDED
                item.failure = None
                return
            item.failure = ImportFailure(
                ErrorCode.TRANSCRIPTION_FAILED,
                "Transcription returned no text.",
                "transcription",
                False,
            )
            item.state = QueueState.FAILED
            return
        if isinstance(terminal, Cancelled):
            item.failure = ImportFailure(
                ErrorCode.CANCELLED, "Processing was cancelled.", "cleanup", False
            )
            item.state = QueueState.CANCELLED
            return
        if isinstance(terminal, Failed):
            item.failure = ImportFailure(
                terminal.code,
                terminal.safe_message,
                terminal.stage.value,
                terminal.retryable,
            )
            item.state = QueueState.FAILED

    def _cancel_job(self, item_id: str, job_id: JobId, media: ImportedMediaPort) -> None:
        try:
            media.cancel(job_id)
        except Exception:
            logger.exception("transcribe cancellation worker failed", extra={"item_id": item_id})
            self._set_page_error("Could not cancel the transcription job.")
        finally:
            with self._lock:
                self._cancel_threads.discard(current_thread())

    def _start_cancel(self, item_id: str, job_id: JobId, media: ImportedMediaPort) -> None:
        thread = Thread(
            target=self._cancel_job,
            args=(item_id, job_id, media),
            name="transcribe-page-cancel",
            daemon=True,
        )
        with self._lock:
            self._cancel_threads.add(thread)
        thread.start()

    @staticmethod
    def _cancel_admitted_job(media: ImportedMediaPort, job_id: JobId | None) -> None:
        if job_id is None:
            return
        try:
            media.cancel(job_id)
        except Exception:
            logger.exception("late imported job cancellation failed", extra={"job_id": str(job_id)})

    def _fail_observer(self, item_id: str) -> None:
        with self._lock:
            item = self._item(item_id)
            if self._closed or item is None or item.state in _TERMINAL:
                return
            item.state = QueueState.FAILED
            item.failure = ImportFailure(
                ErrorCode.RUNTIME_UNAVAILABLE,
                "Observation ended without a terminal result.",
                "observation",
                True,
            )
            self._publish_locked()

    def _reject(self, item_id: str, code: ErrorCode, message: str) -> None:
        with self._lock:
            item = self._item(item_id)
            if item is None:
                return
            item.state = QueueState.REJECTED
            item.failure = ImportFailure(code, message, "admission", code is ErrorCode.QUEUE_FULL)
            item.progress = ProgressSnapshot(Stage.ACCEPTED)
            self._publish_locked()

    def _set_page_error(self, message: str) -> None:
        with self._lock:
            self._snapshot = replace(self._snapshot, page_error=message)
            self._notify_locked()

    def _set_output_status(self, state: OutputState, message: str) -> None:
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                output_status=OutputStatus(state, message),
            )
            self._notify_locked()

    def _item(self, item_id: str) -> _Item | None:
        return next((item for item in self._items if item.item_id == item_id), None)

    def _index(self, item_id: str) -> int | None:
        try:
            return next(index for index, item in enumerate(self._items) if item.item_id == item_id)
        except StopIteration:
            return None

    def _admission_active_locked(self) -> bool:
        return self._admission_thread is not None and self._admission_thread.is_alive()

    def _publish_locked(self) -> None:
        snapshots = tuple(_snapshot(item) for item in self._items)
        active = sum(item.state in _ACTIVE for item in self._items)
        can_cancel_all = any(
            item.state in _ACTIVE and item.job_id is not None for item in self._items
        )
        pending = sum(item.state is QueueState.PENDING for item in self._items)
        aggregate = QueueAggregate(
            len(self._items),
            pending,
            active,
            sum(item.state is QueueState.SUCCEEDED for item in self._items),
            sum(item.state is QueueState.FAILED for item in self._items),
            sum(item.state is QueueState.CANCELLED for item in self._items),
            sum(item.state is QueueState.REJECTED for item in self._items),
        )
        is_processing = active > 0 or self._admission_active_locked()
        available = (
            self._imported_media is not None
            and self._availability is TranscribeAvailability.AVAILABLE
            and not self._closed
        )
        self._snapshot = TranscribePageSnapshot(
            snapshots,
            is_processing=is_processing,
            can_start=pending > 0 and available and not is_processing,
            can_cancel_all=can_cancel_all,
            accepting_files=available,
            availability=self._availability,
            aggregate=aggregate,
            page_error=self._snapshot.page_error,
            output_status=self._snapshot.output_status,
        )
        self._notify_locked()

    def _notify_locked(self) -> None:
        snapshot = self._snapshot
        listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(snapshot)
            except Exception:
                logger.exception("transcribe snapshot listener failed")


_ACTIVE = {
    QueueState.VALIDATING,
    QueueState.QUEUED,
    QueueState.NORMALIZING,
    QueueState.TRANSCRIBING,
    QueueState.RETRY_WAITING,
    QueueState.CLEANING_UP,
}
_TERMINAL = {QueueState.SUCCEEDED, QueueState.FAILED, QueueState.CANCELLED, QueueState.REJECTED}


def _queue_state(stage: Stage) -> QueueState:
    try:
        return QueueState(stage.value)
    except ValueError:
        return QueueState.VALIDATING


def _snapshot(item: _Item) -> TranscriptionQueueItemSnapshot:
    return TranscriptionQueueItemSnapshot(
        item.item_id,
        item.job_id,
        item.attempt,
        item.source_name,
        item.format_hint,
        item.state,
        item.progress,
        item.result,
        item.failure,
        item.state in {QueueState.PENDING, QueueState.REJECTED},
        item.state in _ACTIVE and item.job_id is not None,
        item.state in {QueueState.FAILED, QueueState.CANCELLED, QueueState.REJECTED},
    )


def _safe_rejection_message(error: RejectedRequestError) -> str:
    messages = {
        ErrorCode.INVALID_SOURCE: "The source is not a permitted local file.",
        ErrorCode.QUEUE_FULL: "The import queue is full.",
        ErrorCode.RESOURCE_LIMIT_EXCEEDED: "The media exceeds a configured resource limit.",
    }
    return messages.get(error.code, "The file could not be added.")
