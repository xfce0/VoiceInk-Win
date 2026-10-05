from __future__ import annotations

import socket
from threading import Thread

import pytest

from voiceink_win.domain import ProtocolError
from voiceink_win.infrastructure import TransportResponse, UrllibLoopbackTransport


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
    transport = UrllibLoopbackTransport("http://127.0.0.1:8123")

    with pytest.raises(ProtocolError):
        transport.post("/health", b"{}", timeout=1.0, max_response_bytes=8)


def test_transport_response_is_a_small_infrastructure_value() -> None:
    response = TransportResponse(200, b"ok")

    assert response.status_code == 200
    assert response.body == b"ok"


@pytest.mark.parametrize("body", [b"", b"x" * 8])
def test_urllib_transport_accepts_response_at_or_below_limit(monkeypatch, body: bytes) -> None:
    monkeypatch.setattr(
        "voiceink_win.infrastructure.transport.urlopen",
        lambda *args, **kwargs: SizedHttpResponse(body),
    )

    response = UrllibLoopbackTransport("http://127.0.0.1:8123").post(
        "/health", b"{}", timeout=1.0, max_response_bytes=8
    )

    assert response.body == body


def test_transport_rejects_declared_oversized_body_before_reading(monkeypatch) -> None:
    response = SizedHttpResponse(b"", {"Content-Length": "9"})
    monkeypatch.setattr(
        "voiceink_win.infrastructure.transport.urlopen", lambda *args, **kwargs: response
    )

    with pytest.raises(ProtocolError):
        UrllibLoopbackTransport("http://127.0.0.1:8123").post(
            "/health", b"{}", timeout=1.0, max_response_bytes=8
        )
    assert response.body == b""


def test_transport_rejects_chunked_body_above_limit() -> None:
    transport = UrllibLoopbackTransport("http://127.0.0.1:8123")

    with pytest.raises(ProtocolError):
        transport._parse_response(
            bytearray(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n9\r\n123456789\r\n"),
            8,
            complete=False,
        )


def test_transport_rejects_truncated_content_length_response() -> None:
    transport = UrllibLoopbackTransport("http://127.0.0.1:8123")

    with pytest.raises(ProtocolError):
        transport._parse_response(
            bytearray(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nx"),
            8,
            complete=True,
        )


def test_transport_accepts_zero_length_content_length_response() -> None:
    transport = UrllibLoopbackTransport("http://127.0.0.1:8123")

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
    transport = UrllibLoopbackTransport("http://127.0.0.1:8123")
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
        response = UrllibLoopbackTransport(f"http://127.0.0.1:{port}").post_audio(
            "/transcribe", metadata, memoryview(audio), timeout=1.0, max_response_bytes=8
        )
    finally:
        worker.join(1.0)
        listener.close()

    assert response.body == b"ok"
    assert len(received) == 1
    wire = received[0]
    assert wire.startswith(b"POST /transcribe HTTP/1.1\r\n")
    assert b"Host: 127.0.0.1:" + str(port).encode("ascii") + b"\r\n" in wire
    assert b"X-VoiceInk-ASR-Metadata: " + metadata + b"\r\n" in wire
    assert b"X-VoiceInk-ASR-Metadata: ey" not in wire
    assert b"safe transcript" not in wire
    assert audio in wire
