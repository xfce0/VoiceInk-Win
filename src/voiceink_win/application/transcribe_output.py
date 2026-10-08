"""Deterministic transcript output serialization and small application ports."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol

from voiceink_win.domain import TranscriptDocument, TranscriptVariant, resolve_variant


class ClipboardPort(Protocol):
    def copy(self, text: str) -> None: ...


class TextFilePort(Protocol):
    def write_atomic(self, target: Path, content: bytes) -> None: ...


def serialize_txt(
    document: TranscriptDocument,
    variant: TranscriptVariant | None = None,
) -> bytes:
    text = document.text_for(resolve_variant(document, variant, document.selected_variant))
    return _finalize(text).encode("utf-8")


def serialize_markdown(
    document: TranscriptDocument,
    variant: TranscriptVariant | None = None,
) -> bytes:
    selected = resolve_variant(document, variant, document.selected_variant)
    text = _escape_markdown(document.text_for(selected))
    variant_label = "Enhanced" if selected is TranscriptVariant.ENHANCED else "Original"
    source = _escape_markdown(document.source_name)
    content = (
        "# Transcription\n\n"
        f"**Source:** {source}\n"
        f"**Date:** {_escape_markdown(document.created_at)}\n"
        f"**Duration:** {document.duration_seconds:.3f}\n"
        f"**Variant:** {variant_label}\n\n"
        f"{text}"
    )
    return _finalize(content).encode("utf-8")


def _finalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def _escape_markdown(text: str) -> str:
    escaped_lines: list[str] = []
    for line in _finalize(text).rstrip("\n").split("\n"):
        escaped = line.replace("\\", "\\\\")
        for character in "`*_{}[]()#+-.!>|<~":
            escaped = escaped.replace(character, f"\\{character}")
        escaped_lines.append(escaped)
    return "\n".join(escaped_lines)


def atomic_write(target: Path, content: bytes) -> None:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


class LocalTextFilePort:
    def write_atomic(self, target: Path, content: bytes) -> None:
        atomic_write(target, content)
