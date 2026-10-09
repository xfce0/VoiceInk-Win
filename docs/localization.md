# Localization

The desktop presentation currently supports English (`en`) and Russian (`ru`).
`LocaleConfig` is the single in-memory locale boundary shared by the shell and
its pages. Unsupported values are normalized to English. Calls from a worker
thread are queued internally, so the state mutation, notification, and widget
updates all run on the GUI thread. Standalone widgets may create their own
config for tests, but production composition passes one shared instance.

Translation keys in `voiceink_win.presentation.localization` are stable API
within the presentation layer. New UI text should be added to the catalog and
looked up by key rather than embedded in a widget.

## SQLite Locale Hydration

The desktop composition hydrates the shared `LocaleConfig` from the persisted
SQLite `settings.language` field through the asynchronous Settings page flow.
The value is normalized through `resolve_locale` before widget text is
rendered. A language change updates only the `language` field through the
field-level settings controller, so concurrent mode/auto-copy/hotkey updates
are not lost. Worker-thread persistence calls return Futures; queued Qt
callbacks apply the locale only while the page generation is current.

Missing or invalid persistence leaves the presentation in deterministic English
and shows the persistence-unavailable state rather than raising synchronously
from a widget callback.
