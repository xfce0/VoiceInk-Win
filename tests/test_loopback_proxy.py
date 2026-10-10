from __future__ import annotations

import socket
import time
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
    target_address = ("127.0.0.1", target.getsockname()[1])
    target_endpoint = f"http://{target_address[0]}:{target_address[1]}"
    server_stop = Event()
    server_errors: list[BaseException] = []

    def serve_once() -> None:
        try:
            while not server_stop.is_set():
                try:
                    connection, _ = target.accept()
                except TimeoutError:
                    continue
                if server_stop.is_set():
                    connection.close()
                    return
                with connection:
                    connection.settimeout(_SERVER_JOIN_TIMEOUT_SECONDS)
                    payload = connection.recv(64 * 1024)
                    if not payload:
                        raise AssertionError("loopback target received no request")
                    connection.sendall(payload)
                return
        except BaseException as error:
            if not server_stop.is_set():
                server_errors.append(error)

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
            server_stop.set()
            try:
                with socket.create_connection(target_address, timeout=1.0):
                    pass
            except OSError:
                pass
            target.close()
            server.join(timeout=_SERVER_JOIN_TIMEOUT_SECONDS)

    assert not server.is_alive()
    assert not server_errors, f"loopback target failed: {server_errors[0]!r}"


def test_loopback_proxy_keeps_quiet_long_running_request_alive() -> None:
    target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    target.bind(("127.0.0.1", 0))
    target.listen(1)
    target.settimeout(_SERVER_ACCEPT_TIMEOUT_SECONDS)
    target_address = ("127.0.0.1", target.getsockname()[1])
    target_endpoint = f"http://{target_address[0]}:{target_address[1]}"
    server_stop = Event()
    server_errors: list[BaseException] = []

    def serve_once() -> None:
        try:
            connection, _ = target.accept()
            with connection:
                connection.settimeout(_SERVER_JOIN_TIMEOUT_SECONDS)
                request = connection.recv(64 * 1024)
                if not request:
                    raise AssertionError("loopback target received no request")
                time.sleep(0.4)
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        except BaseException as error:
            if not server_stop.is_set():
                server_errors.append(error)

    server = Thread(target=serve_once, daemon=True)
    server.start()
    proxy = None
    try:
        proxy = LoopbackProxy(
            target_endpoint,
            idle_timeout_seconds=0.5,
            total_timeout_seconds=1.0,
        )
        proxy.start()
        parsed = urlsplit(proxy.endpoint)
        with socket.create_connection((parsed.hostname, parsed.port), timeout=2.0) as client:
            client.settimeout(1.0)
            client.sendall(b"GET /ready HTTP/1.1\r\n\r\n")
            response = client.recv(64 * 1024)
    finally:
        try:
            if proxy is not None:
                proxy.close()
        finally:
            server_stop.set()
            try:
                with socket.create_connection(target_address, timeout=1.0):
                    pass
            except OSError:
                pass
            target.close()
            server.join(timeout=_SERVER_JOIN_TIMEOUT_SECONDS)

    assert not server.is_alive()
    assert not server_errors, f"loopback target failed: {server_errors[0]!r}"
    assert response.startswith(b"HTTP/1.1 200 OK")
