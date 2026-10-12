from __future__ import annotations

from types import SimpleNamespace

from voiceink_win.domain import MicrophoneAvailability, MicrophoneCapability
from voiceink_win.infrastructure.sounddevice_microphone import SoundDeviceCaptureProvider


class _Stream:
    def __init__(self, callback):
        self._callback = callback
        self.closed = False

    def start(self):
        self._callback(b"\x01\x00" * 4, 4, None, None)

    def stop(self):
        return None

    def close(self):
        self.closed = True


class _SoundDevice:
    default = SimpleNamespace(device=[0, -1])

    def __init__(self):
        self.stream: _Stream | None = None

    def query_devices(self):
        return [{"name": "Test microphone", "max_input_channels": 2}]

    def check_input_settings(self, **kwargs):
        assert kwargs["samplerate"] == 16_000
        assert kwargs["channels"] == 1
        assert kwargs["dtype"] == "int16"

    def InputStream(self, **kwargs):
        self.stream = _Stream(kwargs["callback"])
        return self.stream

    RawInputStream = InputStream


def test_sounddevice_provider_enumerates_and_captures_canonical_pcm() -> None:
    sounddevice = _SoundDevice()
    provider = SoundDeviceCaptureProvider(
        sounddevice_module=sounddevice,
        platform_name="nt",
        architecture="AMD64",
    )

    status = provider.status()
    assert status.availability is MicrophoneAvailability.AVAILABLE
    assert status.message == "Windows microphone capture is ready"
    assert status.capability is MicrophoneCapability.SUPPORTED
    devices = provider.enumerate_devices()
    assert len(devices) == 1
    assert devices[0].display_name == "Test microphone"
    assert devices[0].sample_rates == (16_000,)

    session = provider.open(devices[0].selection_token)
    session.start()
    assert session.read_chunk() == b"\x01\x00" * 4
    session.stop()
    assert session.read_chunk() is None
    session.close()
    assert sounddevice.stream is not None
    assert sounddevice.stream.closed
