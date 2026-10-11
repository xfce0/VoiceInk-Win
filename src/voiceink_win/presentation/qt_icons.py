"""Qt rendering for repository-owned sidebar vector and branding icons."""

from __future__ import annotations

from functools import cache

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from .app_icon import sidebar_icon_paths
from .icon_registry import LUCIDE_PATHS, SidebarItem

ICON_SCALE_FACTORS = (1, 1.25, 1.5, 2, 2.5, 3)


@cache
def sidebar_icon(item: SidebarItem, size: int = 28) -> QIcon:
    """Create a high-DPI QIcon with all interaction states represented."""

    if size <= 0:
        raise ValueError("Icon size must be positive")

    icon = QIcon()
    for scale in ICON_SCALE_FACTORS:
        pixmap = _render(item, size, scale)
        muted_pixmap = _render(item, size, scale, disabled=True)
        for state in (QIcon.State.Off, QIcon.State.On):
            for mode in (QIcon.Mode.Normal, QIcon.Mode.Active, QIcon.Mode.Selected):
                icon.addPixmap(pixmap, mode, state)
            icon.addPixmap(muted_pixmap, QIcon.Mode.Disabled, state)
    return icon


def sidebar_svg(item: SidebarItem, size: int = 28, *, disabled: bool = False) -> str:
    """Build the repository-owned SVG source for one QIcon interaction state."""
    if size <= 0:
        raise ValueError("Icon size must be positive")
    tile_color = _muted_color(item.tile_color) if disabled else item.tile_color
    icon_foreground = _muted_color(item.icon_foreground) if disabled else item.icon_foreground
    return f"""
    <svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 28 28">
      <rect x="1" y="1" width="26" height="26" rx="7" fill="{tile_color}"
            stroke="#ffffff" stroke-opacity=".28"/>
      <g transform="translate(2 2)" fill="none" stroke="{icon_foreground}" stroke-width="1.7"
          stroke-linecap="round" stroke-linejoin="round">{LUCIDE_PATHS[item.icon_name]}</g>
    </svg>
    """


def _render(item: SidebarItem, size: int, scale: float, *, disabled: bool = False) -> QPixmap:
    """Rasterize repository-owned SVG paths at a logical size and explicit DPR."""
    if item.asset_filename is not None:
        return _render_asset(item, size, scale, disabled=disabled)

    physical_size = round(size * scale)
    svg = sidebar_svg(item, size, disabled=disabled)
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


def _render_asset(item: SidebarItem, size: int, scale: float, *, disabled: bool) -> QPixmap:
    """Rasterize a repository-owned branding asset at a logical size and DPR."""
    asset_filename = item.asset_filename
    if asset_filename is None:
        raise ValueError(f"Missing sidebar asset name for {item.icon_name}")
    physical_size = round(size * scale)
    source = next(
        (path for path in sidebar_icon_paths() if path.is_file()),
        None,
    )
    if source is None:
        raise ValueError(f"Missing sidebar asset for {item.icon_name}: {asset_filename}")

    pixmap = QPixmap(QSize(physical_size, physical_size))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    if source.suffix.lower() == ".svg":
        renderer = QSvgRenderer(str(source))
        if not renderer.isValid():
            painter.end()
            raise ValueError(f"Invalid sidebar asset for {item.icon_name}: {source}")
        renderer.render(painter)
    else:
        source_pixmap = QPixmap(str(source))
        if source_pixmap.isNull():
            painter.end()
            raise ValueError(f"Invalid sidebar asset for {item.icon_name}: {source}")
        painter.drawPixmap(
            pixmap.rect(),
            source_pixmap.scaled(
                pixmap.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ),
        )
    if disabled:
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor(_muted_color(item.icon_foreground)))
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap


def _muted_color(value: str) -> str:
    color = QColor(value)
    if not color.isValid():
        raise ValueError(f"Invalid sidebar color: {value}")
    hue, saturation, lightness, alpha = color.getHslF()
    if hue < 0:
        hue = 0.0
    color.setHslF(hue, saturation * 0.18, 0.52 + (lightness - 0.52) * 0.55, alpha)
    return color.name(QColor.NameFormat.HexRgb)
