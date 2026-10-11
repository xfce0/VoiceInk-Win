"""Convert the repository-owned PNG into a multi-size Windows ICO."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, QSize, Qt
from PySide6.QtGui import QImage, QPainter

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _render_png(source: QImage, size: int) -> bytes:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        scaled = source.scaled(
            QSize(size, size),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (size - scaled.width()) // 2
        y = (size - scaled.height()) // 2
        painter.drawImage(x, y, scaled)
    finally:
        painter.end()

    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise RuntimeError("Unable to open an in-memory PNG buffer")
    try:
        if not image.save(buffer, "PNG"):
            raise RuntimeError(f"Unable to encode {size}x{size} PNG data")
        return bytes(buffer.data())
    finally:
        buffer.close()


def _write_ico(output: Path, images: list[tuple[int, bytes]]) -> None:
    directory_size = 6 + 16 * len(images)
    offset = directory_size
    directory = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    payload = bytearray()

    for size, image in images:
        dimension = 0 if size == 256 else size
        directory.extend(
            struct.pack(
                "<BBBBHHII",
                dimension,
                dimension,
                0,
                0,
                1,
                32,
                len(image),
                offset,
            )
        )
        payload.extend(image)
        offset += len(image)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(directory + payload)


def build_icon(source: Path, output: Path) -> None:
    source_image = QImage(str(source))
    if source_image.isNull():
        raise ValueError(f"Invalid PNG icon source: {source}")
    _write_ico(output, [(size, _render_png(source_image, size)) for size in ICON_SIZES])


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {Path(sys.argv[0]).name} SOURCE.png OUTPUT.ico", file=sys.stderr)
        return 2
    try:
        build_icon(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"icon build: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
