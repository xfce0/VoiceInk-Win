# RFC: Shell Layout State

## Status

Status: Approved for the presentation-only workstream.

## Goal

Keep the Windows shell at one stable application geometry and make the
Transcribe, History, and Dictionary content use the width available beside the
sidebar. Navigation must expose exactly one active page while a page, including
History, is loading.

## Boundaries

- Change only the Qt presentation shell, page layout code, shared stylesheet
  selectors needed for width behavior, and focused presentation tests.
- Preserve the existing page, persistence, localization, async, and domain
  contracts.
- Keep the existing sidebar labels, ordering, disabled-page behavior, and
  navigation destinations.
- Do not change persistence, migrations, transcription behavior, or native
  runtime integration.

## Design

- `MainWindow` uses one fixed width and height so the sidebar and page geometry
  remain stable between launches and page changes.
- Transcribe queue content and list-based History and Dictionary rows expand to
  the current page viewport, with horizontal scrolling disabled where rows are
  intended to wrap.
- Sidebar selection is synchronized before page-specific refresh work starts;
  route handling leaves only the destination button checked.
- Existing Qt layouts, object names, stylesheet tokens, and offscreen testing
  patterns remain in use.

## Acceptance

- The main window reports the configured fixed geometry and cannot be resized
  to a different width or height.
- Transcribe queue items, History rows, and Dictionary rows occupy the
  available content width without introducing a horizontal scrollbar.
- Selecting any enabled page checks only its sidebar button, including while
  History data is loading.
- Existing page behavior and persistence tests continue to pass.
- Focused offscreen GUI tests cover geometry, width usage, and navigation state.
