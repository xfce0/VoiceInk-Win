"""Owned loopback listener forwarding traffic to the isolated ASR sidecar."""

from __future__ import annotations

import select
import socket
import time
from threading import BoundedSemaphore, Event, Lock, Thread, current_thread
from urllib.parse import urlsplit

from voiceink_win.domain import ConfigurationError, RuntimeRecoveryPendingError

_BUFFER_BYTES = 64 * 1024
_MAX_HEADER_BYTES = 64 * 1024
_MAX_REQUEST_BYTES = 66 * 1024 * 1024
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_BACKLOG = 16
_CONNECT_TIMEOUT_SECONDS = 2.0
_IDLE_TIMEOUT_SECONDS = 30.0
_TOTAL_TIMEOUT_SECONDS = 120.0
_SELECT_TIMEOUT_SECONDS = 0.25
_ALLOWED_REQUESTS = {
    ("GET", "/ready"),
    ("GET", "/v1/models"),
    ("POST", "/v1/audio/transcriptions"),
}


def _parse_endpoint(endpoint: str, *, allow_zero: bool = False) -> tuple[str, int]:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError("loopback proxy endpoint must be an HTTP loopback URL")
    minimum = 0 if allow_zero else 1
    if parsed.port is None or not minimum <= parsed.port <= 65535:
        raise ConfigurationError("loopback proxy endpoint must include a valid port")
    return parsed.hostname, parsed.port


