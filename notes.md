# Development Notes

## 2026-10-07

- Manual Windows validation passed for the pinned NeMo-Speech.cpp v0.2.0 CPU runtime and Parakeet TDT v3 GGUF model.
- The runtime reported `ready: true`, `device: cpu`, and model `parakeet-tdt-0.6b-v3.oss-align.q8_0`.
- Direct CLI transcription of `audio_2026-10-05_17-13-15.wav` returned a non-empty Russian transcript with exit code `0`.
- This confirms that the backend transcription path works on Windows; native smoke evidence is technical validation, not the final WER/RTFx/RSS quality gate.
- Platform direction: develop from macOS, validate the Windows runtime through pinned native artifacts and real Windows checks.
- Current priority: finish a reliable backend and composition boundary first, then build the Windows-facing frontend on top of it.
- PR #6 contains the NeMo/Parakeet integration and native Windows smoke workflow.
