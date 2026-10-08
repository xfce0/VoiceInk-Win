"""Qt rendering for the repository-owned sidebar SVG icons."""

from __future__ import annotations

from functools import cache

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .icon_registry import LUCIDE_PATHS, SidebarItem

ICON_SCALE_FACTORS = (1, 2, 3, 4, 5)


@cache
def sidebar_icon(item: SidebarItem, size: int = 28) -> QIcon:
    """Create a high-DPI QIcon with all interaction states represented."""

    if size <= 0:
        raise ValueError("Icon size must be positive")

    icon = QIcon()
    for scale in ICON_SCALE_FACTORS:
        pixmap = _render(item, size, scale)
        for mode in (
            QIcon.Mode.Normal,
            QIcon.Mode.Active,
            QIcon.Mode.Selected,
            QIcon.Mode.Disabled,
        ):
            for state in (QIcon.State.Off, QIcon.State.On):
                icon.addPixmap(pixmap, mode, state)
    return icon


def sidebar_svg(item: SidebarItem, size: int = 28) -> str:
    """Build the repository-owned SVG source shared by every QIcon state."""
    if size <= 0:
        raise ValueError("Icon size must be positive")
    return f"""
    <svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 28 28">
      <rect x="1" y="1" width="26" height="26" rx="7" fill="{item.tile_color}"
            stroke="#ffffff" stroke-opacity=".28"/>
      <g transform="translate(2 2)" fill="none" stroke="{item.icon_foreground}" stroke-width="1.7"
         stroke-linecap="round" stroke-linejoin="round">{LUCIDE_PATHS[item.icon_name]}</g>
    </svg>
    """


def _render(item: SidebarItem, size: int, scale: int) -> QPixmap:
    """Rasterize repository-owned SVG paths at a logical size and explicit DPR."""
    physical_size = size * scale
    svg = sidebar_svg(item, size)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise ValueError(f"Invalid sidebar SVG for {item.icon_name}")
    pixmap = QPixmap(QSize(physical_size, physical_size))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap
