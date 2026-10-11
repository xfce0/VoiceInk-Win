# RFC: Branding Theme

## Status

Implemented.

## Summary

Establish the VoiceInk branding asset provenance and keep the source and
packaged assets available. The icon role assignment in this historical
workstream is superseded by `icon-assets-swap.md`; the theme and typography
decisions remain in force.

## Asset Decision

The macOS VoiceInk checkout exists at `/Users/paulosipov/VoiceInk`. Its
`VoiceInk/Assets.xcassets/menuBarIcon.imageset/menuBarIcon.png` is the original
transparent VoiceInk microphone template asset, not a generic product icon.
It is copied byte-for-byte to:

```text
packaging/voiceink-shell-windows-x64/voiceink-transcribe.png
```

The copied asset SHA-256 is
`de11e5550a84a03094f4cc60c6aff71045b67ccd020d142d6e379795c32ce0b`. No source
asset blocker remains.

The Windows repository's existing `voiceink-shell.svg` is the Transcribe
sidebar source. The standalone menu-bar microphone remains the application
icon source and no macOS AppIcon is substituted for the Windows executable.

## Decisions

- `Transcribe` uses the repository-owned Windows SVG while other navigation
  items continue to use their inline SVG paths.
- Runtime lookup tries frozen-bundle, executable-adjacent, and source-checkout
  locations for the Transcribe PNG, matching the existing application icon
  lookup order.
- The frontend builder adds the PNG-derived ICO, PNG, and sidebar SVG to both
  PyInstaller executables.
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
