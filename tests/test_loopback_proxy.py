from __future__ import annotations

import socket
from threading import Thread
from urllib.parse import urlsplit

from voiceink_win.infrastructure import LoopbackProxy


def test_loopback_proxy_forwards_bidirectional_bytes() -> None:
    target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    target.bind(("127.0.0.1", 0))
    target.listen(1)
    target_address = ("127.0.0.1", target.getsockname()[1])
    target_endpoint = f"http://{target_address[0]}:{target_address[1]}"

    def serve_once() -> None:
        try:
            connection, _ = target.accept()
        except OSError:
            return
        with connection:
            connection.settimeout(2.0)
            connection.sendall(connection.recv(64 * 1024))

    server = Thread(target=serve_once, daemon=True)
    server.start()
    proxy = None
    try:
        proxy = LoopbackProxy(target_endpoint)
        parsed = urlsplit(proxy.endpoint)
        assert proxy.endpoint != target_endpoint
        with socket.create_connection((parsed.hostname, parsed.port), timeout=2.0) as client:
            proxy.start()
            client.sendall(b"GET /ready HTTP/1.1\r\n\r\n")
            assert client.recv(64 * 1024) == b"GET /ready HTTP/1.1\r\n\r\n"
    finally:
        try:
            if proxy is not None:
                proxy.close()
        finally:
            try:
                with socket.create_connection(target_address, timeout=1.0):
                    pass
            except OSError:
                pass
            target.close()
            server.join(timeout=3.0)

    assert not server.is_alive()
