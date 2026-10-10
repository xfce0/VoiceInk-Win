# RFC: Branding Theme

## Status

Implemented.

## Summary

Replace the Transcribe navigation glyph with the actual VoiceInk macOS
microphone branding asset, keep that asset available in source and packaged
runtimes, and correct the Light theme's recorder surfaces. Application text
weights are made consistently slightly heavier without changing sizes or
layout geometry.

## Asset Decision

The macOS VoiceInk checkout exists at `/Users/paulosipov/VoiceInk`. Its
`VoiceInk/Assets.xcassets/menuBarIcon.imageset/menuBarIcon.png` is the original
transparent VoiceInk microphone template asset, not a generic product icon.
It is copied byte-for-byte to:

```text
packaging/voiceink-shell-windows-x64/voiceink-transcribe.png
```

The copied asset SHA-256 is
`634396427fc3ff823cd24fe57e88f49e55de4f3d`. No source asset blocker remains.

The Windows repository's existing `voiceink-shell.svg` remains the executable
branding source. No macOS AppIcon is substituted for the Windows executable;
the standalone menu-bar microphone is the correct source for the Transcribe
navigation action.

## Decisions

- `Transcribe` uses the repository-owned copied VoiceInk PNG while other
  navigation items continue to use their inline SVG paths.
- Runtime lookup tries frozen-bundle, executable-adjacent, and source-checkout
  locations for the Transcribe PNG, matching the existing application icon
  lookup order.
- The frontend builder adds the PNG to both PyInstaller executables alongside
  the ICO and SVG assets.
- Light recorder surfaces use light semantic tokens; recording/error colors
  remain state-specific and accessible.
- Typography weights are represented by shared tokens and raised modestly;
  font sizes, spacing, and widget geometry are unchanged.
- Existing accessible names, descriptions, focus rules, and disabled states
  remain in place. Tests verify the asset, package commands, theme surfaces,
  font tokens, and navigation accessibility.

## Scope

This workstream changes branding assets, the presentation icon pipeline, the
shared Qt theme stylesheet, and focused tests. Routing, microphone capture,
transcription behavior, and unrelated domain/application code are out of
scope.

## Validation

- Targeted runtime asset, packaging, navigation, and theme tests.
- Ruff format/lint and compile checks for changed Python files.
- Relevant full repository test suite through `make test`.
