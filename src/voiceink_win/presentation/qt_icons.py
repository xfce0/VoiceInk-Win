"""Qt rendering for the repository-owned sidebar SVG icons."""

from __future__ import annotations

from functools import cache

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .icon_registry import LUCIDE_PATHS, SidebarItem


@cache
def sidebar_icon(item: SidebarItem, size: int = 28) -> QIcon:
    """Create a real QIcon with normal and disabled SVG-rendered pixmaps."""

    if size <= 0:
        raise ValueError("Icon size must be positive")

    icon = QIcon()
    icon.addPixmap(_render(item, size, item.tile_color, "#ffffff"), QIcon.Mode.Normal)
    icon.addPixmap(_render(item, size, "#62646d", "#d8d9de"), QIcon.Mode.Disabled)
    return icon


def _render(item: SidebarItem, size: int, tile_color: str, foreground: str) -> QPixmap:
    svg = f"""
    <svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 28 28">
      <rect x="1" y="1" width="26" height="26" rx="7" fill="{tile_color}"
            stroke="#ffffff" stroke-opacity=".28"/>
      <g transform="translate(2 2)" fill="none" stroke="{foreground}" stroke-width="1.7"
         stroke-linecap="round" stroke-linejoin="round">{LUCIDE_PATHS[item.icon_name]}</g>
    </svg>
    """
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise ValueError(f"Invalid sidebar SVG for {item.icon_name}")
    pixmap = QPixmap(QSize(size, size))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return pixmap
