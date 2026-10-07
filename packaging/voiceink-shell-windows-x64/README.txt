VoiceInk Windows x64 frontend shell

Run the user-facing GUI from PowerShell:

    .\voiceink-shell.exe

The artifact also contains `voiceink-shell-smoke.exe`. It is a CI-only
console-mode smoke-test executable used to launch the same UI in offscreen
mode and show Python tracebacks and Qt diagnostic messages. It is not
`voiceink-diagnostic.exe` and is not a user-facing CLI.

This build is the first desktop UI slice. It uses a deterministic fake backend
for the demo flow. Microphone capture, real ASR, global hotkeys, system tray,
history persistence, and imported-media actions are not wired yet.
