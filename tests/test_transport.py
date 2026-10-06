from __future__ import annotations

import io
import json
import socket
import wave
from email import policy
from email.parser import BytesParser
from threading import Event, Thread

import pytest

from voiceink_win.application import CancellationTokenSource
from voiceink_win.domain import CancellationError, ProtocolError
from voiceink_win.infrastructure import (
    ASR_NONCE_HEADER,
    TransportResponse,
    UrllibLoopbackTransport,
    UrllibReadinessProbe,
    validate_nonce,
)


class FakeHttpResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def read(self, amount: int) -> bytes:
        return b"x" * (amount + 1)


class SizedHttpResponse(FakeHttpResponse):
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.headers = headers or {}

    def read(self, amount: int) -> bytes:
        chunk, self.body = self.body[:amount], self.body[amount:]
        return chunk


def test_urllib_transport_bounds_response_read(monkeypatch) -> None:
    monkeypatch.setattr(
        "voiceink_win.infrastructure.transport.urlopen", lambda *args, **kwargs: FakeHttpResponse()
    )
    transport = UrllibLoopbackTransport("http://127.0.0.1:8123", nonce="test-nonce")

    with pytest.raises(ProtocolError):
        transport.post("/health", b"{}", timeout=1.0, max_response_bytes=8)


def test_transport_response_is_a_small_infrastructure_value() -> None:
    response = TransportResponse(200, b"ok")

    assert response.status_code == 200
    assert response.body == b"ok"


def test_urllib_model_attestation_get_uses_api_credentials(monkeypatch) -> None:
    requests = []

    class Response(SizedHttpResponse):
        status = 200

    monkeypatch.setattr(
        "voiceink_win.infrastructure.transport.urlopen",
        lambda request, timeout: (
            requests.append((request, timeout))
            or Response(b'{"data":[{"id":"parakeet-tdt-0.6b-v3.oss-align.q8_0"}]}')
        ),
    )
    transport = UrllibLoopbackTransport("http://127.0.0.1:8123", nonce="test-nonce")
    transport.set_api_key("test-api-key")

    response = transport.get("/v1/models", timeout=1.0, max_response_bytes=1024)

    assert response.status_code == 200
    headers = {name.casefold(): value for name, value in requests[0][0].header_items()}
    assert headers["authorization"] == "Bearer test-api-key"
    assert headers[ASR_NONCE_HEADER.casefold()] == "test-nonce"


def test_nonce_validation_is_constant_time_compatible_and_rejects_wrong_values() -> None:
    assert validate_nonce("nonce", "nonce")
    assert not validate_nonce("wrong", "nonce")
    assert not validate_nonce("нonce", "nonce")


def test_readiness_probe_sends_nonce_header(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self, amount: int) -> bytes:
            del amount
            return json.dumps(
                {
                    "ready": True,
                    "device": "cpu",
                    "capabilities": ["transcription"],
                }
            ).encode()

    requests = []
    monkeypatch.setattr(
        "voiceink_win.infrastructure.process.urlopen",
        lambda request, timeout: requests.append((request, timeout)) or Response(),
    )
    probe = UrllibReadinessProbe("http://127.0.0.1:8123")
    probe.set_nonce("test-nonce")

    assert probe.ready(1.0)
    headers = {name.casefold(): value for name, value in requests[0][0].header_items()}
    assert headers[ASR_NONCE_HEADER.casefold()] == "test-nonce"


def test_readiness_probe_rejects_forged_health_payload(monkeypatch) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self, amount: int) -> bytes:
            del amount
            return b'{"ready": false}'

    monkeypatch.setattr(
        "voiceink_win.infrastructure.process.urlopen",
        lambda request, timeout: Response(),
    )
    probe = UrllibReadinessProbe("http://127.0.0.1:8123")
    probe.set_nonce("test-nonce")

    assert not probe.ready(1.0)


@pytest.mark.parametrize(
    ("backend", "device", "expected"),
    [
        ("cpu", "cpu", True),
        ("cpu", "cuda:0", False),
        ("cpu", "auto", False),
        ("cuda:2", "cuda:2", True),
        ("cuda:2", "cuda:0", False),
        ("cuda:2", "cpu", False),
    ],
)
def test_readiness_probe_attests_device_for_configured_backend(
    monkeypatch, backend: str, device: str, expected: bool
) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self, amount: int) -> bytes:
            del amount
            return json.dumps(
                {"ready": True, "device": device, "capabilities": ["transcription"]}
            ).encode()

    monkeypatch.setattr(
        "voiceink_win.infrastructure.process.urlopen",
        lambda request, timeout: Response(),
    )

    probe = UrllibReadinessProbe("http://127.0.0.1:8123", expected_backend=backend)

    assert probe.ready(1.0) is expected


