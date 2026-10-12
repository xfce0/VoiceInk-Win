"""Application orchestration for one bounded microphone recording."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from threading import Event, Lock, Thread
from typing import Protocol
from uuid import uuid4

from voiceink_win.domain import (
    AsrError,
    AsrRequest,
    AsrRequestHandle,
    AudioCaptureSession,
    AudioInputPort,
    BoundedPcm16Capture,
    CancellationError,
    CaptureBusyError,
    CaptureCancelledError,
    CaptureError,
    CaptureErrorCode,
    CaptureLimits,
    CaptureTimeoutError,
    InputDevice,
    InputLevel,
    MicrophoneStatus,
    QueueFullError,
    RecordingResult,
    RecordingState,
    RuntimeRecoveryPendingError,
)

from .cancellation import EventCancellationToken

logger = logging.getLogger(__name__)

DEFAULT_ASR_DEADLINE_SECONDS = 45 * 60
ASR_RECOVERY_BUDGET_SECONDS = 30.0


class MicrophoneAsrScheduler(Protocol):
    def try_admit(self, request: AsrRequest) -> AsrRequestHandle: ...


class MicrophoneRecordingHandle:
    """Control and completion boundary for one single-use recording."""

    def __init__(
        self,
        service: MicrophoneRecordingService,
        session: AudioCaptureSession,
        *,
        request_id: str,
        capture_deadline: float,
        asr_deadline_seconds: float,
        limits: CaptureLimits,
    ) -> None:
        self._service = service
        self._session = session
        self._request_id = request_id
        self._capture_deadline = capture_deadline
        self._asr_deadline_seconds = asr_deadline_seconds
        self._capture = BoundedPcm16Capture(limits)
        self._stop_requested = Event()
        self._cancel_requested = Event()
        self._capture_done = Event()
        self._done = Event()
        self._control_lock = Lock()
        self._asr_handle: AsrRequestHandle | None = None
        self._result: RecordingResult | None = None
        self._recovery_pending = False

    @property
    def result(self) -> RecordingResult | None:
        with self._control_lock:
            return self._result

    @property
    def is_done(self) -> bool:
        return self._done.is_set()

    @property
    def recovery_pending(self) -> bool:
        return self._recovery_pending

    def wait_capture(self) -> None:
        self._capture_done.wait()

    def stop(self) -> None:
        if self._done.is_set():
            return
        self._stop_requested.set()
        self._session.stop()

    def cancel(self) -> None:
        if self._done.is_set() and not self._recovery_pending:
            return
        self._cancel_requested.set()
        failure: Exception | None = None
        try:
            self._service._cancel_asr(self)
        except Exception as error:
            failure = error
            logger.exception("microphone ASR cancellation failed")
        try:
            self._session.cancel()
        except Exception as error:
            failure = failure or error
            logger.exception("microphone capture cancellation failed")
        if failure is not None:
            raise failure

    def subscribe_level(self, listener: Callable[[InputLevel], None]) -> Callable[[], None]:
        subscribe = getattr(self._session, "subscribe_level", None)
        if subscribe is None:
            return lambda: None
        return subscribe(listener)

    def wait(self, timeout: float | None = None) -> RecordingResult:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must not be negative")
        if not self._done.wait(timeout):
            raise CaptureTimeoutError("recording did not finish before the wait deadline")
        result = self.result
        if result is None:
            raise RuntimeError("recording completed without a terminal result")
        return result

    def _set_asr_handle(self, handle: AsrRequestHandle | None) -> None:
        with self._control_lock:
            self._asr_handle = handle

    def _cancelled(self) -> bool:
        return self._cancel_requested.is_set()

    def _finish(self, result: RecordingResult) -> None:
        with self._control_lock:
            self._result = result
        self._done.set()
        self._service._finished(self)

    def _run(self) -> None:
        try:
            self._session.start()
            while True:
                if self._cancelled():
                    raise CaptureCancelledError()
                if time.monotonic() >= self._capture_deadline:
                    raise CaptureTimeoutError("microphone capture deadline exceeded")
                chunk = self._session.read_chunk(self._capture_deadline)
                if chunk is None:
                    break
                self._capture.append(chunk)
            if self._cancelled():
                raise CaptureCancelledError()
            audio = self._capture.finish()
        except CaptureError as error:
            close_error = self._close_session()
            self._capture_done.set()
            self._finish(_capture_result(close_error or error))
            return
        except Exception:
            logger.exception("microphone capture worker failed")
            self._close_session()
            self._capture_done.set()
            self._finish(
                RecordingResult(
                    RecordingState.FAILED,
                    error=CaptureError(
                        code=CaptureErrorCode.FAILED,
                        message="microphone capture failed",
                    ),
                )
            )
            return

        close_error = self._close_session()
        self._capture_done.set()
        if close_error is not None:
            self._finish(_capture_result(close_error))
            return
        if audio is None:
            self._finish(RecordingResult(RecordingState.EMPTY))
            return
        self._transcribe(audio)

    def _close_session(self) -> CaptureError | None:
        try:
            self._session.close()
        except CaptureError as error:
            return error
        except Exception:
            logger.exception("microphone session cleanup failed")
            return CaptureError(
                code=CaptureErrorCode.FAILED,
                message="microphone session cleanup failed",
            )
        return None

    def _transcribe(self, audio) -> None:
        if self._cancelled():
            self._finish(RecordingResult(RecordingState.CANCELLED, error=CaptureCancelledError()))
            return
        request = AsrRequest(
            audio=audio,
            request_id=self._request_id,
            deadline=time.monotonic() + self._asr_deadline_seconds,
            cancellation=EventCancellationToken(self._cancel_requested),
        )
        try:
            handle = self._service._asr.try_admit(request)
        except QueueFullError as error:
            self._finish(RecordingResult(RecordingState.REJECTED, error=error))
            return
        except CancellationError as error:
            self._finish(_asr_result(error, cancelled=True))
            return
        except AsrError as error:
            self._finish(RecordingResult(RecordingState.FAILED, error=error))
            return

        self._set_asr_handle(handle)
        result: RecordingResult | None = None
        try:
            if self._cancelled():
                handle.cancel()
            transcript = handle.await_result()
            result = (
                RecordingResult(RecordingState.CANCELLED, error=CaptureCancelledError())
                if self._cancelled()
                else RecordingResult(RecordingState.SUCCEEDED, audio=audio, transcript=transcript)
            )
        except (CancellationError, AsrError) as error:
            result = _asr_result(error, cancelled=self._cancelled())

        try:
            handle.await_quiescence()
        except RuntimeRecoveryPendingError as error:
            self._recovery_pending = True
            Thread(
                target=self._reconcile_asr,
                args=(handle, result or RecordingResult(RecordingState.FAILED, error=error)),
                name=f"microphone-asr-recovery-{self._request_id}",
                daemon=True,
            ).start()
            return
        else:
            try:
                handle.release()
            except AsrError as error:
                result = RecordingResult(RecordingState.FAILED, error=error)
        finally:
            if not self._recovery_pending:
                self._set_asr_handle(None)
        self._finish(result or RecordingResult(RecordingState.FAILED))

    def _reconcile_asr(self, handle: AsrRequestHandle, result: RecordingResult) -> None:
        deadline = time.monotonic() + ASR_RECOVERY_BUDGET_SECONDS
        while True:
            try:
                handle.await_quiescence(deadline=min(deadline, time.monotonic() + 0.5))
                break
            except RuntimeRecoveryPendingError:
                if time.monotonic() >= deadline:
                    logger.error("microphone ASR recovery exceeded its deadline")
                    return
                time.sleep(0.1)
        while True:
            try:
                handle.release()
                break
            except RuntimeRecoveryPendingError:
                if time.monotonic() >= deadline:
                    logger.error("microphone ASR release exceeded its deadline")
                    return
                time.sleep(0.1)
            except AsrError as error:
                result = RecordingResult(RecordingState.FAILED, error=error)
                break
        self._set_asr_handle(None)
        self._recovery_pending = False
        self._finish(result)


class MicrophoneRecordingService:
    """Coordinate capture and submit only finalized audio to shared ASR admission."""

    def __init__(
        self,
        audio_input: AudioInputPort,
        asr: MicrophoneAsrScheduler,
        *,
        limits: CaptureLimits | None = None,
        asr_deadline_seconds: float = DEFAULT_ASR_DEADLINE_SECONDS,
    ) -> None:
        if asr_deadline_seconds <= 0:
            raise ValueError("ASR deadline must be positive")
        self._audio_input = audio_input
        self._asr = asr
        self._limits = limits or CaptureLimits()
        self._asr_deadline_seconds = asr_deadline_seconds
        self._lock = Lock()
        self._active: MicrophoneRecordingHandle | None = None
        self._closed = False
        self._opening = False
        self._opening_done = Event()
        self._opening_done.set()

    @property
    def status(self) -> MicrophoneStatus:
        return self._audio_input.status()

    @property
    def active(self) -> MicrophoneRecordingHandle | None:
        with self._lock:
            return self._active

    def enumerate_devices(self, deadline: float | None = None) -> tuple[InputDevice, ...]:
        return self._audio_input.enumerate_devices(deadline)

    def start(
        self,
        selection_token: str | None = None,
        *,
        request_id: str | None = None,
        deadline: float | None = None,
    ) -> MicrophoneRecordingHandle:
        with self._lock:
            if self._closed:
                raise CaptureError(CaptureErrorCode.CANCELLED, "microphone service is closed")
            if self._active is not None and (
                not self._active.is_done or self._active.recovery_pending
            ):
                raise CaptureBusyError()
            capture_deadline = (
                deadline
                if deadline is not None
                else time.monotonic() + self._limits.max_duration_seconds
            )
            self._opening = True
            self._opening_done.clear()
        try:
            session = self._audio_input.open(selection_token, capture_deadline)
        except BaseException:
            with self._lock:
                self._opening = False
                self._opening_done.set()
            raise
        with self._lock:
            self._opening = False
            self._opening_done.set()
            if self._closed:
                try:
                    session.close()
                except Exception:
                    logger.exception("microphone session cleanup failed after close")
                raise CaptureError(CaptureErrorCode.CANCELLED, "microphone service is closed")
            handle = MicrophoneRecordingHandle(
                self,
                session,
                request_id=request_id or str(uuid4()),
                capture_deadline=capture_deadline,
                asr_deadline_seconds=self._asr_deadline_seconds,
                limits=self._limits,
            )
            self._active = handle
        Thread(target=handle._run, name="microphone-recording", daemon=True).start()
        return handle

    def close(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        with self._lock:
            self._closed = True
            active = self._active
            opening = self._opening
        if opening:
            self._opening_done.wait(max(0.0, deadline - time.monotonic()))
            active = self.active
        if active is None or (active.is_done and not active.recovery_pending):
            return
        Thread(target=_cancel_recording, args=(active,), daemon=True).start()
        active.wait(max(0.0, deadline - time.monotonic()))

    def _cancel_asr(self, recording: MicrophoneRecordingHandle) -> None:
        with recording._control_lock:
            handle = recording._asr_handle
        if handle is not None:
            handle.cancel()

    def _finished(self, recording: MicrophoneRecordingHandle) -> None:
        with self._lock:
            if self._active is recording and not recording.recovery_pending:
                self._active = None


def _capture_result(error: CaptureError) -> RecordingResult:
    state = (
        RecordingState.CANCELLED
        if error.code is CaptureErrorCode.CANCELLED
        else RecordingState.FAILED
    )
    return RecordingResult(state, error=error)


def _asr_result(error: AsrError, *, cancelled: bool) -> RecordingResult:
    state = RecordingState.CANCELLED if cancelled else RecordingState.FAILED
    return RecordingResult(state, error=error)


def _cancel_recording(recording: MicrophoneRecordingHandle) -> None:
    try:
        recording.cancel()
    except Exception:
        logger.exception("microphone service cancellation failed during close")
