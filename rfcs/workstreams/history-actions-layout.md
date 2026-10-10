# RFC: History Actions Layout

## Status

Status: Approved for the presentation-only workstream.

## Goal

Keep History copy, export, and delete controls attached to the transcript row
that owns the corresponding record. The controls must remain visible and usable
at the existing fixed content width, including when the row is expanded and
when Russian labels are active.

## Problem

`HistoryPage` currently creates fallback action widgets directly on the page and
only replaces those references after a row is selected. The fallback widgets are
not in a layout, so Qt can render them at the page origin. In particular, the
Delete button can appear in the upper-left corner instead of inside a History
row. The single horizontal action layout also has insufficient room for all
localized labels at the minimum shell width.

## Boundaries

- Change only the History presentation widgets, their local stylesheet rules if
  needed, and focused GUI/regression tests.
- Keep copy, TXT/Markdown export, delete confirmation, deletion service usage,
  injected audio/folder ports, and localization keys unchanged.
- Do not change shell-wide geometry, persistence, migrations, microphone code,
  or native audio behavior.
- Keep page-level private test handles compatible by making fallback handles
  non-rendering and rebinding them to the selected row's controls.

## Proposed Design

`HistoryRow` owns every visible History action. The variant selector is placed
on a local selector row, while copy, audio, folder, TXT, Markdown, and Delete
are placed on a second local action row. This gives the action buttons enough
width at the existing minimum content size without changing the shell.

`HistoryPage` keeps hidden fallback widgets only as private handles used before
and between selections. Rendering a page resets those handles to the hidden
fallbacks; selecting a record rebinds them to that record's `HistoryRow`.
Consequently no visible action widget can be positioned outside its owning
row, and existing copy/export/delete methods continue to operate on the
selected record.

## Acceptance

- No History action control is visible at the page origin before a row is
  selected or when persistence is unavailable.
- Every visible History action is owned by the corresponding `HistoryRow` and
  remains inside that row at the minimum content width.
- Expanding a row keeps its controls aligned with that row and usable.
- Copy, TXT export, Markdown export, delete confirmation, deletion, and
  injected audio/folder behavior remain unchanged.
- English and Russian action labels continue to update through the shared
  locale signal.
- Offscreen GUI tests cover fallback visibility, row ownership, fixed-width
  placement, expanded-row placement, and localized action labels.

## Verification

- Run focused History GUI tests and relevant localization/presentation tests.
- Run the repository's format, lint, spec, and full test gates.
- Confirm the diff contains no shell-wide geometry or microphone/backend changes.