@pytest.mark.parametrize(
    "payload",
    [
        {"ready": True},
        {"ready": True, "capabilities": {"transcription": True}},
        {"ready": True, "capabilities": ["speech"]},
    ],
)
def test_readiness_probe_rejects_missing_or_wrong_capability(monkeypatch, payload) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def read(self, amount: int) -> bytes:
            del amount
            return json.dumps(payload).encode()

    monkeypatch.setattr(
        "voiceink_win.infrastructure.process.urlopen",
        lambda request, timeout: Response(),
    )
    probe = UrllibReadinessProbe("http://127.0.0.1:8123")
    probe.set_nonce("test-nonce")

    assert not probe.ready(1.0)


@pytest.mark.parametrize("body", [b"", b"x" * 8])
def test_urllib_transport_accepts_response_at_or_below_limit(monkeypatch, body: bytes) -> None:
    monkeypatch.setattr(
        "voiceink_win.infrastructure.transport.urlopen",
        lambda *args, **kwargs: SizedHttpResponse(body),
    )

    response = UrllibLoopbackTransport("http://127.0.0.1:8123", nonce="test-nonce").post(
        "/health", b"{}", timeout=1.0, max_response_bytes=8
    )

    assert response.body == body


def test_transport_rejects_declared_oversized_body_before_reading(monkeypatch) -> None:
    response = SizedHttpResponse(b"", {"Content-Length": "9"})
    monkeypatch.setattr(
        "voiceink_win.infrastructure.transport.urlopen", lambda *args, **kwargs: response
    )

    with pytest.raises(ProtocolError):
        UrllibLoopbackTransport("http://127.0.0.1:8123", nonce="test-nonce").post(
            "/health", b"{}", timeout=1.0, max_response_bytes=8
        )
    assert response.body == b""


