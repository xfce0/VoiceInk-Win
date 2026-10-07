from __future__ import annotations

import socket
from threading import Event, Thread
from urllib.parse import urlsplit

from voiceink_win.infrastructure import LoopbackProxy

_SERVER_ACCEPT_TIMEOUT_SECONDS = 0.25
_SERVER_JOIN_TIMEOUT_SECONDS = 1.0


def test_loopback_proxy_forwards_bidirectional_bytes() -> None:
    target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    target.bind(("127.0.0.1", 0))
    target.listen(1)
    target.settimeout(_SERVER_ACCEPT_TIMEOUT_SECONDS)
    target_endpoint = f"http://127.0.0.1:{target.getsockname()[1]}"
    server_stop = Event()
    server_errors: list[BaseException] = []

    def serve_once() -> None:
        try:
            while not server_stop.is_set():
                try:
                    connection, _ = target.accept()
                except TimeoutError:
                    continue
                connection.settimeout(_SERVER_JOIN_TIMEOUT_SECONDS)
                with connection:
                    payload = connection.recv(64 * 1024)
                    if not payload:
                        raise AssertionError("loopback target received no request")
                    connection.sendall(payload)
                return
        except BaseException as error:
            server_errors.append(error)

    server = Thread(target=serve_once, daemon=True)
    server.start()
    proxy = LoopbackProxy(target_endpoint)
    try:
        parsed = urlsplit(proxy.endpoint)
        assert proxy.endpoint != target_endpoint
        with socket.create_connection((parsed.hostname, parsed.port), timeout=2.0) as client:
            proxy.start()
            client.sendall(b"GET /ready HTTP/1.1\r\n\r\n")
            assert client.recv(64 * 1024) == b"GET /ready HTTP/1.1\r\n\r\n"
    finally:
        try:
            proxy.close()
        finally:
            server_stop.set()
            server.join(timeout=_SERVER_JOIN_TIMEOUT_SECONDS)
            target.close()

    assert not server.is_alive()
    assert not server_errors, f"loopback target failed: {server_errors[0]!r}"
