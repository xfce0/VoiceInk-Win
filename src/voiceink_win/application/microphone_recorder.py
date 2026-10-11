"""Asynchronous presentation-facing orchestration for microphone recording."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Thread, Timer

from voiceink_win.domain import (
    ACTIVE_RECORDER_STATES,
    TERMINAL_RECORDER_STATES,
    AsrError,
    AsrErrorCode,
    CaptureError,
    CaptureErrorCode,
    InputLevel,
    MicrophoneCapability,
    RecorderSnapshot,
    RecorderState,
    RecordingResult,
    RecordingState,
)

from .microphone_service import MicrophoneRecordingHandle, MicrophoneRecordingService

logger = logging.getLogger(__name__)

RECORDER_SOUND_ON_THRESHOLD = 0.08
RECORDER_SOUND_OFF_THRESHOLD = 0.04
RECORDER_LEVEL_STALE_SECONDS = 0.250

RecorderListener = Callable[[RecorderSnapshot], None]


class MicrophoneRecorderController:
    """Serialize recorder commands while keeping capture and ASR off the Qt thread."""

    def __init__(self, service: MicrophoneRecordingService | None = None) -> None:
        self._lock = Lock()
        self._service = None
        self._snapshot = RecorderSnapshot()
        self._listeners: list[RecorderListener] = []
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="microphone-command")
        self._generation = 0
        self._handle: MicrophoneRecordingHandle | None = None
        self._pending_cancel = False
        self._pending_stop = False
        self._unsubscribe_level: Callable[[], None] | None = None
        self._level_timer: Timer | None = None
        self._last_level_timestamp = -1.0
        self._closed = False
        if service is not None:
            self.attach_service(service)

    @property
    def snapshot(self) -> RecorderSnapshot:
        with self._lock:
            return self._snapshot

    def subscribe(self, listener: RecorderListener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def attach_service(self, service: MicrophoneRecordingService | None) -> None:
        if service is None:
            self._publish(RecorderSnapshot(RecorderState.UNAVAILABLE))
            return
        status = service.status
        state = (
            RecorderState.IDLE
            if status.capability is MicrophoneCapability.SUPPORTED
            else RecorderState.UNAVAILABLE
        )
        with self._lock:
            if self._closed or self._handle is not None:
                return
            self._service = service
            generation = self._generation
        message = status.message if state is RecorderState.UNAVAILABLE else ""
        self._publish(RecorderSnapshot(state, message=message, generation=generation))

    def start(self, selection_token: str | None = None) -> bool:
        with self._lock:
            if self._closed or self._service is None:
                return False
            if self._snapshot.state not in {RecorderState.IDLE, *TERMINAL_RECORDER_STATES}:
                return False
            self._generation += 1
            generation = self._generation
            self._pending_cancel = False
            self._pending_stop = False
            service = self._service
        self._publish(RecorderSnapshot(RecorderState.REQUESTING_ACCESS, generation=generation))
        self._executor.submit(self._start_worker, service, generation, selection_token)
        return True

    def stop(self) -> bool:
        with self._lock:
            if self._snapshot.state not in {
                RecorderState.RECORDING_SILENT,
                RecorderState.RECORDING_SOUNDING,
            }:
                return False
            self._pending_stop = True
            handle = self._handle
            generation = self._generation
        self._publish(RecorderSnapshot(RecorderState.STOPPING, generation=generation))
        if handle is None:
            return True
        try:
            handle.stop()
        except Exception:
            logger.exception("microphone stop command failed")
            self._publish_error(generation, "microphone stop failed")
            return False
        self._publish(RecorderSnapshot(RecorderState.PROCESSING, generation=generation))
        return True

    def cancel(self) -> bool:
        with self._lock:
            if self._snapshot.state not in ACTIVE_RECORDER_STATES:
                return False
            self._pending_cancel = True
            self._pending_stop = False
            handle = self._handle
            generation = self._generation
        self._publish(RecorderSnapshot(RecorderState.CANCEL_REQUESTED, generation=generation))
        if handle is not None:
            try:
                handle.cancel()
            except Exception:
                logger.exception("microphone cancel command failed")
                self._publish_error(generation, "microphone cancellation failed")
        return True

    def dismiss(self) -> bool:
        with self._lock:
            if self._snapshot.state in ACTIVE_RECORDER_STATES:
                return False
            self._clear_level_subscription_locked()
            generation = self._generation
        self._publish(RecorderSnapshot(self.snapshot.state, generation=generation))
        return True

    def close(self, deadline: float = 5.0) -> None:
        if deadline < 0:
            raise ValueError("deadline must not be negative")
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handle = self._handle
            generation = self._generation
        if handle is not None:
            try:
                handle.cancel()
                handle.wait(deadline)
            except Exception:
                logger.exception("microphone recorder did not close cleanly")
                self._publish(
                    RecorderSnapshot(RecorderState.RECOVERY_PENDING, generation=generation)
                )
        with self._lock:
            self._clear_level_subscription_locked()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def start_recording(self) -> bool:
        return self.start()

    def stop_recording(self) -> bool:
        return self.stop()

    def _start_worker(
        self,
        service: MicrophoneRecordingService,
        generation: int,
        selection_token: str | None,
    ) -> None:
        self._publish_if_current(
            RecorderSnapshot(RecorderState.STARTING, generation=generation), generation
        )
        try:
            handle = service.start(selection_token, request_id=f"recorder-{generation}")
        except Exception as error:
            self._start_failed(generation, error)
            return

        with self._lock:
            if generation != self._generation or self._closed:
                cancel = True
                pending_stop = False
            else:
                self._handle = handle
                cancel = self._pending_cancel
                pending_stop = self._pending_stop
                self._last_level_timestamp = -1.0
        self._subscribe_levels(handle, generation)
        if cancel:
            self._publish_if_current(
                RecorderSnapshot(RecorderState.CANCEL_REQUESTED, generation=generation), generation
            )
            handle.cancel()
        elif pending_stop:
            self._publish_if_current(
                RecorderSnapshot(RecorderState.PROCESSING, generation=generation), generation
            )
            handle.stop()
        else:
            self._publish_if_current(
                RecorderSnapshot(RecorderState.RECORDING_SILENT, generation=generation), generation
            )
        Thread(
            target=self._watch_result,
            args=(handle, generation),
            name=f"microphone-result-{generation}",
            daemon=True,
        ).start()

    def _subscribe_levels(self, handle: MicrophoneRecordingHandle, generation: int) -> None:
        try:
            unsubscribe = handle.subscribe_level(
                lambda level: self._level_received(generation, level)
            )
        except Exception:
            logger.exception("microphone level subscription failed")
            return
        with self._lock:
            if generation != self._generation or self._closed:
                unsubscribe()
            else:
                self._unsubscribe_level = unsubscribe

    def _level_received(self, generation: int, level: InputLevel) -> None:
        if not isinstance(level, InputLevel):
            return
        now = time.monotonic()
        if level.timestamp < now - RECORDER_LEVEL_STALE_SECONDS:
            return
        with self._lock:
            if generation != self._generation or level.timestamp < self._last_level_timestamp:
                return
            state = self._snapshot.state
            if state not in {RecorderState.RECORDING_SILENT, RecorderState.RECORDING_SOUNDING}:
                return
            self._last_level_timestamp = level.timestamp
            next_state = state
            if (
                state is RecorderState.RECORDING_SILENT
                and level.value >= RECORDER_SOUND_ON_THRESHOLD
            ):
                next_state = RecorderState.RECORDING_SOUNDING
            elif (
                state is RecorderState.RECORDING_SOUNDING
                and level.value <= RECORDER_SOUND_OFF_THRESHOLD
            ):
                next_state = RecorderState.RECORDING_SILENT
            self._restart_level_timer_locked(generation)
        self._publish(RecorderSnapshot(next_state, level=level.value, generation=generation))

    def _restart_level_timer_locked(self, generation: int) -> None:
        if self._level_timer is not None:
            self._level_timer.cancel()
        self._level_timer = Timer(
            RECORDER_LEVEL_STALE_SECONDS,
            self._expire_level,
            args=(generation,),
        )
        self._level_timer.daemon = True
        self._level_timer.start()

    def _expire_level(self, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            if time.monotonic() - self._last_level_timestamp < RECORDER_LEVEL_STALE_SECONDS:
                return
            if self._snapshot.state is not RecorderState.RECORDING_SOUNDING:
                return
        self._publish(RecorderSnapshot(RecorderState.RECORDING_SILENT, generation=generation))

    def _watch_result(self, handle: MicrophoneRecordingHandle, generation: int) -> None:
        try:
            result = handle.wait()
        except Exception:
            logger.exception("microphone result wait failed")
            self._publish_error(generation, "microphone recording failed")
            return
        self._publish_result(generation, result)

    def _publish_result(self, generation: int, result: RecordingResult) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._handle = None
            self._clear_level_subscription_locked()
        if result.state is RecordingState.CANCELLED:
            snapshot = RecorderSnapshot(RecorderState.CANCELLED, generation=generation)
        elif result.state is RecordingState.EMPTY:
            snapshot = RecorderSnapshot(RecorderState.EMPTY, generation=generation)
        elif result.state is RecordingState.SUCCEEDED:
            transcript = result.transcript.text if result.transcript is not None else ""
            snapshot = RecorderSnapshot(
                RecorderState.SUCCEEDED,
                transcript=transcript,
                generation=generation,
            )
        elif (
            isinstance(result.error, CaptureError)
            and result.error.code is CaptureErrorCode.CLEANUP_FAILED
        ):
            snapshot = RecorderSnapshot(
                RecorderState.RECOVERY_PENDING,
                message="microphone cleanup is still pending",
                generation=generation,
            )
        else:
            snapshot = RecorderSnapshot(
                RecorderState.ERROR,
                message=_safe_error_message(result.error),
                generation=generation,
            )
        self._publish(snapshot)

    def _start_failed(self, generation: int, error: Exception) -> None:
        with self._lock:
            cancelled = self._pending_cancel
        if cancelled:
            self._publish_if_current(
                RecorderSnapshot(RecorderState.CANCELLED, generation=generation), generation
            )
            return
        self._publish_if_current(
            RecorderSnapshot(
                RecorderState.ERROR,
                message=_safe_error_message(error),
                generation=generation,
            ),
            generation,
        )

    def _publish_error(self, generation: int, message: str) -> None:
        self._publish_if_current(
            RecorderSnapshot(RecorderState.ERROR, message=message, generation=generation),
            generation,
        )

    def _publish_if_current(self, snapshot: RecorderSnapshot, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
        self._publish(snapshot)

    def _publish(self, snapshot: RecorderSnapshot) -> None:
        with self._lock:
            if self._closed and snapshot.state is not RecorderState.RECOVERY_PENDING:
                return
            self._snapshot = snapshot
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(snapshot)
            except Exception:
                logger.exception("recorder listener failed")

    def _clear_level_subscription_locked(self) -> None:
        if self._level_timer is not None:
            self._level_timer.cancel()
            self._level_timer = None
        unsubscribe = self._unsubscribe_level
        self._unsubscribe_level = None
        if unsubscribe is not None:
            try:
                unsubscribe()
            except Exception:
                logger.exception("microphone level unsubscribe failed")


def _safe_error_message(error: BaseException | None) -> str:
    if isinstance(error, CaptureError):
        return {
            CaptureErrorCode.PERMISSION_DENIED: (
                "Allow microphone access in Windows Privacy settings and try again."
            ),
            CaptureErrorCode.DEVICE_UNAVAILABLE: (
                "The microphone is unavailable. Check the selected device and try again."
            ),
            CaptureErrorCode.BUSY: "The microphone is busy. Try again when it is free.",
            CaptureErrorCode.DEVICE_DISCONNECTED: (
                "The microphone was disconnected. Connect it and try again."
            ),
            CaptureErrorCode.TIMEOUT: "Microphone access timed out. Try again.",
            CaptureErrorCode.CANCELLED: "The recording was cancelled.",
        }.get(error.code, "Microphone recording failed. Try again.")
    if isinstance(error, AsrError):
        if error.code is AsrErrorCode.QUEUE_FULL:
            return "The transcription queue is busy. Try again shortly."
        if error.code is AsrErrorCode.RECOVERY_PENDING:
            return "Transcription cleanup is still in progress."
    return "Microphone recording failed. Try again."
