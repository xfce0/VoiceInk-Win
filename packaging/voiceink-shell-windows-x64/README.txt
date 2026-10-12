VoiceInk Windows x64 portable CPU package

Launch the user-facing GUI directly from Explorer or PowerShell:

    .\voiceink-shell.exe

`voiceink-shell.cmd` is retained only as a developer/CLI helper. It is not the
Explorer shortcut target because command scripts can flash a terminal window.

The portable package contains the pinned CPU sidecar, Parakeet model, and
FFmpeg executable. `voiceink-package.json` records their hashes, provenance,
licenses, and package-relative paths. The shell discovers this descriptor next
to the executable and verifies the artifacts before starting the backend.

The package builder does not download artifacts. Supply the exact files from
the approved artifact lock to `make portable-package`.

Microphone capture uses the packaged `sounddevice` PortAudio backend. WASAPI
and native helper binaries are not included.
