from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from voiceink_win.application import (
    HistoryMediaActionService,
    HistoryMediaCode,
    HistoryMediaState,
)
from voiceink_win.domain import (
    HistoryRecord,
    TranscriptionSource,
)
from voiceink_win.infrastructure import (
    AudioArtifactStore,
    WindowsHistoryArtifactRevealAdapter,
    WindowsHistoryAudioPlaybackAdapter,
)


class _FakePlayback:
    def __init__(
        self,
        available: bool = True,
        error: Exception | None = None,
        availability_error: Exception | None = None,
    ) -> None:
        self.available = available
        self.error = error
        self.availability_error = availability_error
        self.paths: list[Path] = []
        self.availability_checks = 0

    def is_available(self) -> bool:
        self.availability_checks += 1
        if self.availability_error is not None:
            raise self.availability_error
        return self.available

    def play(self, path: Path, on_failure=None) -> None:
        self.paths.append(path)
        if self.error is not None:
            raise self.error


class _FakeReveal:
    def __init__(self, available: bool = True, availability_error: Exception | None = None) -> None:
        self.available = available
        self.availability_error = availability_error
        self.paths: list[Path] = []

    def is_available(self) -> bool:
        if self.availability_error is not None:
            raise self.availability_error
        return self.available

    def reveal(self, path: Path) -> None:
        self.paths.append(path)


class _FakeArtifacts:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.references: list[str] = []
        self.fail = False
        self.error: Exception | None = None

    def reveal_path(self, reference: str) -> Path:
        self.references.append(reference)
        if self.error is not None:
            raise self.error
        if self.fail:
            raise FileNotFoundError(reference)
        return self.path


class _TestWindowsAdapter:
    def validate_source(self, _path: Path) -> None:
        pass

    def cleanup_workspace(self, _path: Path) -> None:
        pass

    def delete_artifact(self, _root: Path, _relative_path: str) -> None:
        pass


def _artifact_store(path: Path) -> AudioArtifactStore:
    adapter = _TestWindowsAdapter() if os.name == "nt" else None
    return AudioArtifactStore(path, windows_adapter=adapter)


def _record(artifact: str | None = "history/item.wav") -> HistoryRecord:
    return HistoryRecord(
        id="history-action-test",
        source=TranscriptionSource.IMPORTED_FILE,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        original_text="private transcript",
        source_metadata={"folder_reference": "source-folder-token"},
        audio_artifact_path=artifact,
    )


def test_actions_resolve_and_pass_the_same_stored_artifact(tmp_path: Path) -> None:
    store = _artifact_store(tmp_path / "audio")
    store.write("history/item.wav", b"wav")
    playback = _FakePlayback()
    reveal = _FakeReveal()
    service = HistoryMediaActionService(store, playback, reveal)
    record = _record("history/item.wav")

    availability = service.inspect(record)
    play_result = service.play(record)
    reveal_result = service.reveal(record)
    expected = tmp_path / "audio" / "history" / "item.wav"

    assert availability.audio.state is HistoryMediaState.AVAILABLE
    assert availability.reveal.state is HistoryMediaState.AVAILABLE
    assert play_result.code is HistoryMediaCode.STARTED
    assert reveal_result.code is HistoryMediaCode.STARTED
    assert playback.paths == [expected]
    assert reveal.paths == [expected]
    assert playback.paths == reveal.paths


@pytest.mark.parametrize(
    ("artifact", "setup"),
    [
        (None, lambda _store: None),
        ("history/missing.wav", lambda _store: None),
        ("../outside.wav", lambda _store: None),
        (
            "history/link.wav",
            lambda store: (
                (store.folder_path / "history").mkdir(exist_ok=True)
                or (store.folder_path / "history/link.wav").symlink_to("/tmp")
            ),
        ),
        (
            "history/directory",
            lambda store: (
                (store.folder_path / "history").mkdir(exist_ok=True)
                or (store.folder_path / "history" / "directory").mkdir()
            ),
        ),
    ],
)
def test_invalid_or_missing_artifacts_never_call_platform_ports(
    tmp_path: Path, artifact: str | None, setup
) -> None:
    store = _artifact_store(tmp_path / "audio")
    setup(store)
    playback = _FakePlayback()
    reveal = _FakeReveal()
    service = HistoryMediaActionService(store, playback, reveal)

    availability = service.inspect(_record(artifact))
    result = service.play(_record(artifact))

    assert availability.audio.code in {
        HistoryMediaCode.NO_ARTIFACT,
        HistoryMediaCode.ARTIFACT_MISSING_OR_INVALID,
    }
    assert result.code is availability.audio.code
    assert playback.paths == []
    assert reveal.paths == []


