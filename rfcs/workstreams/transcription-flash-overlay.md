# RFC: File Transcription Console Window Flash

## Status

Proposed fix, pending tests.

## Symptom

Pressing `Start` for file transcription briefly opens a window and closes it. The flash occurs when FFmpeg starts normalizing the media; the UI does not create a second top-level window for this action.

## Finding

The Windows FFmpeg runner creates the process with `CREATE_SUSPENDED` so it can assign a Job Object before resuming it. It does not set `CREATE_NO_WINDOW`. A console-subsystem `ffmpeg.exe` can therefore create a visible console while the GUI process is running. The short lifetime matches FFmpeg normalization, not a Qt widget lifecycle or sidecar readiness failure.

## Decision

Add `CREATE_NO_WINDOW` to the existing Windows FFmpeg creation flags while preserving suspended startup and Job Object assignment. Do not add an overlay or change microphone/history code. The sidecar is started during application composition, outside the `Start` action, and is not the source of this symptom.

## Regression Evidence

Inject the existing `subprocess.Popen` seam in the Windows runner test and assert that the captured flags contain both `CREATE_SUSPENDED` and `CREATE_NO_WINDOW`. This tests the window-creation contract rather than merely asserting that process startup raises no exception.

The test runs on the current macOS host by injecting the Windows platform branch and fake native process dependencies. It cannot prove the OS-level visual behavior of a real Windows console; Windows native smoke remains the final confirmation.
