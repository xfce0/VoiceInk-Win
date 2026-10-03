from __future__ import annotations

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
