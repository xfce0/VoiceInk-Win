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

## Future Settings Persistence

The Settings page should persist only the locale value, for example under the
`language` key in the application's `QSettings` namespace. At application
startup, read that value, pass it through `resolve_locale`, and initialize
`LocaleConfig` before constructing the windows. When the Settings control
changes language on the GUI thread, call `LocaleConfig.set_locale`; after the
change succeeds, write `LocaleConfig.locale.value` back to `QSettings`.
Worker-thread calls are asynchronous requests and return whether a different
locale was queued.

Persistence is intentionally not part of this foundation. Until Settings owns
that read/write flow, every launch defaults deterministically to English.
