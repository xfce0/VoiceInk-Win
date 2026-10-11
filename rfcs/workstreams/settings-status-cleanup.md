# RFC: Settings Status Cleanup

## Status

Proposed for implementation on `fix/settings-status`.

## Summary

The Settings page currently renders two passive page-level states that do not
help the user complete a settings task:

- a standalone availability label directly below the subtitle when persistence
  is unavailable; and
- a bottom `Ready`/`Готово` label after a successful settings load.

The first is visually detached from the controls it explains, and the second
describes an idle state rather than an action. The cleanup removes those two
steady-state labels without removing meaningful capability information or
actionable errors.

In this RFC, “top-left `Недоступно`” means the standalone Settings page
availability region (`_availability`), not the inline model/audio values. The
inline `Not available.` / `Недоступно.` values remain part of the Settings
contract.

## Current presentation contract

`SettingsPage` currently owns the full Settings content inside a scroll area:

1. title and subtitle;
2. `_availability` (`QLabel#pageUnavailable`) immediately below the subtitle;
3. editable settings form;
4. model and audio preference labels and values;
5. a stretch followed by `_status` (`QLabel#metadata`) and `_error`
   (`QLabel#inlineError`).

The current states are:

| Condition | Current visible state |
| --- | --- |
| Persistence is loading | Bottom `Loading...` / `Загрузка...`; controls disabled. |
| Persistence loaded | Bottom `Ready` / `Готово`; availability hidden; model/audio values show `Not available.` / `Недоступно.` when their preference maps are empty. |
| Persistence is absent or load fails | Top neutral availability label: `Local storage is unavailable.` / `Локальное хранилище недоступно.`; controls disabled; status and error are empty. |
| Save in progress | Bottom `Saving...` / `Сохранение...`. |
| Save succeeds | Bottom `Saved` / `Сохранено`. |
| Save fails | Bottom `Error` / `Ошибка` plus inline `Could not save settings. Try again.` / `Не удалось сохранить настройки. Попробуйте ещё раз.`. |

The shared stylesheet deliberately distinguishes `pageUnavailable` from
`inlineError`. The former must remain available to other pages that use the
neutral persistence state; this RFC does not remove that global selector.

## Decision

### Steady state

- Do not render a standalone Settings availability label below the subtitle.
- Do not render `Ready` / `Готово` after a successful load.
- The Settings feedback area is conditional: it has no visible text and does
  not reserve a visible status row when the page is loaded and idle.
- Keep the inline model and audio capability values exactly as localized
  `Not available.` / `Недоступно.` when the corresponding preference map is
  empty. These values explain unavailable capabilities at the field that owns
  them and must not be promoted to a page-level banner.

### Loading and persistence failure

- While persistence is loading, disable the settings controls and show only
  `Loading...` / `Загрузка...` in the Settings feedback area.
- If persistence is absent or loading fails, keep the controls disabled and
  show the localized persistence message inline in the Settings feedback area:
  `Local storage is unavailable.` / `Локальное хранилище недоступно.`.
- The persistence message must not appear in the top-left availability region,
  and `Ready` / `Готово` must not appear in this state.

### Save feedback

- Keep `Saving...` / `Сохранение...` while an edit is being persisted.
- Keep `Saved` / `Сохранено` as the short-lived confirmation for a successful
  edit; it is an operation result, not the page's idle state.
- Keep the localized save error inline and actionable on failure:
  `Could not save settings. Try again.` /
  `Не удалось сохранить настройки. Попробуйте ещё раз.`.
- A save failure must not replace the error with `Ready` / `Готово` or with a
  generic page-level availability banner.

## Exact target states

The following table is the behavioral contract for implementation and tests.

| Condition | English | Russian | Controls |
| --- | --- | --- | --- |
| Loading | `Loading...` only in feedback | `Загрузка...` only in feedback | Disabled |
| Loaded and idle | No Settings status text; inline model/audio values remain `Not available.` where applicable | No Settings status text; inline model/audio values remain `Недоступно.` where applicable | Enabled |
| Persistence unavailable | Inline `Local storage is unavailable.`; no top availability label | Inline `Локальное хранилище недоступно.`; no top availability label | Disabled |
| Saving | `Saving...` | `Сохранение...` | Enabled unless the existing save flow disables a control |
| Saved | `Saved` | `Сохранено` | Enabled |
| Save error | `Error` plus `Could not save settings. Try again.` | `Ошибка` plus `Не удалось сохранить настройки. Попробуйте ещё раз.` | Enabled |

For all target states, the Settings page must not display a standalone
`Ready`/`Готово` label. The Dashboard, floating recorder, Modes, History,
Dictionary, Audio, and AI Models states are unchanged by this RFC.

## Layout ownership

- `SettingsPage` remains the sole owner of Settings title, form controls,
  inline capability values, and feedback rendering.
- `MainWindow` continues to own page routing, shared locale wiring, and the
  global stylesheet, but must not synthesize Settings status text.
- The Settings page's feedback area remains below the settings content, close
  to the controls it describes. It is visible only when loading, saving, save
  confirmation, or an error is active; it is not a permanent page footer.
