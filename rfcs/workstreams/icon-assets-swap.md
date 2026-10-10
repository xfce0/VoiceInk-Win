# RFC: Icon Asset Role Swap

## Status

Proposed. This RFC records the correction only; implementation is intentionally
deferred.

## Summary

The two existing VoiceInk assets are currently assigned to the wrong product
roles. Swap their roles without creating, redrawing, recoloring, or sourcing a
new brand asset:

| Product role | Current assignment | Corrected assignment |
| --- | --- | --- |
| `Transcribe` sidebar icon | `packaging/voiceink-shell-windows-x64/voiceink-transcribe.png` | `packaging/voiceink-shell-windows-x64/voiceink-shell.svg` |
| Qt application icon and Windows executable icon | generated `voiceink-shell.ico`, derived from `voiceink-shell.svg` | `packaging/voiceink-shell-windows-x64/voiceink-transcribe.png`, with a generated `voiceink-transcribe.ico` for PE metadata |

The filenames should follow the corrected roles after implementation. The
existing SVG and PNG bytes remain unchanged.

## Evidence and Current Pipeline

The current presentation registry sets `SidebarItem("Transcribe", ...,
"voiceink-transcribe.png")`. `qt_icons._render_asset()` resolves that file
through `branding_asset_paths()` and rasterizes it for all sidebar states and
device-pixel-ratio scales.

The current application path is separate: `application_icon()` resolves
`voiceink-shell.ico`, then `voiceink-shell.svg`; `app.py` installs that icon on
`QApplication` and copies it to `MainWindow`. The Windows frontend builder
passes the generated `voiceink-shell.ico` to PyInstaller's `--icon` option and
bundles the ICO, SVG, and PNG as runtime data in both the GUI and smoke
executables.

## Exact Source/Target Mapping

### Source A: repository-owned Windows SVG

- Source: `packaging/voiceink-shell-windows-x64/voiceink-shell.svg`.
- Current role: source for the application ICO and runtime application-icon
  fallback.
- Corrected role: `Transcribe` sidebar asset.
- Current SHA-256: `618c279c438c7e32cf7cc5c99e60614748be4117c4c3917609ab87d83ffdeaa1`.
- Visual identity: 28x28 viewBox, red `#db594b` rounded tile, white waveform
  path. The SVG is a tracked Windows asset, not a new design for this RFC.

### Source B: copied macOS VoiceInk template PNG

- Source of provenance: `/Users/paulosipov/VoiceInk/VoiceInk/Assets.xcassets/menuBarIcon.imageset/menuBarIcon.png`.
- Repository copy: `packaging/voiceink-shell-windows-x64/voiceink-transcribe.png`.
- Current role: `Transcribe` sidebar asset.
- Corrected role: Qt application icon source and source for the generated
  Windows ICO.
- SHA-256 of both the macOS source and repository copy:
  `de11e5550a84a03094f4cc60c6aff71045b67ccd020d142d6e379795c32ce0b0`.
- Format: byte-identical 750x750 indexed PNG with transparent background and
  the black VoiceInk microphone/template mark.

The macOS `AppIcon.appiconset` is a separate application-icon family. It is
explicitly excluded: this correction swaps the two existing repository assets
and must not introduce an AppIcon derivative or invent another brand variant.

## Runtime Implications

1. `SIDEBAR_ITEMS` must point `Transcribe` at `voiceink-shell.svg`. The
   sidebar renderer must rasterize that SVG through the same candidate-root
   policy as the other branded asset; it must preserve normal, active,
   selected, and disabled states and all existing high-DPI scale factors.
2. The application-icon registry must resolve
   `voiceink-transcribe.png` first at frozen-bundle, executable-adjacent, and
   source-checkout locations. `QApplication.setWindowIcon()` and the explicit
   `MainWindow` icon assignment remain the only runtime consumers.
3. Missing or unreadable assets must retain the existing non-blocking behavior:
   the sidebar should fail visibly in its existing validation path, while the
   top-level application icon loader may return an empty `QIcon` rather than
   blocking startup.
4. The implementation must remove role-confusing constants and fallback
   names. A legacy `voiceink-shell.ico` may not silently win application-icon
   lookup after the swap; compatibility is allowed only if it cannot mask the
   corrected PNG-derived identity.

## Packaging Implications