def test_unavailable_platform_is_truthful_and_does_not_invoke_action(tmp_path: Path) -> None:
    store = _artifact_store(tmp_path / "audio")
    store.write("history/item.wav", b"wav")
    playback = _FakePlayback(available=False)
    service = HistoryMediaActionService(store, playback, _FakeReveal(available=False))

    availability = service.inspect(_record())
    result = service.play(_record())

    assert availability.audio.code is HistoryMediaCode.PLATFORM_UNAVAILABLE
    assert availability.reveal.code is HistoryMediaCode.PLATFORM_UNAVAILABLE
    assert result.code is HistoryMediaCode.PLATFORM_UNAVAILABLE
    assert playback.paths == []


def test_adapter_failure_is_safe_and_does_not_log_paths_or_exception_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    artifact = tmp_path / "private" / "item.wav"
    store = _artifact_store(tmp_path / "audio")
    store.write("history/item.wav", b"wav")
    error = RuntimeError(f"cannot play {artifact}: private transcript")
    service = HistoryMediaActionService(store, _FakePlayback(error=error), _FakeReveal())

    with caplog.at_level(logging.INFO):
        result = service.play(_record())

    assert result.code is HistoryMediaCode.OPERATION_FAILED
    assert str(artifact) not in caplog.text
    assert "private transcript" not in caplog.text
    assert any(
        getattr(record, "reason_code", None) == HistoryMediaCode.OPERATION_FAILED.value
        for record in caplog.records
    )
    operation_log = next(
        record
        for record in caplog.records
        if getattr(record, "reason_code", None) == HistoryMediaCode.OPERATION_FAILED.value
    )
    assert operation_log.failure_stage == "operation"
    assert operation_log.exception_type == "RuntimeError"


def test_capability_failure_is_unavailable_and_structured_for_play(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = _artifact_store(tmp_path / "audio")
    store.write("history/item.wav", b"wav")
    service = HistoryMediaActionService(
        store,
        _FakePlayback(availability_error=RuntimeError("player unavailable")),
        _FakeReveal(),
    )

    with caplog.at_level(logging.INFO):
        result = service.play(_record())

    assert result.code is HistoryMediaCode.PLATFORM_UNAVAILABLE
    capability_log = next(
        record
        for record in caplog.records
        if getattr(record, "failure_stage", None) == "capability"
    )
    assert capability_log.reason_code == HistoryMediaCode.PLATFORM_UNAVAILABLE.value
    assert capability_log.exception_type == "RuntimeError"


def test_capability_failure_is_unavailable_and_structured_for_reveal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = _artifact_store(tmp_path / "audio")
    store.write("history/item.wav", b"wav")
    service = HistoryMediaActionService(
        store,
        _FakePlayback(),
        _FakeReveal(availability_error=RuntimeError("explorer unavailable")),
    )

    with caplog.at_level(logging.INFO):
        result = service.reveal(_record())

    assert result.code is HistoryMediaCode.PLATFORM_UNAVAILABLE
    capability_log = next(
        record
        for record in caplog.records
        if getattr(record, "failure_stage", None) == "capability"
    )
    assert capability_log.reason_code == HistoryMediaCode.PLATFORM_UNAVAILABLE.value
    assert capability_log.exception_type == "RuntimeError"


def test_action_revalidates_after_inspection(tmp_path: Path) -> None:
    store = _FakeArtifacts(tmp_path / "history" / "item.wav")
    playback = _FakePlayback()
    service = HistoryMediaActionService(store, playback, _FakeReveal())
    record = _record()

    assert service.inspect(record).audio.available
    store.fail = True

    result = service.play(record)

    assert result.code is HistoryMediaCode.ARTIFACT_MISSING_OR_INVALID
    assert playback.paths == []
    assert store.references == [record.audio_artifact_path, record.audio_artifact_path]


def test_reveal_revalidates_after_inspection(tmp_path: Path) -> None:
    store = _FakeArtifacts(tmp_path / "history" / "item.wav")
    reveal = _FakeReveal()
    service = HistoryMediaActionService(store, _FakePlayback(), reveal)
    record = _record()

    assert service.inspect(record).reveal.available
    store.fail = True

    result = service.reveal(record)

    assert result.code is HistoryMediaCode.ARTIFACT_MISSING_OR_INVALID
    assert reveal.paths == []


def test_artifact_resolution_failure_is_unavailable(tmp_path: Path, caplog) -> None:
    store = _FakeArtifacts(tmp_path / "history" / "item.wav")
    store.error = PermissionError("artifact lookup denied")
    service = HistoryMediaActionService(store, _FakePlayback(), _FakeReveal())

    with caplog.at_level(logging.INFO):
        availability = service.inspect(_record())
        result = service.play(_record())

    assert availability.audio.state is HistoryMediaState.UNAVAILABLE
    assert availability.audio.code is HistoryMediaCode.ARTIFACT_MISSING_OR_INVALID
    assert result.code is HistoryMediaCode.ARTIFACT_MISSING_OR_INVALID
    assert any(
        getattr(record, "failure_stage", None) == "resolve"
        and getattr(record, "exception_type", None) == "PermissionError"
        for record in caplog.records
    )


def test_windows_reveal_adapter_selects_the_exact_artifact() -> None:
    commands: list[list[str]] = []
    adapter = WindowsHistoryArtifactRevealAdapter(
        platform_name="nt",
        launcher=commands.append,
        explorer_path=r"C:\Windows\explorer.exe",
    )
    artifact = Path(r"C:\Users\Test User\VoiceInk\аудио\history\item.wav")

    assert adapter.is_available()
    adapter.reveal(artifact)

    assert commands == [
        [
            r"C:\Windows\explorer.exe",
            r'/select,"C:\Users\Test User\VoiceInk\аудио\history\item.wav"',
        ]
    ]


def test_windows_reveal_adapter_uses_the_windows_directory(monkeypatch) -> None:
    import ctypes

    class Kernel32:
        @staticmethod
        def GetWindowsDirectoryW(buffer, _size) -> int:
            buffer.value = r"C:\Windows"
            return len(buffer.value)

    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=Kernel32()), raising=False)
    monkeypatch.setattr(Path, "is_file", lambda _path: True)
    commands: list[list[str]] = []
    adapter = WindowsHistoryArtifactRevealAdapter(platform_name="nt", launcher=commands.append)

    adapter.reveal(Path(r"C:\Users\Test User\VoiceInk\audio\item.wav"))

    assert commands[0][0] == str(Path(r"C:\Windows") / "explorer.exe")


