# Architecture Overview

## Status

Proposed. This document is the baseline architecture for the first implementation phase.

## Layers

```text
Presentation -> Application -> Domain <- Infrastructure adapters
```

- Presentation: PySide6/Qt windows, system tray, recording overlay, drag-and-drop, and view models.
- Application: recording sessions, file-import queue, transcription workflow, cancellation, enhancement, history, and text delivery orchestration.
- Domain: immutable data models, ports, state transitions, mode rules, and validation.
- Infrastructure: WASAPI capture, FFmpeg conversion, Parakeet runtime, SQLite, Windows Credential Manager/DPAPI, global shortcuts, clipboard, and `SendInput`.

## ASR Runtime

The initial production path is an isolated `NeMo-Speech.cpp` runtime using Parakeet TDT 0.6B V3 GGUF. The Python application communicates with it through a narrow adapter. A CPU build and an NVIDIA CUDA build are supported. The runtime must not leak subprocess, HTTP, or CUDA details into the domain layer.

## Audio Contract

Microphone recordings and imported media converge to mono 16 kHz PCM before transcription. FFmpeg is the only media conversion boundary. The supported format list and conversion errors are specified by the file-transcription feature.

## Persistence

SQLite stores transcription metadata, raw text, enhanced text, timestamps, model identifiers, and processing status. Schema changes require migrations. Secrets are not stored in SQLite or JSON settings.

## Open Decisions

- Whether the first live mode uses final transcription on key release or a separate streaming model.
- Whether the ASR runtime is embedded through a native SDK or kept as a local service after the MVP.
- Installer technology and update channel.
- Exact supported NVIDIA driver and GPU matrix.