class LoopbackProxy:
    """Own one listener and relay bytes to the private sidecar endpoint."""

    def __init__(self, target_endpoint: str, listen_endpoint: str | None = None) -> None:
        target_host, target_port = _parse_endpoint(target_endpoint)
        listen_host, listen_port = _parse_endpoint(
            listen_endpoint or "http://127.0.0.1:0", allow_zero=True
        )
        self._target = (target_host, target_port)
        family = socket.AF_INET6 if listen_host == "::1" else socket.AF_INET
        self._listener = socket.socket(family, socket.SOCK_STREAM)
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self._listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        else:
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bind_address = (
            (listen_host, listen_port, 0, 0)
            if family == socket.AF_INET6
            else (
                listen_host,
                listen_port,
            )
        )
        self._listener.bind(bind_address)
        self._listener.listen(_BACKLOG)
        self._listener.settimeout(_SELECT_TIMEOUT_SECONDS)
        bound_host, bound_port = self._listener.getsockname()[:2]
        formatted_host = f"[{bound_host}]" if family == socket.AF_INET6 else bound_host
        self._endpoint = f"http://{formatted_host}:{bound_port}"
        self._stop = Event()
        self._thread: Thread | None = None
        self._connections: set[socket.socket] = set()
        self._connections_lock = Lock()
        self._workers: set[Thread] = set()
        self._connection_slots = BoundedSemaphore(_BACKLOG)
        self._closed = False

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def start(self) -> None:
        if self._stop.is_set():
            raise ConfigurationError("loopback proxy is closed")
        if self._thread is not None:
            return
        self._thread = Thread(target=self._accept_loop, name="asr-loopback-proxy", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._stop.set()
        workers: tuple[Thread, ...] = ()
        try:
            self._listener.close()
        finally:
            with self._connections_lock:
                connections = tuple(self._connections)
            for connection in connections:
                self._close_socket(connection)
            if self._thread is not None and self._thread is not current_thread():
                self._thread.join(timeout=1.0)
            with self._connections_lock:
                workers = tuple(self._workers)
            for worker in workers:
                if worker is not current_thread():
                    worker.join(timeout=1.0)
        if (self._thread is not None and self._thread.is_alive()) or any(
            worker.is_alive() for worker in workers
        ):
            raise RuntimeRecoveryPendingError("loopback proxy cleanup remains pending")
        self._closed = True

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except (OSError, TimeoutError):
                continue
            if not self._connection_slots.acquire(blocking=False):
                self._close_socket(client)
                continue
            worker = Thread(target=self._relay_connection, args=(client,), daemon=True)
            with self._connections_lock:
                self._connections.add(client)
                self._workers.add(worker)
            worker.start()

    def _relay_connection(self, client: socket.socket) -> None:
        target: socket.socket | None = None
        try:
            target = socket.create_connection(self._target, timeout=_CONNECT_TIMEOUT_SECONDS)
            with self._connections_lock:
                self._connections.add(target)
            client.setblocking(False)
            target.setblocking(False)
            self._relay(client, target)
        except (OSError, ValueError):
            pass
        finally:
            self._close_socket(client)
            if target is not None:
                self._close_socket(target)
            with self._connections_lock:
                self._workers.discard(current_thread())
            self._connection_slots.release()

    def _relay(self, client: socket.socket, target: socket.socket) -> None:
        deadline = time.monotonic() + _TOTAL_TIMEOUT_SECONDS
        request = self._read_request(client, deadline)
        self._send_all(target, request, deadline)
        request_bytes = len(request)
        response_bytes = 0
        last_activity = time.monotonic()
        sockets = (client, target)
        while not self._stop.is_set() and time.monotonic() < deadline:
            if time.monotonic() - last_activity > _IDLE_TIMEOUT_SECONDS:
                return
            readable, _, exceptional = select.select(sockets, (), sockets, _SELECT_TIMEOUT_SECONDS)
            if exceptional:
                return
            for source in readable:
                payload = source.recv(_BUFFER_BYTES)
                if not payload:
                    return
                destination = target if source is client else client
                if source is client and response_bytes:
                    return
                if source is client:
                    request_bytes += len(payload)
                    if request_bytes > _MAX_REQUEST_BYTES:
                        return
                else:
                    response_bytes += len(payload)
                    if response_bytes > _MAX_RESPONSE_BYTES:
                        return
                self._send_all(destination, payload, deadline)
                last_activity = time.monotonic()

    def _read_request(self, client: socket.socket, deadline: float) -> bytes:
        request = bytearray()
        while b"\r\n\r\n" not in request:
            if len(request) > _MAX_HEADER_BYTES:
                raise ValueError("HTTP request headers exceed proxy limit")
            if time.monotonic() >= deadline:
                raise TimeoutError("HTTP request header timed out")
            readable, _, _ = select.select(
                (client,), (), (), min(_SELECT_TIMEOUT_SECONDS, deadline - time.monotonic())
            )
            if not readable:
                continue
            chunk = client.recv(_BUFFER_BYTES)
            if not chunk:
                raise ValueError("HTTP request ended before headers")
            request.extend(chunk)
        header_end = request.index(b"\r\n\r\n") + 4
        self._validate_request(bytes(request[:header_end]))
        return bytes(request)

    @staticmethod
    def _validate_request(headers: bytes) -> None:
        try:
            lines = headers[:-4].decode("ascii").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("malformed HTTP request") from error
        path = urlsplit(target)
        if version not in {"HTTP/1.0", "HTTP/1.1"} or path.query or path.fragment:
            raise ValueError("unsupported HTTP request")
        if (method, path.path) not in _ALLOWED_REQUESTS:
            raise ValueError("HTTP request is not allowed through the ASR proxy")
        content_length = 0
        for header in lines[1:]:
            name, separator, value = header.partition(":")
            if not separator:
                raise ValueError("malformed HTTP header")
            if name.casefold() == "transfer-encoding":
                raise ValueError("chunked HTTP requests are not allowed")
            if name.casefold() == "content-length":
                try:
                    content_length = int(value.strip())
                except ValueError as error:
                    raise ValueError("invalid HTTP content length") from error
        if content_length < 0 or content_length > _MAX_REQUEST_BYTES:
            raise ValueError("HTTP request body exceeds proxy limit")

    @staticmethod
    def _send_all(destination: socket.socket, payload: bytes, deadline: float) -> None:
        offset = 0
        while offset < len(payload):
            if time.monotonic() >= deadline:
                raise TimeoutError("HTTP relay timed out")
            _, writable, _ = select.select(
                (), (destination,), (), min(_SELECT_TIMEOUT_SECONDS, deadline - time.monotonic())
            )
            if not writable:
                continue
            try:
                offset += destination.send(payload[offset:])
            except BlockingIOError:
                continue

    def _close_socket(self, connection: socket.socket) -> None:
        with self._connections_lock:
            self._connections.discard(connection)
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass
