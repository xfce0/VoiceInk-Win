from __future__ import annotations

import time
from pathlib import Path
from threading import Event, Thread, current_thread

from voiceink_win.application import (
    AsrApplicationService,
    ImportedMediaTranscriptionService,
    TranscribePageController,
)
from voiceink_win.application.transcribe_output import serialize_markdown
from voiceink_win.domain import (
    Attempt,
    Cancelled,
    EndOfStream,
    ErrorCode,
    Failed,
    ImportedTranscriptionResult,
    ImportObservation,
    JobId,
    OutputState,
    ProcessingMetadata,
    ProgressSnapshot,
    Stage,
    Success,
    TranscriptDocument,
    TranscriptResult,
    TranscriptVariant,
)
from voiceink_win.infrastructure import FakeAsrRuntime, FakeMediaNormalizer, FakeSnapshotStore


class FakeSubscription:
    def __init__(self, job_id: JobId) -> None:
        self.job_id = job_id
        self._events: list[ImportObservation] = []
        self._ready = Event()
        self.closed = False

    def push(self, observation: ImportObservation) -> None:
        self._events.append(observation)
        self._ready.set()

    def next(self, timeout=None):
        if not self._events:
            self._ready.wait(timeout)
        if not self._events:
            return None
        event = self._events.pop(0)
        if not self._events:
            self._ready.clear()
        return event

    def close(self) -> None:
        self.closed = True
        self._ready.set()


class FakeImportedMedia:
    def __init__(self) -> None:
        self.submissions: list[str] = []
        self.jobs: dict[JobId, FakeSubscription] = {}
        self.cancelled: list[JobId] = []
        self.submit_started: Event | None = None
        self.allow_submit: Event | None = None

    def submit(self, path: str, options=None) -> JobId:
        del options
        if self.submit_started is not None:
            self.submit_started.set()
        if self.allow_submit is not None:
            self.allow_submit.wait(2)
        job_id = JobId(f"job-{len(self.jobs) + 1}")
        self.submissions.append(path)
        self.jobs[job_id] = FakeSubscription(job_id)
        return job_id

    def observe(self, job_id: JobId) -> FakeSubscription:
        subscription = self.jobs[job_id]
        subscription.push(ImportObservation(job_id, Attempt(1), ProgressSnapshot(Stage.QUEUED)))
        return subscription

    def cancel(self, job_id: JobId) -> bool:
        self.cancelled.append(job_id)
        return True

    def finish_success(self, job_id: JobId, text: str = "original") -> None:
        self.jobs[job_id].push(
            ImportObservation(
                job_id,
                Attempt(1),
                ProgressSnapshot(Stage.SUCCEEDED),
                Success(
                    "succeeded",
                    job_id,
                    Attempt(1),
                    ImportedTranscriptionResult(
                        job_id,
                        Path(self.submissions[0]).name,
                        TranscriptResult(text, 1.25),
                        ProcessingMetadata(1.25, 1, (), 0.1, Stage.SUCCEEDED),
                        diagnostics=None,  # type: ignore[arg-type]
                    ),
                ),
            )
        )

    def finish_failure(self, job_id: JobId, *, retryable: bool = False) -> None:
        self.jobs[job_id].push(
            ImportObservation(
                job_id,
                Attempt(1),
                ProgressSnapshot(Stage.CLEANING_UP),
                Failed(
                    "failed",
                    job_id,
                    Attempt(1),
                    Stage.TRANSCRIPTION,
                    ErrorCode.TRANSCRIPTION_FAILED,
                    "Transcription failed.",
                    retryable,
                ),
            )
        )

    def finish_cancelled(self, job_id: JobId) -> None:
        self.jobs[job_id].push(
            ImportObservation(
                job_id,
                Attempt(1),
                ProgressSnapshot(Stage.CLEANING_UP),
                Cancelled("cancelled", job_id, Attempt(1), Stage.CLEANING_UP, "user"),  # type: ignore[arg-type]
            )
        )

    def finish_without_terminal(self, job_id: JobId) -> None:
        self.jobs[job_id].push(EndOfStream())  # type: ignore[arg-type]


class FakeClipboard:
    def __init__(self) -> None:
        self.text = ""
        self.thread_id: int | None = None

    def copy(self, text: str, completion) -> None:
        self.text = text
        self.thread_id = current_thread().ident
        completion(None)


class FakeFiles:
    def __init__(self) -> None:
        self.writes: list[tuple[Path, bytes]] = []
        self.fail = False

    def write_atomic(self, target: Path, content: bytes) -> None:
        if self.fail:
            raise OSError("disk full")
        self.writes.append((target, content))


def wait_for(predicate) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_controller_tracks_every_required_format_as_a_hint(tmp_path: Path) -> None:
    media = FakeImportedMedia()
    controller = TranscribePageController(media)
    paths = []
    for extension in (
        "wav",
        "mp3",
        "m4a",
        "aiff",
        "mp4",
        "mov",
        "aac",
        "flac",
        "caf",
        "amr",
        "ogg",
        "opus",
        "3gp",
        "webm",
    ):
        path = tmp_path / f"sample.{extension}"
        path.write_bytes(b"content")
        paths.append(path)

    controller.add_paths(paths)

    assert [item.source_format_hint.value for item in controller.snapshot.items] == [
        extension.upper()
        for extension in (
            "wav",
            "mp3",
            "m4a",
            "aiff",
            "mp4",
            "mov",
            "aac",
            "flac",
            "caf",
            "amr",
            "ogg",
            "opus",
            "3gp",
            "webm",
        )
    ]
    assert "oga" not in {
        item.source_format_hint.value.lower()
        for item in controller.snapshot.items
        if item.source_format_hint
    }
    controller.close()


def test_controller_submits_files_and_publishes_success_failure_and_cancellation(
    tmp_path: Path,
) -> None:
    media = FakeImportedMedia()
    controller = TranscribePageController(media)
    paths = [tmp_path / name for name in ("success.wav", "failure.mp3", "cancel.m4a")]
    for path in paths:
        path.write_bytes(b"content")
    controller.add_paths(paths)
    assert controller.start_queue()
    wait_for(lambda: len(media.jobs) == 3)

    jobs = tuple(media.jobs)
    media.finish_success(jobs[0], "hello")
    media.finish_failure(jobs[1])
    assert controller.cancel_item(controller.snapshot.items[2].item_id)
    media.finish_cancelled(jobs[2])
    wait_for(
        lambda: all(
            item.state.value in {"succeeded", "failed", "cancelled"}
            for item in controller.snapshot.items
        )
    )

    assert controller.snapshot.items[0].result is not None
    assert controller.snapshot.items[1].failure.code is ErrorCode.TRANSCRIPTION_FAILED
    assert controller.snapshot.items[2].failure.code is ErrorCode.CANCELLED
    assert jobs[2] in media.cancelled
    controller.close()


def test_controller_exports_and_copies_selected_variant(tmp_path: Path) -> None:
    media = FakeImportedMedia()
    clipboard = FakeClipboard()
    files = FakeFiles()
    controller = TranscribePageController(media, clipboard=clipboard, text_files=files)
    source = tmp_path / "meeting.wav"
    source.write_bytes(b"content")
    controller.add_paths([source])
    controller.start_queue()
    wait_for(lambda: len(media.jobs) == 1)
    job_id = next(iter(media.jobs))
    media.finish_success(job_id, "# original\n[link]")
    wait_for(lambda: controller.snapshot.items[0].result is not None)

    assert controller.copy(controller.snapshot.items[0].item_id)
    wait_for(lambda: controller.snapshot.output_status.state is OutputState.SUCCEEDED)
    assert clipboard.text == "# original\n[link]"
    assert clipboard.thread_id != current_thread().ident
    assert (
        controller.select_variant(controller.snapshot.items[0].item_id, TranscriptVariant.ENHANCED)
        is True
    )
    assert controller.snapshot.items[0].selected_variant is TranscriptVariant.ORIGINAL
    assert controller.save_txt(controller.snapshot.items[0].item_id, tmp_path / "out.txt")
    assert controller.save_markdown(controller.snapshot.items[0].item_id, tmp_path / "out.md")
    wait_for(lambda: len(files.writes) == 2)
    assert files.writes[0][1] == b"# original\n[link]\n"
    assert b"# Transcription\n" in files.writes[1][1]
    assert b"\\# original" in files.writes[1][1]
    files.fail = True
    assert controller.save_txt(controller.snapshot.items[0].item_id, tmp_path / "failed.txt")
    wait_for(lambda: controller.snapshot.output_status.state is OutputState.FAILED)
    controller.close()


def test_controller_disables_start_when_runtime_becomes_unavailable(tmp_path: Path) -> None:
    controller = TranscribePageController(FakeImportedMedia())
    source = tmp_path / "queued.wav"
    source.write_bytes(b"content")
    controller.add_paths([source])

    assert controller.snapshot.can_start
    controller.mark_unavailable("runtime stopped")

    assert not controller.snapshot.accepting_files
    assert not controller.snapshot.can_start
    controller.close()


def test_markdown_export_has_stable_utc_timestamp_and_escaped_content() -> None:
    document = TranscriptDocument(
        "meeting[1].wav",
        "2024-01-02T03:04:05.123+03:00",
        1.25,
        "# title\n[link] *bold* - item\nC:\\tmp",
    )

    assert serialize_markdown(document).decode("utf-8") == (
        "# Transcription\n\n"
        "**Source:** meeting\\[1\\]\\.wav\n"
        "**Date:** 2024-01-02T00:04:05.123Z\n"
        "**Duration:** 1.250s\n"
        "**Variant:** Original\n\n"
        "\\# title\n"
        "\\[link\\] \\*bold\\* \\- item\n"
        "C:\\\\tmp\n"
    )


def test_close_cancels_late_admitted_job_and_waits_for_admission_worker(tmp_path: Path) -> None:
    media = FakeImportedMedia()
    media.submit_started = Event()
    media.allow_submit = Event()
    controller = TranscribePageController(media)
    source = tmp_path / "late.wav"
    source.write_bytes(b"content")
    controller.add_paths([source])
    assert controller.start_queue()
    assert media.submit_started.wait(1)
    assert controller.snapshot.can_cancel_all is False

    close_thread = Thread(target=controller.close)
    close_thread.start()
    wait_for(lambda: controller._closed)
    media.allow_submit.set()
    close_thread.join(2)

    assert not close_thread.is_alive()
    assert len(media.cancelled) == 1
    assert controller._admission_thread is None
    assert not controller._watchers


def test_controller_retry_restarts_admission_for_retryable_failure(tmp_path: Path) -> None:
    media = FakeImportedMedia()
    controller = TranscribePageController(media)
    source = tmp_path / "retry.wav"
    source.write_bytes(b"content")
    controller.add_paths([source])
    controller.start_queue()
    wait_for(lambda: len(media.jobs) == 1)
    first_job = next(iter(media.jobs))
    media.finish_failure(first_job, retryable=True)
    wait_for(lambda: controller.snapshot.items[0].state.value == "failed")

    assert controller.retry_item(controller.snapshot.items[0].item_id)
    wait_for(lambda: len(media.jobs) == 2)
    second_job = tuple(media.jobs)[1]
    assert second_job != first_job
    media.finish_success(second_job, "retried")
    wait_for(lambda: controller.snapshot.items[0].state.value == "succeeded")
    controller.close()


def test_controller_surfaces_end_of_stream_without_terminal_result(tmp_path: Path) -> None:
    media = FakeImportedMedia()
    controller = TranscribePageController(media)
    source = tmp_path / "stream-ended.wav"
    source.write_bytes(b"content")
    controller.add_paths([source])
    controller.start_queue()
    wait_for(lambda: len(media.jobs) == 1)

    media.finish_without_terminal(next(iter(media.jobs)))
    wait_for(lambda: controller.snapshot.items[0].state.value == "failed")

    failure = controller.snapshot.items[0].failure
    assert failure is not None
    assert failure.stage == "observation"
    assert "terminal" in failure.message
    controller.close()


def test_existing_import_service_observation_uses_backend_job_state(tmp_path: Path) -> None:
    source = tmp_path / "input.wav"
    source.write_bytes(b"media")
    service = ImportedMediaTranscriptionService(
        FakeMediaNormalizer(),
        AsrApplicationService(FakeAsrRuntime()),
        FakeSnapshotStore(tmp_path / "work"),
    )
    job_id = service.submit(str(source))
    observation = service.observe(job_id)
    first = observation.next(timeout=1)
    result = service.wait(job_id, timeout=2)
    terminal = observation.next(timeout=1)
    observation.close()
    service.close()

    assert first.progress.stage in {
        Stage.QUEUED,
        Stage.NORMALIZING,
        Stage.TRANSCRIBING,
        Stage.CLEANING_UP,
    }
    assert result.status == "succeeded"
    assert terminal.terminal.status == "succeeded"
