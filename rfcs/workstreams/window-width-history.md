# RFC: Fixed Shell Geometry and History Row Width Budget

## Status

Proposed. This RFC is implementation-ready and supersedes the geometry portions
of `shell-layout-state.md` for the fixed-shell/list-width workstream. It does
not replace the History interaction or persistence RFCs.

## Goal

Make the Windows desktop shell use one deliberate fixed geometry, make the
available content width explicit, and ensure that History rows and the other
variable-width list surfaces never clip their content or introduce horizontal
scrolling.

## Current Failure Mechanism

The current shell already calls `MainWindow.setFixedSize(950, 750)`, but the
geometry contract is implicit and is not carried through the page layouts:

- `950` includes the fixed `208 px` sidebar, leaving `742 px` for the stacked
  page viewport.
- Each page then applies its own margins and child/list styles. The effective
  History/Dictionary page content width is therefore smaller than the shell
  width, and a vertical list scrollbar can reduce the list viewport again.
- History and Dictionary set list-item hints from `viewport().width()`, while
  History row height is derived from a word-wrapped widget before the final row
  geometry is necessarily settled. A stale or over-wide item hint can make the
  row widget wider than its viewport; a late height change can clip wrapped
  text or action controls.
- History action rows depend on implicit button size policies. The existing
  two-row action design is correct, but there is no regression assertion that
  the localized controls fit inside a row at the actual fixed shell geometry.
- Existing tests validate these surfaces at different standalone sizes. The
  History action test uses a `592 px` page, the Dictionary test accepts a row
  narrower than the viewport, and the Transcribe test checks only the queue
  scroll-area widget. They do not prove one shared width budget through the
  real `MainWindow`.

The failure is presentation-only. No persistence, localization data, or
transcription operation is responsible for the clipping.

## Exact Geometry Decision

The Windows shell will retain the macOS reference geometry and make the
calculation explicit:

```text
MAIN_WINDOW_WIDTH  = 950 px
MAIN_WINDOW_HEIGHT = 750 px
SIDEBAR_WIDTH      = 208 px
PAGE_VIEWPORT_WIDTH = 950 - 208 = 742 px
PAGE_HORIZONTAL_INSET = 30 px per side
LIST_PAGE_CONTENT_WIDTH = 742 - (2 * 30) = 682 px
```

The fixed-size decision is intentional:

- `MainWindow` must report exactly `950 x 750` after construction.
- `minimumSize()` and `maximumSize()` must both equal the fixed window size.
- The sidebar remains exactly `208 px`; the page stack receives the remaining
  `742 px`.
- History, Dictionary, and Transcribe must consume the page width available to
  them. They must not reserve a second arbitrary content width.
- The `682 px` value is the outer page content budget, not a hard-coded row
  width. List rows must use the actual list `viewport().width()` because Qt
  styles, frame padding, and a visible vertical scrollbar may reduce it.
- A visible vertical scrollbar is acceptable. A horizontal scrollbar is not
  acceptable on the History, Dictionary, or Transcribe list surfaces.

The values should live in one presentation geometry contract. Existing imports
of `MAIN_WINDOW_WIDTH`, `MAIN_WINDOW_HEIGHT`, and `SIDEBAR_WIDTH` from
`main_window.py` remain compatible; moving their definitions to a small
presentation metrics module is allowed, but a generic layout framework is not
needed.

## Proposed Design

### Shell and page budget

1. Keep `MainWindow.setFixedSize(MAIN_WINDOW_WIDTH, MAIN_WINDOW_HEIGHT)` as the
   single shell sizing operation.
2. Define the sidebar width and page insets beside the shell metrics, rather
   than repeating unexplained literals in page implementations.
3. Keep the existing page margins (`30, 28, 30, 24`) unless a test proves that
   a specific page violates the budget. The implementation must not solve a
   row-clipping defect by changing the shell size or by adding page-specific
   fixed widths.
4. Treat the stacked page viewport as the boundary for all width assertions.
   The dashboard and vertically scrolling settings/audio/model pages may keep
   their current behavior; this RFC targets variable-width list surfaces.

### List width synchronization

Apply one policy to the three relevant list surfaces:

- Transcribe queue scroll content;
- History `QListWidget` rows;
- Dictionary `QListWidget` rows.

Each list must expand to its parent viewport, disable horizontal scrolling, and
recalculate item/widget geometry after insertion and after a viewport resize.
For History specifically:

- derive the item width from the current `historyList.viewport().width()`;
- synchronize the row height after the row has received that width, including
  after asynchronous rendering and after expansion/collapse;
- do not use `historyList.width()` or the `682 px` outer budget as the row
  width;
- ensure the row has an expanding horizontal policy and no non-zero minimum
  width that can exceed its viewport.

Dictionary rows follow the same viewport rule. Transcribe queue content must
continue to match its scroll-area viewport rather than the outer page width.

### History row internals

Preserve the approved History actions design:

- the variant selector has its own row;
- Copy/Audio/Folder remain together on one action row;
- TXT/Markdown/Delete remain together on a second action row;
- visible action widgets remain owned by their `HistoryRow`;
- hidden page-level fallback handles remain non-rendering compatibility handles.

Make the width behavior explicit:

- the source title is horizontally expanding and may wrap;
- metadata may wrap when the available header width is reduced;
- preview and full transcript text remain word-wrapped;
- action rows keep their leading stretch and localized controls use their
  natural minimum size, so no action is placed outside the row to make room;
- no action row is changed back to one six-button horizontal row.

The row's final item height must be based on the final row width. This is the
critical ordering rule for preventing vertical clipping of wrapped previews,
expanded transcripts, and localized action controls.

## Boundaries

