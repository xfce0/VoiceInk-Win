# RFC: Global Toggle Shortcut

## Status

Status: Implemented.

This RFC covers one global shortcut that toggles the existing recording state.
It does not implement microphone capture, audio buffering, transcription, or a
second global command.

## Summary

The application uses the existing `Settings.hotkeys["start_stop"]` value and
registers one callback for the application lifetime. The default is
`Ctrl+Alt+Space`. The callback starts recording from an idle-like state and
stops an active recording; the existing recorder UI remains responsible for
the processing transition.

## Goals

- Keep global shortcut registration behind a domain/application port.
- Register and unregister exactly one toggle shortcut with deterministic
  lifecycle ownership.
- Report conflict and platform/session unavailability without crashing the
  application.
- Reuse the existing persisted settings path without a database migration.
- Keep domain and application code independent of Qt and Windows APIs.

## Non-Goals

- Microphone capture or native audio implementation.
- A cancellation shortcut, push-to-talk mode, or any second global combination.
- Changing Windows privacy settings or silently taking over a conflicting key.

## Proposed Architecture

`GlobalShortcutPort` and `GlobalShortcutRegistration` are domain-owned ports.
`GlobalToggleShortcutService` owns the single registration, maps typed conflict
and unavailable failures to status values, and calls the existing recording
controller. The settings page updates the registration only after the existing
`start_stop` value is persisted.

The infrastructure boundary has two adapters:

- `WindowsGlobalShortcutPort` uses Win32 `RegisterHotKey` and a Qt native event
  filter only on Windows.
- `UnavailableGlobalShortcutPort` is a safe no-op boundary elsewhere and when
  the Windows/Qt session cannot provide native registration.

The native adapter supports one modifier-plus-key combination, including the
default. A Win32 already-registered result is exposed as `CONFLICT`; unsupported
platforms, missing Qt, missing event loop, and other registration failures are
exposed as `UNAVAILABLE`.

Registration and unregistration are `O(1)` in application-owned state and use
`O(1)` native registration state. Shortcut dispatch does not capture audio; it
only performs the existing controller state transition.

## Exit Criteria

- The default desktop entrypoint creates the application shortcut service.
- Window construction registers it and disposal unregisters it.
- A stored `hotkeys.start_stop` value replaces the default after loading.
- Conflicts and unavailable environments remain visible in typed service status
  and do not raise through the UI lifecycle.
- Tests cover toggle dispatch, replacement, idempotent cleanup, unavailable
  controller/adapter behavior, conflicts, and native syntax parsing.
- `make check` passes on a non-Windows host.

## Open Questions

- Windows CI still needs a real desktop-session smoke test for `RegisterHotKey`
  conflict and delivery behavior. The cross-platform tests intentionally do not
  claim native Windows evidence.
