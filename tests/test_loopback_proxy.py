from __future__ import annotations

import socket
from threading import Thread
from urllib.parse import urlsplit

from voiceink_win.infrastructure import LoopbackProxy


def test_loopback_proxy_forwards_bidirectional_bytes() -> None:
    target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    target.bind(("127.0.0.1", 0))
    target.listen(1)
    target_endpoint = f"http://127.0.0.1:{target.getsockname()[1]}"

    def serve_once() -> None:
        connection, _ = target.accept()
        with connection:
            connection.sendall(connection.recv(64 * 1024))

    server = Thread(target=serve_once)
    server.start()
    proxy = LoopbackProxy(target_endpoint)
    proxy.start()
    try:
        parsed = urlsplit(proxy.endpoint)
        with socket.create_connection((parsed.hostname, parsed.port), timeout=2.0) as client:
            client.sendall(b"GET /ready HTTP/1.1\r\n\r\n")
            assert client.recv(64 * 1024) == b"GET /ready HTTP/1.1\r\n\r\n"
    finally:
        proxy.close()
        target.close()
        server.join(timeout=2.0)

    assert not server.is_alive()