- The model and audio rows own their own capability placeholders. Their values
  stay adjacent to `Model preference` / `Audio preference` and are not moved to
  the feedback area.
- No change is made to sidebar ownership, scroll-area ownership, fixed window
  geometry, or the layout of other pages.

## Localization

- Continue using `TranslationKey` and the shared `LocaleConfig`; do not embed
  English or Russian strings in widgets or tests as implementation literals
  beyond exact acceptance assertions.
- Keep both locale entries for:
  `COMMON_LOADING`, `COMMON_SAVING`, `COMMON_SAVED`, `COMMON_ERROR`,
  `COMMON_PERSISTENCE_UNAVAILABLE`, `SETTINGS_BACKEND_UNAVAILABLE`, and
  `SETTINGS_SAVE_ERROR`.
- `COMMON_READY` remains in the catalog because other pages and the recorder
  use it. Settings simply stops using it for its loaded idle state.
- Locale changes must update visible Settings feedback and capability values
  without reintroducing a stale English label or a steady `Готово` label.
- The existing catalog parity and placeholder checks remain mandatory.

## Styles

- Reuse `QLabel#inlineError` for actionable Settings errors and the existing
  muted styling for model/audio capability values.
- Do not remove or globally change `QLabel#pageUnavailable`; History and other
  presentation surfaces may still own neutral page-level availability states.
- Do not introduce a new color meaning, error panel, animation, or global
  typography change for this cleanup. A stylesheet change is justified only if
  the conditional Settings feedback needs its empty/visible geometry expressed
  without affecting other pages.

## Acceptance tests

Focused offscreen GUI tests must cover both locales and the state transitions.
The assertions below are intentionally behavioral; they should not depend on
private widget names if an equivalent accessible presentation assertion is
available.

### English (`en`)

1. With usable persistence, open Settings and wait for load completion. Assert
   that no standalone availability label is visible, no Settings status text is
   visible, `Ready` is absent from the Settings page, the controls are enabled,
   and empty model/audio preferences render `Not available.` inline beside
   their fields.
2. With persistence absent or a failed load, assert that controls are disabled,
   the top availability region is absent/not visible, and the inline feedback
   reads exactly `Local storage is unavailable.`. Assert that neither `Ready`
   nor `Saved` is shown.
3. During loading, assert that the only Settings feedback text is `Loading...`
   and that no `Ready` or top availability label is visible.
4. After a successful edit, assert the transition `Saving...` → `Saved` and
   that the page does not settle on `Ready`.
5. After a failed edit, assert `Error` and
   `Could not save settings. Try again.` remain visible inline and are not
   replaced by a page-level availability label.
6. Change the shared locale to Russian and assert the equivalent Russian state
   transitions and inline values below.

### Russian (`ru`)

1. With usable persistence, open Settings and wait for load completion. Assert
   that no standalone top `Недоступно`/persistence label is visible, no
   Settings status text is visible, `Готово` is absent from the Settings page,
   the controls are enabled, and empty model/audio preferences render
   `Недоступно.` inline beside their fields.
2. With persistence absent or a failed load, assert that controls are disabled,
   the top availability region is absent/not visible, and the inline feedback
   reads exactly `Локальное хранилище недоступно.`. Assert that neither
   `Готово` nor `Сохранено` is shown.
3. During loading, assert that the only Settings feedback text is `Загрузка...`
   and that no `Готово` or top availability label is visible.
4. After a successful edit, assert the transition `Сохранение...` → `Сохранено`
   and that the page does not settle on `Готово`.
5. After a failed edit, assert `Ошибка` and
   `Не удалось сохранить настройки. Попробуйте ещё раз.` remain visible inline
   and are not replaced by a page-level availability label.
6. Switch back to English and assert that the same widgets update to English,
   including clearing any stale Russian feedback.

### Localization and style regression

- Assert every `TranslationKey` still has both locale entries and matching
  format placeholders.
- Assert the Settings capability and error strings come from the catalog.
- Assert the shared stylesheet still distinguishes neutral `pageUnavailable`
  from unboxed `inlineError` and preserves both light and dark theme behavior.
- Existing Dashboard, recorder, History, Dictionary, and other presentation
  availability tests must remain unchanged and passing.

## Boundaries

### In scope

- Settings presentation state rendering and conditional feedback layout.
- Settings localization key usage and focused English/Russian assertions.
- Focused Settings-related stylesheet assertions only if the layout change
  requires them.

### Out of scope

- Persistence interfaces, SQLite schema, migrations, error propagation, or
  asynchronous callback ownership.
- Enabling model, microphone, audio, ASR, or any other unavailable runtime
  capability.
- Removing the inline model/audio `Not available.` / `Недоступно.` values.
- Changing availability banners or statuses on Dashboard, Modes, History,
  Dictionary, Audio, AI Models, or the floating recorder.
- Removing `COMMON_READY` from the shared localization catalog.
- Changes to navigation, window geometry, theme tokens, packaging, native
  Windows behavior, or global shortcut behavior.
- Implementation, PR creation, push, or changes to unrelated tests.

## Verification

Run the focused presentation and localization tests for the changed Settings
states, then run the repository quality gate required for implementation. This
RFC commit itself contains documentation only and must not change production
code or tests.
