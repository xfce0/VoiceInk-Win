"""Test-only deterministic backend for enabled shell controller tests."""

from __future__ import annotations

from voiceink_win.domain import TranscriptResult


class FakeShellBackend:
    """Return one configured result without importing Qt or a native runtime."""

    def __init__(
        self, result: TranscriptResult | None = None, error: Exception | None = None
    ) -> None:
        self._result = result or TranscriptResult(
            text="VoiceInk is ready for your next thought.",
            duration=1.0,
            detected_language="en",
        )
        self._error = error
        self.calls = 0

    def transcribe(self) -> TranscriptResult:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result
