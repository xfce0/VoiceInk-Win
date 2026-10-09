from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

import voiceink_win.desktop_composition as desktop_composition
from voiceink_win.application import ShellController
from voiceink_win.domain import ShellState
from voiceink_win.presentation.app import _run_session


def test_builder_has_exact_no_argument_api_and_returns_unavailable_controller() -> None:
    assert inspect.signature(desktop_composition.build_desktop_composition).parameters == {}

    composition = desktop_composition.build_desktop_composition()

    assert isinstance(composition.controller, ShellController)
    assert composition.controller.snapshot.state is ShellState.UNAVAILABLE
    assert composition.close() is None
    assert composition.close() is None


class _CompositionFake:
    def __init__(self) -> None:
        self.close_calls = 0

    @property
    def controller(self) -> ShellController:
        return ShellController.unavailable()

    def close(self) -> None:
        self.close_calls += 1


class _WindowFake:
    def __init__(self, events: list[str], show_error: BaseException | None = None) -> None:
        self._events = events
        self._show_error = show_error

    def show(self) -> None:
        self._events.append("show")
        if self._show_error is not None:
            raise self._show_error


def test_session_runner_preserves_normal_exit_code_and_closes_once() -> None:
    composition = _CompositionFake()
    events: list[str] = []

    result = _run_session(
        composition,
        lambda: _WindowFake(events),
        lambda: events.append("exec") or 17,
    )

    assert result == 17
    assert events == ["show", "exec"]
    assert composition.close_calls == 1


def test_session_runner_closes_and_reraises_window_exception() -> None:
    composition = _CompositionFake()
    error = RuntimeError("window construction failed")

    with pytest.raises(RuntimeError) as raised:
        _run_session(
            composition,
            lambda: (_ for _ in ()).throw(error),
            lambda: 0,
        )

    assert raised.value is error
    assert composition.close_calls == 1


def test_session_runner_closes_and_reraises_show_exception() -> None:
    composition = _CompositionFake()
    error = RuntimeError("show failed")

    with pytest.raises(RuntimeError) as raised:
        _run_session(
            composition,
            lambda: _WindowFake([], show_error=error),
            lambda: 0,
        )

    assert raised.value is error
    assert composition.close_calls == 1


def test_session_runner_closes_and_reraises_event_loop_exception() -> None:
    composition = _CompositionFake()
    error = ValueError("event loop failed")

    with pytest.raises(ValueError) as raised:
        _run_session(
            composition,
            lambda: _WindowFake([]),
            lambda: (_ for _ in ()).throw(error),
        )

    assert raised.value is error
    assert composition.close_calls == 1


def test_session_runner_preserves_session_error_when_window_cleanup_fails() -> None:
    composition = _CompositionFake()
    session_error = ValueError("event loop failed")

    def fail_cleanup(_window: _WindowFake) -> None:
        raise RuntimeError("window cleanup failed")

    with pytest.raises(ValueError) as raised:
        _run_session(
            composition,
            lambda: _WindowFake([]),
            lambda: (_ for _ in ()).throw(session_error),
            cleanup_window=fail_cleanup,
        )

    assert raised.value is session_error
    assert composition.close_calls == 1


def test_production_composition_and_entrypoint_do_not_reference_fake_shell() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "voiceink_win"

    assert "FakeShellBackend" not in (source_root / "desktop_composition.py").read_text()
    assert "FakeShellBackend" not in (source_root / "presentation" / "app.py").read_text()
    assert not (source_root / "infrastructure" / "fake_shell.py").exists()


def test_production_builder_imports_persistence_without_runtime_or_fake_shell() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(source_root), environment.get("PYTHONPATH", ""))
    )
    script = """
import sys
from voiceink_win.desktop_composition import build_desktop_composition

composition = build_desktop_composition()
assert composition.controller.snapshot.state.value == "unavailable"
assert composition.persistence is not None
composition.close()
assert any(name == "voiceink_win.infrastructure.sqlite_persistence" for name in sys.modules)
assert not any(name.endswith("fake_shell") for name in sys.modules)
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
