VoiceInk Windows x64 portable CPU package

Launch the user-facing GUI directly from Explorer or PowerShell:

    .\voiceink-shell.exe

`voiceink-shell.cmd` is retained only as a developer/CLI helper. It is not the
Explorer shortcut target because command scripts can flash a terminal window.

For diagnostics, use the console smoke executable explicitly:

    .\voiceink-shell-smoke.exe --smoke

The artifact also contains `voiceink-shell-smoke.exe`. It is a CI-only
console-mode smoke-test executable used to launch the same UI in offscreen
mode and show Python tracebacks and Qt diagnostic messages. It is not
`voiceink-diagnostic.exe` and is not a user-facing CLI.

The portable package contains the pinned CPU sidecar, Parakeet model, and
FFmpeg executable. `voiceink-package.json` records their hashes, provenance,
licenses, and package-relative paths. The shell discovers this descriptor next
to the executable and verifies the artifacts before starting the backend.

The package builder does not download artifacts. Supply the exact files from
the approved artifact lock to `make portable-package`.

Microphone capture, WASAPI, and native audio are not included. This package
does not contain a fake microphone or a fake production backend.