def test_transport_rejects_chunked_body_above_limit() -> None:
    transport = UrllibLoopbackTransport("http://127.0.0.1:8123", nonce="test-nonce")

    with pytest.raises(ProtocolError):
        transport._parse_response(
            bytearray(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n9\r\n123456789\r\n"),
            8,
            complete=False,
        )


def test_transport_rejects_truncated_content_length_response() -> None:
    transport = UrllibLoopbackTransport("http://127.0.0.1:8123", nonce="test-nonce")

    with pytest.raises(ProtocolError):
        transport._parse_response(
            bytearray(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nx"),
            8,
            complete=True,
        )


def test_transport_accepts_zero_length_content_length_response() -> None:
    transport = UrllibLoopbackTransport("http://127.0.0.1:8123", nonce="test-nonce")

    response = transport._parse_response(
        bytearray(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"),
        8,
        complete=False,
    )

    assert response == TransportResponse(204, b"")


@pytest.mark.parametrize(
    "path", ["/transcribe\r\nX-Leak: yes", "//other-host", "/transcribe?token=leak"]
)
def test_post_audio_rejects_non_origin_form_path_before_connecting(monkeypatch, path: str) -> None:
    transport = UrllibLoopbackTransport("http://127.0.0.1:8123", nonce="test-nonce")
    monkeypatch.setattr(
        transport, "_connect_cancellable", lambda *args, **kwargs: pytest.fail("connected")
    )

    with pytest.raises(ProtocolError):
        transport.post_audio(path, b"{}", memoryview(b""), 1.0, 8)


def test_post_audio_uses_compact_raw_metadata_and_never_embeds_audio_or_transcript() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    received: list[bytes] = []

    def serve() -> None:
        connection, _ = listener.accept()
        try:
            connection.settimeout(1.0)
            data = bytearray()
            while b"\r\n\r\n" not in data:
                data.extend(connection.recv(4096))
            content_length = int(
                next(
                    line.split(b":", 1)[1]
                    for line in data.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
            )
            while len(data) < data.find(b"\r\n\r\n") + 4 + content_length:
                data.extend(connection.recv(4096))
            received.append(bytes(data))
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        finally:
            connection.close()

    worker = Thread(target=serve, daemon=True)
    worker.start()
    try:
        metadata = b'{"schema":"voiceink.asr.request.v1","request_id":"safe"}'
        audio = b"\x01\x02\x03\x04"
        response = UrllibLoopbackTransport(
            f"http://127.0.0.1:{port}", nonce="test-nonce"
        ).post_audio("/transcribe", metadata, memoryview(audio), timeout=1.0, max_response_bytes=8)
    finally:
        worker.join(1.0)
        listener.close()

    assert response.body == b"ok"
    assert len(received) == 1
    wire = received[0]
    assert wire.startswith(b"POST /transcribe HTTP/1.1\r\n")
    assert b"Host: 127.0.0.1:" + str(port).encode("ascii") + b"\r\n" in wire
    assert b"X-VoiceInk-ASR-Metadata: " + metadata + b"\r\n" in wire
    assert wire.count(b"X-VoiceInk-ASR-Nonce: test-nonce\r\n") == 1
    assert b"X-VoiceInk-ASR-Metadata: ey" not in wire
    assert b"safe transcript" not in wire
    assert wire.count(audio) == 1


def test_post_multipart_audio_uses_official_wav_form_and_auth_headers() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    received: list[bytes] = []

    def serve() -> None:
        connection, _ = listener.accept()
        try:
            connection.settimeout(1.0)
            data = bytearray()
            while b"\r\n\r\n" not in data:
                data.extend(connection.recv(4096))
            header_end = data.find(b"\r\n\r\n")
            content_length = int(
                next(
                    line.split(b":", 1)[1]
                    for line in data[:header_end].split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
            )
            while len(data) < header_end + 4 + content_length:
                data.extend(connection.recv(4096))
            received.append(bytes(data))
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        finally:
            connection.close()

    worker = Thread(target=serve, daemon=True)
    worker.start()
    pcm = b"\x01\x02\x03\x04"
    try:
        transport = UrllibLoopbackTransport(f"http://127.0.0.1:{port}", nonce="test-nonce")
        transport.set_api_key("test-api-key")
        response = transport.post_multipart_audio(
            "/v1/audio/transcriptions",
            memoryview(pcm),
            16000,
            "parakeet-tdt-0.6b-v3.oss-align.q8_0",
            "en",
            "verbose_json",
            timeout=1.0,
            max_response_bytes=8,
        )
    finally:
        worker.join(1.0)
        listener.close()

    assert response.body == b"ok"
    wire = received[0]
    headers, body = wire.split(b"\r\n\r\n", 1)
    assert headers.startswith(b"POST /v1/audio/transcriptions HTTP/1.1\r\n")
    assert b"Authorization: Bearer test-api-key" in headers
    assert b"X-VoiceInk-ASR-Nonce: test-nonce\r\n" in headers
    content_type = next(
        line.split(b":", 1)[1].strip()
        for line in headers.split(b"\r\n")
        if line.lower().startswith(b"content-type:")
    )
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    fields = {
        part.get_param("name", header="content-disposition"): part for part in message.iter_parts()
    }
    assert fields["model"].get_payload(decode=True) == b"parakeet-tdt-0.6b-v3.oss-align.q8_0"
    assert fields["language"].get_payload(decode=True) == b"en"
    assert fields["response_format"].get_payload(decode=True) == b"verbose_json"
    audio = fields["file"].get_payload(decode=True)
    assert fields["file"].get_filename() == "audio.wav"
    with wave.open(io.BytesIO(audio), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.readframes(wav.getnframes()) == pcm


def test_post_multipart_audio_cancellation_closes_connection_and_discards_result() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    request_received = Event()
    client_closed = Event()

    def serve() -> None:
        connection, _ = listener.accept()
        try:
            connection.settimeout(1.0)
            data = bytearray()
            while b"\r\n\r\n" not in data:
                data.extend(connection.recv(4096))
            header_end = data.find(b"\r\n\r\n")
            content_length = int(
                next(
                    line.split(b":", 1)[1]
                    for line in data[:header_end].split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
            )
            while len(data) < header_end + 4 + content_length:
                data.extend(connection.recv(4096))
            request_received.set()
            while connection.recv(1):
                pass
            client_closed.set()
        finally:
            connection.close()

    server = Thread(target=serve, daemon=True)
    server.start()
    source = CancellationTokenSource()
    outcome: list[BaseException | TransportResponse] = []

    def request() -> None:
        try:
            outcome.append(
                UrllibLoopbackTransport(
                    f"http://127.0.0.1:{port}", nonce="test-nonce"
                ).post_multipart_audio(
                    "/v1/audio/transcriptions",
                    memoryview(b"\x00\x00" * 16),
                    16000,
                    "parakeet-tdt-0.6b-v3.oss-align.q8_0",
                    None,
                    "json",
                    timeout=5.0,
                    max_response_bytes=8,
                    cancellation=source.token,
                )
            )
        except BaseException as error:
            outcome.append(error)

    client = Thread(target=request, daemon=True)
    client.start()
    try:
        assert request_received.wait(1.0)
        source.cancel()
        client.join(1.0)
        server.join(1.0)
    finally:
        listener.close()

    assert not client.is_alive()
    assert isinstance(outcome[0], CancellationError)
    assert client_closed.is_set()