### In scope

- `MainWindow` presentation geometry constants and fixed-size enforcement;
- page/list width budgeting in `HistoryPage`, `HistoryRow`,
  `DictionaryPage`, and `TranscribePage`;
- only the local History row size-policy/wrapping changes required by the
  geometry contract;
- focused offscreen GUI regression tests for shell geometry and list widths;
- this RFC and any implementation notes needed to keep existing test handles
  stable.

### Out of scope

- persistence, SQLite schema, migrations, pagination, or newest-first order;
- transcription queue state, media normalization, or ASR/runtime behavior;
- microphone, audio playback, folder reveal, tray, hotkey, or Windows API code;
- redesigning the History card, action ownership, or localization keys;
- making the shell user-resizable;
- changing the macOS application.

## Acceptance Criteria

- A constructed `MainWindow` is exactly `950 x 750`, and its minimum and
  maximum sizes equal that geometry.
- The sidebar is exactly `208 px` wide and the stacked page viewport is
  `742 px` wide at the fixed shell geometry.
- The effective page content budget is `682 px` before Qt list frame/padding or
  a vertical scrollbar is applied; no implementation uses `682 px` as a fixed
  row width.
- Transcribe queue content, History rows, and Dictionary rows are no wider
  than their actual viewport and have no visible horizontal scrollbar.
- A long History source name, long metadata string, multi-line preview, and
  expanded transcript remain visible without clipping at the fixed shell size.
- English and Russian History labels fit at the fixed shell size; all six
  visible History action buttons remain inside their owning row when selected
  and expanded.
- History row geometry is correct both when data arrives asynchronously and
  after a row is expanded, collapsed, or the page receives a resize event.
- Existing History actions, pagination/search, Dictionary CRUD, Transcribe
  queue behavior, accessibility names, and localization behavior remain
  unchanged.

## Regression Test Plan

Add or update offscreen PySide6 tests using the real `MainWindow` wherever the
assertion depends on the shell budget.

### Shell geometry

Extend `test_presentation_navigation.py` to assert:

- `window.size() == QSize(MAIN_WINDOW_WIDTH, MAIN_WINDOW_HEIGHT)`;
- `window.minimumSize() == window.maximumSize() == window.size()`;
- sidebar width is `SIDEBAR_WIDTH`;
- the current page stack width is `MAIN_WINDOW_WIDTH - SIDEBAR_WIDTH`.

### Transcribe list surface

Extend the existing Transcribe GUI coverage with an enabled fake controller and
at least one queue item. Assert that the queue content width equals its scroll
viewport width, every rendered queue item stays within that width, and the
horizontal scrollbar is not visible. Exercise a long source name or failure
message so the test covers wrapping rather than an empty list only.

### History list surface

Use a real fixed-size `MainWindow` with persisted records containing:

- a long source name and metadata;
- a multi-line transcript long enough to wrap;
- a second record to make the list vertically scrollable;
- Russian locale labels, followed by an English locale check.

After the asynchronous load, and again after selecting/expanding the row,
assert:

- every row width is `<= historyList.viewport().width()`;
- the horizontal scrollbar is not visible;
- the row rectangle contains every visible action button and the variant
  selector;
- the row height contains the expanded full-text label and action rows;
- the row remains valid after a page resize event (where a resize is possible
  in the isolated page test).

Retain the existing action ownership, copy, audio/folder port, deletion, and
newest-first tests; this RFC adds geometry coverage rather than replacing
behavior coverage.

### Dictionary list surface

Create a long phrase/replacement entry through the existing persistence test
fixture and assert that each dictionary row is `<= dictionaryList.viewport()`
width, its Edit/Delete controls are contained by the row, and the horizontal
scrollbar is not visible. Run the assertion at the fixed `MainWindow` geometry
and in Russian where the labels are longest. Keep the current `<=` semantics:
Qt may reserve width for a vertical scrollbar.

### Verification commands

Run the focused GUI files first, then the repository gates required by
`PROJECT_CONTEXT.md`:

```text
pytest tests/test_presentation_navigation.py \
       tests/test_settings_history_dictionary_gui.py \
       tests/test_transcribe_gui.py
make spec-check
make format-check
make lint
make test
make build
```

The implementation RFC is complete only when the focused tests exercise all
three list surfaces and the full gates pass in an environment with the GUI
extra installed.

## Dependency and Conflict Notes

- Builds on the approved `shell-layout-state.md`; this RFC makes its width
  statement numeric and testable rather than introducing a different shell
  geometry.
- Builds on `history-macos-layout.md` and `history-actions-layout.md`. Their
  row ownership, two-line preview, two action rows, and hidden fallback-handle
  decisions remain authoritative. The latter RFC's prohibition on shell-wide
  geometry changes is superseded only for this dedicated geometry workstream.
- Preserves `history-newest-first.md`: the persistence order and cursor
  behavior are untouched.
- Coordinates with the Dictionary visual redesign and the Transcribe page
  workstream by applying the same viewport-width contract; no shared domain or
  persistence dependency is introduced.
- The prior scrollbar regression fix deliberately relaxed Dictionary equality
  to `row.width() <= viewport.width()`. Do not revert that change to strict
  equality.
- The macOS reference confirms `950 x 750` as the intended main-window
  geometry. The Windows implementation remains fixed in both dimensions for
  this workstream, even though the macOS window manager permits additional
  vertical growth.
- No dependency on native Windows validation is required for the layout
  contract; offscreen Qt tests prove the measurable geometry, while interactive
  Windows validation remains useful for final visual confirmation.

## Open Questions

None. The width, height, sidebar budget, viewport boundary, list scope, and
regression strategy are fixed by this RFC.
