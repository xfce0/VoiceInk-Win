# Third-Party Notices

The native Windows smoke archive uses the following pinned artifacts. The
canonical versions, revisions, URLs, and SHA-256 values are recorded in
`.github/native-smoke/artifact-lock.template.json`.

| Artifact | Pinned version or revision | URL | License | Attribution |
| --- | --- | --- | --- | --- |
| FFmpeg essentials build | 7.1.1 | https://github.com/GyanD/codexffmpeg/releases/download/7.1.1/ffmpeg-7.1.1-essentials_build.zip | GPL-3.0-or-later | FFmpeg project; Windows build published by GyanD/codexffmpeg |
| NeMo-Speech.cpp Windows runtime | 0.2.0 | https://github.com/NVIDIA/NeMo-Speech.cpp/releases/download/v0.2.0/nemo-speech-0.2.0-windows-x86_64-cpu.zip | Apache-2.0 | NVIDIA NeMo-Speech.cpp contributors |
| Parakeet TDT 0.6B v3 GGUF model | Hugging Face revision `541d1f99c6b0c3cd0b11a95167540bb8edefd82b` | https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3/resolve/541d1f99c6b0c3cd0b11a95167540bb8edefd82b/parakeet-tdt-0.6b-v3.q8_0.gguf | CC-BY-4.0 | NVIDIA, distributed through the `nvidia/parakeet-tdt-0.6b-v3` Hugging Face repository |
| Native smoke audio fixture | OpenSLR 12 source | https://dldata-public.s3.us-east-2.amazonaws.com/2086-149220-0033.wav | CC-BY-4.0 | OpenSLR 12 / LibriSpeech source |

The runtime archive may contain transitive dependencies. Their transitive notices
follow the upstream archive notices and must be retained with any
redistributed runtime archive; this repository does not replace or supersede
those notices.

The VoiceInk-Win project remains GPL-3.0-only. These notices describe the
external artifacts and do not change the license of the project source.
