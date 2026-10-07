"""Small Qt widgets that are independent from application orchestration."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget


class WaveformWidget(QWidget):
    """A restrained 15-bar waveform matching the compact macOS recorder panel."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._active = False
        self._frame = 0
        self._timer = QTimer(self)
        self._timer.setInterval(90)
        self._timer.timeout.connect(self._advance)
        self.setMinimumSize(88, 32)

    def sizeHint(self) -> QSize:
        return QSize(112, 32)

    def set_active(self, active: bool) -> None:
        if self._active == active:
            return
        self._active = active
        if active:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _advance(self) -> None:
        self._frame += 1
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ARG002
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#f5f5f5"))

        bar_count = 15
        bar_width = 3
        spacing = 3
        total_width = bar_count * bar_width + (bar_count - 1) * spacing
        x_start = (self.width() - total_width) / 2
        center = self.height() / 2
        for index in range(bar_count):
            if self._active:
                phase = (self._frame + index * 2) % 12
                height = 7 + abs(phase - 6) * 2
            else:
                height = 4
            y = center - height / 2
            painter.drawRoundedRect(
                QRectF(x_start + index * (bar_width + spacing), y, bar_width, height),
                1.5,
                1.5,
            )
        painter.end()
