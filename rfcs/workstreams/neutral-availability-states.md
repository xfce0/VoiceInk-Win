# RFC: Neutral Availability States

## Status

Approved for implementation on `refactor/neutral-availability-states`.

## Summary

Settings, History, and Dictionary currently render persistence absence as a
permanent red `pageError` at the bottom of the page. Settings also exposes a
backend placeholder as "Backend is not configured yet." These are capability
states, not user mistakes, so they should not compete visually with errors that
require a user response.

## Decision

- Render missing or unavailable local storage with a compact neutral inline
  state and disable the affected controls as the pages already do.
- Render unavailable model/audio preferences with localized neutral copy, not
  backend-status language.
- Keep validation and operation failures as localized inline errors so the user
  can correct, retry, copy, export, or delete as appropriate.
- Keep persistence/backend contracts, error propagation, and the existing system
  theme selection unchanged.

## Verification

Localization tests cover both locales and placeholder parity. Offscreen GUI
tests cover neutral states on all three pages and confirm actionable dictionary
errors remain visible.
