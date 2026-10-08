"""Non-blocking Qt clipboard boundary for application output actions."""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication


class QtClipboardPort(QObject):
    """Queue clipboard mutations onto the Qt GUI thread."""

    _copy_requested = Signal(str, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._copy_requested.connect(self._copy_on_gui_thread, Qt.ConnectionType.QueuedConnection)

    def copy(self, text: str, completion) -> None:
        self._copy_requested.emit(text, completion)

    @Slot(str, object)
    def _copy_on_gui_thread(self, text: str, completion) -> None:
        try:
            clipboard = QApplication.clipboard()
            if clipboard is None:
                raise RuntimeError("Qt clipboard is unavailable")
            clipboard.setText(text)
        except BaseException as error:
            completion(error)
        else:
            completion(None)
