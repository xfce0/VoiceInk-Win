from __future__ import annotations

from tests.support.fake_shell import FakeShellBackend
from voiceink_win.application import ShellController
from voiceink_win.domain import (
    RuntimeUnavailableError,
    ShellState,
    TranscriptResult,
)


def test_unavailable_shell_is_stable_and_does_not_publish_noop_actions() -> None:
    controller = ShellController.unavailable()
    snapshots = []
    controller.subscribe(snapshots.append)

    assert controller.snapshot.state is ShellState.UNAVAILABLE
    assert controller.snapshot.transcript == ""
    assert controller.snapshot.error == ""
    assert controller.start_recording() is False
    assert controller.stop_recording() is False
    assert controller.complete_processing() is False
    assert controller.reset() is None

    assert controller.snapshot.state is ShellState.UNAVAILABLE
    assert snapshots == []
    assert controller._backend is None


def test_shell_starts_idle_without_transcript() -> None:
    controller = ShellController(FakeShellBackend())

    assert controller.snapshot.state is ShellState.IDLE
    assert controller.snapshot.transcript == ""
    assert controller.snapshot.error == ""


def test_shell_publishes_recording_processing_and_ready_states() -> None:
    backend = FakeShellBackend(
        TranscriptResult("A finished thought.", duration=1.0, detected_language="en")
    )
    controller = ShellController(backend)
    snapshots = []
    controller.subscribe(snapshots.append)

    assert controller.start_recording()
    assert controller.stop_recording()
    assert controller.complete_processing()

    assert [snapshot.state for snapshot in snapshots] == [
        ShellState.RECORDING,
        ShellState.PROCESSING,
        ShellState.TRANSCRIPT_READY,
    ]
    assert controller.snapshot.transcript == "A finished thought."
    assert backend.calls == 1


def test_shell_publishes_empty_state_for_silence() -> None:
    controller = ShellController(FakeShellBackend(TranscriptResult("  ", duration=1.0)))

    controller.start_recording()
    controller.stop_recording()
    controller.complete_processing()

    assert controller.snapshot.state is ShellState.EMPTY
    assert controller.snapshot.transcript == ""


def test_shell_publishes_typed_runtime_error_as_visible_state() -> None:
    controller = ShellController(
        FakeShellBackend(error=RuntimeUnavailableError("Local runtime is not configured."))
    )

    controller.start_recording()
    controller.stop_recording()
    controller.complete_processing()

    assert controller.snapshot.state is ShellState.ERROR
    assert controller.snapshot.error == (
        "Local transcription is unavailable. Check the runtime and try again."
    )


def test_shell_maps_unexpected_backend_failure_to_safe_message() -> None:
    controller = ShellController(FakeShellBackend(error=RuntimeError("private path")))

    controller.start_recording()
    controller.stop_recording()
    controller.complete_processing()

    assert controller.snapshot.state is ShellState.ERROR
    assert controller.snapshot.error == "Transcription failed. Check the runtime and try again."


def test_shell_ignores_invalid_actions_and_does_not_call_backend() -> None:
    backend = FakeShellBackend()
    controller = ShellController(backend)

    assert not controller.stop_recording()
    assert not controller.complete_processing()
    assert controller.start_recording()
    assert not controller.start_recording()
    assert controller.stop_recording()
    assert controller.complete_processing()
    assert not controller.complete_processing()
    assert backend.calls == 1


def test_shell_unsubscribe_stops_state_notifications() -> None:
    controller = ShellController(FakeShellBackend())
    snapshots = []
    unsubscribe = controller.subscribe(snapshots.append)

    controller.start_recording()
    unsubscribe()
    controller.stop_recording()

    assert [snapshot.state for snapshot in snapshots] == [ShellState.RECORDING]


def test_shell_can_start_again_after_ready_empty_or_error() -> None:
    for backend in (
        FakeShellBackend(),
        FakeShellBackend(TranscriptResult("", duration=1.0)),
        FakeShellBackend(error=RuntimeUnavailableError("unavailable")),
    ):
        controller = ShellController(backend)
        controller.start_recording()
        controller.stop_recording()
        controller.complete_processing()

        assert controller.start_recording()
        assert controller.snapshot.state is ShellState.RECORDING


def test_shell_reset_returns_to_idle() -> None:
    controller = ShellController(FakeShellBackend())

    controller.start_recording()
    controller.stop_recording()
    controller.complete_processing()
    controller.reset()

    assert controller.snapshot.state is ShellState.IDLE
    assert controller.snapshot.transcript == ""
    assert controller.snapshot.error == ""