def test_windows_audio_adapter_uses_the_exact_artifact() -> None:
    class Signal:
        def connect(self, _callback) -> None:
            pass

    class Player:
        def __init__(self) -> None:
            self.output = None
            self.source = None
            self.play_calls = 0
            self.errorOccurred = Signal()

        def setAudioOutput(self, output) -> None:
            self.output = output

        def setSource(self, source) -> None:
            self.source = source

        def play(self) -> None:
            self.play_calls += 1

        def playbackState(self):
            return "PlayingState"

        def stop(self) -> None:
            pass

    player = Player()
    output = object()
    adapter = WindowsHistoryAudioPlaybackAdapter(
        platform_name="nt",
        player_factory=lambda: player,
        audio_output_factory=lambda: output,
        url_factory=lambda value: value,
        empty_url_factory=lambda: None,
    )
    artifact = Path(r"C:\Users\Test User\VoiceInk\аудио\history\item.wav")

    assert adapter.is_available()
    adapter.play(artifact)

    assert player.output is output
    assert player.source == str(artifact)
    assert player.play_calls == 1
    adapter.close()
    assert player.source is None


def test_windows_audio_adapter_reports_asynchronous_error() -> None:
    class Signal:
        def __init__(self) -> None:
            self.callback = None

        def connect(self, callback) -> None:
            self.callback = callback

        def emit(self) -> None:
            assert self.callback is not None
            self.callback(None, "backend failure")

    class Player:
        def __init__(self) -> None:
            self.errorOccurred = Signal()

        def setAudioOutput(self, _output) -> None:
            pass

        def setSource(self, _source) -> None:
            pass

        def play(self) -> None:
            pass

        def playbackState(self):
            return "PlayingState"

        def stop(self) -> None:
            pass

    player = Player()
    adapter = WindowsHistoryAudioPlaybackAdapter(
        platform_name="nt",
        player_factory=lambda: player,
        audio_output_factory=object,
        url_factory=lambda value: value,
    )

    failures: list[Exception] = []
    adapter.set_failure_callback(failures.append)
    adapter.play(Path(r"C:\Windows\Temp\item.wav"))
    player.errorOccurred.emit()

    assert [str(error) for error in failures] == ["history audio playback failed"]


def test_windows_audio_adapter_initialization_failure_is_unavailable() -> None:
    def fail_factory():
        raise ValueError("unexpected Qt factory failure")

    adapter = WindowsHistoryAudioPlaybackAdapter(
        platform_name="nt",
        player_factory=fail_factory,
        audio_output_factory=object,
        url_factory=lambda value: value,
    )

    assert not adapter.is_available()


def test_windows_adapters_are_unavailable_off_windows() -> None:
    reveal = WindowsHistoryArtifactRevealAdapter(platform_name="posix")
    audio = WindowsHistoryAudioPlaybackAdapter(platform_name="posix")

    assert not reveal.is_available()
    assert not audio.is_available()
