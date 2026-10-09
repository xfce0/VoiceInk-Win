VoiceInk Windows x64 frontend shell

Run the user-facing GUI from PowerShell:

    .\voiceink-shell.exe

The artifact also contains `voiceink-shell-smoke.exe`. It is a CI-only
console-mode smoke-test executable used to launch the same UI in offscreen
mode and show Python tracebacks and Qt diagnostic messages. It is not
`voiceink-diagnostic.exe` and is not a user-facing CLI.

The shell supports imported-media Transcribe when the approved runtime is
provisioned locally. Set `VOICEINK_RUNTIME_MANIFEST`, `VOICEINK_ARTIFACT_LOCK`,
`VOICEINK_ARTIFACT_LOCK_SHA256`, the approved `VOICEINK_FFMPEG_*` values,
`VOICEINK_IMPORT_WORKSPACE_ROOT`, and `VOICEINK_IMPORT_ROOTS` before launching.
The package does not download or provision those runtime assets; local
provisioning is intentionally a separate operator step. Without that
configuration, Transcribe starts unavailable.

Microphone capture, WASAPI, and native audio are not included. This package
does not contain a fake microphone or a fake production backend.
