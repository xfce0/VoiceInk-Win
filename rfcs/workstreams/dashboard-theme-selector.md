# RFC: Dashboard Theme Selector

## Status

Status: Approved for this implementation slice.

## Summary

Persist a `System`, `Light`, or `Dark` dashboard preference in the existing
Settings/SQLite boundary. `System` follows Qt's effective color scheme;
explicit modes ignore later system-scheme changes. Settings changes refresh the
visible shell immediately and are re-applied when the window is rebuilt.

The stylesheet keeps one semantic palette for both modes and adds shared
Dashboard Good Morning typography tokens, brighter primary text, slightly
heavier body/sidebar text, and visible keyboard focus states. Dictionary and
History layouts and the application icon are out of scope.

## Boundaries

- Add one backwards-compatible Settings field and one SQLite migration.
- Keep theme resolution in presentation; persistence stores the preference only.
- Reuse the existing stylesheet for the main window and floating recorder.
- React to Qt system-theme signals only while `System` is selected.
- Verify persistence, selector behavior, stylesheet tokens, focus rules, and
  runtime refresh with targeted tests.
