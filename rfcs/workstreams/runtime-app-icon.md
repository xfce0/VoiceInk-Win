# RFC: Runtime Qt Application Icon

## Status

Approved / Implemented.

## Summary

Make the existing VoiceInk SVG/ICO assets available to the running Qt
application so the taskbar, title bar, and top-level windows use the same
identity as the packaged executable.

## Decision

- `QApplication.setWindowIcon()` is configured during shell startup.
- The created `MainWindow` receives the same icon explicitly.
- Runtime lookup prefers the generated `voiceink-shell.ico`, then the existing
  repository-owned `voiceink-shell.svg`.
- Frozen bundle, executable-adjacent, and source-checkout locations are tried.
- Missing or unusable assets return an empty `QIcon` and do not block startup.
- PyInstaller keeps its existing `--icon` executable metadata behavior and also
  bundles the generated ICO and SVG for runtime lookup.

## Scope

This does not change theme, layout, tray behavior, installer behavior, or the
icon generation pipeline. The fallback is intentionally non-visual: Qt keeps
its platform default when no repository asset can be loaded.

## Validation

Targeted tests cover the generated packaging command, SVG fallback, empty-icon
fallback, and frozen/source candidate locations.