- Update the icon conversion input from `voiceink-shell.svg` to
  `voiceink-transcribe.png`. The converter must produce a multi-size
  `voiceink-transcribe.ico` suitable for PyInstaller PE metadata; it must not
  redraw the PNG or use the excluded macOS AppIcon.
- Pass the generated `voiceink-transcribe.ico` to `--icon` for both
  `voiceink-shell.exe` and `voiceink-shell-smoke.exe`.
- Bundle both corrected runtime inputs in both one-file executables:
  `voiceink-transcribe.png` for the application icon and `voiceink-shell.svg`
  for the Transcribe sidebar. The generated ICO must also be bundled for
  runtime lookup.
- Preserve the existing Windows-only build contract, package layout, portable
  package behavior, relocation smoke, and package README. `portable_package.py`
  and the release bundler should continue copying the already self-contained
  executables; no extra loose icon file is added to the release package.
- Build tests must assert the new source paths, `--icon` input, and both
  `--add-data` entries. A Windows build remains required to prove PE metadata
  and PyInstaller resource behavior; macOS tests cannot make that claim.

## Accessibility and Visual Tests

### Automated tests

- Registry test: `Transcribe` maps to `voiceink-shell.svg`; its label, enabled
  state, tooltip, accessible name, accessible description, and route remain
  unchanged.
- Qt rendering test: the corrected sidebar asset loads, produces non-null
  normal/active/selected/disabled icons, remains valid at every existing DPR,
  and preserves the SVG's red tile/waveform identity.
- Runtime application-icon test: the PNG is selected from frozen,
  executable-adjacent, and source roots in the documented order; an invalid or
  missing candidate follows the existing empty-icon fallback.
- Asset provenance test: the tracked PNG remains a valid 750x750 PNG and its
  SHA-256 stays equal to the approved macOS source copy. The generated ICO
  contains the expected multi-size entries and is derived from the PNG.
- Packaging test: both PyInstaller command constructions use the PNG-derived
  ICO and bundle both role assets for both executables.
- Accessibility regression test: the sidebar button continues to expose the
  localized `Transcribe` accessible name and destination description; icon
  swapping must not change accessible text or keyboard routing.

### Visual verification

- Offscreen Qt verification must show the waveform tile in the `Transcribe`
  sidebar slot and the microphone mark as the application/window identity.
- On Windows, inspect the built executable/taskbar/title-bar identity and run
  the existing GUI and relocation smoke checks from a moved package.
- Verify light/dark, hover/checked/disabled, and high-DPI sidebar states. The
  swap must not alter tile colors, layout geometry, labels, focus behavior, or
  disabled-state contrast outside the asset role change.

## Boundaries

In scope: asset-role mapping, icon filename/constants, SVG/PNG runtime
rasterization where required by the swap, PNG-to-ICO conversion, PyInstaller
data/resource wiring, and focused runtime/packaging/accessibility/visual tests.

Out of scope: new artwork, recoloring, use of the macOS AppIcon, changes to
sidebar order or routes, theme/layout changes, localization, window/tray
behavior, transcription behavior, packaging artifact policy, and any edits to
the macOS VoiceInk checkout. No production code or binary asset is changed by
this RFC commit.

## Acceptance Criteria

1. The implementation diff contains no new branding artwork and no mutation of
   either existing source asset; only their consumers and derived ICO naming
   change.
2. The corrected mapping is observable in the registry, Qt runtime, generated
   Windows ICO, PyInstaller commands, and both packaged executables.
3. `Transcribe` renders the existing `voiceink-shell.svg`; the application and
   Windows executable use the existing macOS-derived PNG through
   `voiceink-transcribe.ico`/`voiceink-transcribe.png`.
4. Frozen, relocated, and source-checkout runtime lookup paths are covered;
   missing assets never make normal startup crash.
5. Accessibility names/descriptions, navigation behavior, state styling,
   high-DPI rendering, and disabled-state readability remain unchanged.
6. Focused tests pass on macOS, and the Windows build plus native package
   smoke provides the Windows-only evidence. No macOS test is presented as
   proof of Windows PE or taskbar behavior.

## Implementation Boundary and Follow-up

This RFC supersedes the current role assignment described by
`rfcs/workstreams/branding-theme.md` and
`rfcs/workstreams/runtime-app-icon.md` only after implementation is approved.
Those files are intentionally not edited in this planning-only commit. The
next implementation change must update the code, focused tests, and any
status/provenance text atomically, then run the repository's applicable quality
gates.
