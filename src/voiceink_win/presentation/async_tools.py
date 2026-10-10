"""Helpers for delivering background Future results on the Qt thread."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from typing import TypeVar

from PySide6.QtCore import QObject, Qt, Signal, Slot

Result = TypeVar("Result")


class FutureBridge(QObject):
    """Turn a concurrent Future completion into a queued Qt callback."""

    _completed = Signal(object, object)
    _queued = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self._queued.connect(self._deliver_queued, Qt.ConnectionType.QueuedConnection)

    def watch(
        self,
        future: Future[Result],
        callback: Callable[[Result | None, BaseException | None], None],
    ) -> None:
        future.add_done_callback(lambda completed: self._completed.emit(completed, callback))

    @Slot(object, object)
    def _deliver(self, future: Future[Result], callback: Callable) -> None:
        try:
            result = future.result()
        except BaseException as error:
            callback(None, error)
        else:
            callback(result, None)

    def post(self, callback: Callable[[], None]) -> None:
        self._queued.emit(callback)

    @Slot(object)
    def _deliver_queued(self, callback: Callable[[], None]) -> None:
        callback()
