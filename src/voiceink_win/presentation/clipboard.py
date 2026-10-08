"""Non-blocking Qt clipboard boundary for application output actions."""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication


class QtClipboardPort(QObject):
    """Queue clipboard mutations onto the Qt GUI thread."""

    _copy_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._copy_requested.connect(self._copy_on_gui_thread, Qt.ConnectionType.QueuedConnection)

    def copy(self, text: str) -> None:
        self._copy_requested.emit(text)

    @Slot(str)
    def _copy_on_gui_thread(self, text: str) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is None:
            raise RuntimeError("Qt clipboard is unavailable")
        clipboard.setText(text)
