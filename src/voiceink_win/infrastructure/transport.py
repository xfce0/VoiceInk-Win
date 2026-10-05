"""Injected loopback transport boundary and its stdlib implementation."""

from __future__ import annotations

import errno
import io
import selectors
import socket
import wave
from dataclasses import dataclass
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from voiceink_win.domain import CancellationError, ConfigurationError, MonotonicClock, ProtocolError

from .authentication import ASR_AUTHORIZATION_HEADER, ASR_NONCE_HEADER

_MAX_RESPONSE_HEADER_BYTES = 64 * 1024
_MAX_RESPONSE_CHUNK_OVERHEAD_BYTES = 64 * 1024
_RECV_CHUNK_BYTES = 64 * 1024


def _origin_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise ProtocolError("sidecar request path must be a non-empty origin-form path")
    if any(ord(char) <= 0x20 or ord(char) >= 0x7F for char in path):
        raise ProtocolError("sidecar request path contains invalid characters")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ProtocolError("sidecar request path must not contain an authority or query")
    return f"/{parsed.path.lstrip('/')}"


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    body: bytes


class UrllibLoopbackTransport:
    def __init__(
        self, endpoint: str, *, clock: MonotonicClock | None = None, nonce: str | None = None
    ) -> None:
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
            raise ConfigurationError("transport endpoint must be an HTTP loopback URL")
        if parsed.port is None or not 1 <= parsed.port <= 65535:
            raise ConfigurationError("transport endpoint must include a valid port")
        self._endpoint = endpoint.rstrip("/")
        self._clock = clock
        self._nonce = nonce
        self._api_key: str | None = None

    def set_clock(self, clock: MonotonicClock) -> None:
        self._clock = clock

    def set_nonce(self, nonce: str) -> None:
        if not isinstance(nonce, str) or not nonce:
            raise ConfigurationError("sidecar nonce must be non-empty")
        self._nonce = nonce

    def set_api_key(self, api_key: str) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ConfigurationError("sidecar API key must be non-empty")
        self._api_key = api_key

    def post_multipart_audio(
        self,
        path: str,
        pcm: memoryview,
        sample_rate: int,
        model: str,
        language: str | None,
        response_format: str,
        timeout: float | None,
        max_response_bytes: int,
        cancellation=None,
        deadline: float | None = None,
        nonce: str | None = None,
    ) -> TransportResponse:
        """Send canonical PCM as the WAV multipart form expected by NeMo-Speech.cpp."""
        if cancellation is not None and cancellation.is_cancelled():
            raise CancellationError("sidecar request was cancelled")
        if deadline is not None and self._now() >= deadline:
            raise TimeoutError("sidecar request timed out")
        boundary = "----VoiceInkASR" + self._effective_nonce(nonce)[:16]
        wav_body = _wav_bytes(pcm, sample_rate)
        fields = {"model": model, "response_format": response_format}
        if language is not None:
            fields["language"] = language
        body = bytearray()
        for name, value in fields.items():
            field_header = (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            )
            body.extend(field_header.encode())
        file_header = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'
        )
        body.extend(file_header.encode())
        body.extend(wav_body)
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        request = Request(
            f"{self._endpoint}{_origin_path(path)}",
            data=bytes(body),
            headers={
                **self._headers(f"multipart/form-data; boundary={boundary}", nonce),
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return TransportResponse(
                    response.status, self._read_bounded(response, max_response_bytes)
                )
        except HTTPError as error:
            return TransportResponse(error.code, self._read_bounded(error, max_response_bytes))

    def post(
        self,
        path: str,
        body: bytes,
        timeout: float | None,
        max_response_bytes: int,
        nonce: str | None = None,
    ) -> TransportResponse:
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        request = Request(
            f"{self._endpoint}{_origin_path(path)}",
            data=body,
            headers=self._headers("application/json", nonce),
            method="POST",
        )
        try:
            if timeout is None:
                with urlopen(request) as response:
                    return TransportResponse(
                        response.status, self._read_bounded(response, max_response_bytes)
                    )
            with urlopen(request, timeout=timeout) as response:
                return TransportResponse(
                    response.status, self._read_bounded(response, max_response_bytes)
                )
        except HTTPError as error:
            return TransportResponse(error.code, self._read_bounded(error, max_response_bytes))

    def post_audio(
        self,
        path: str,
        metadata: bytes,
        pcm: memoryview,
        timeout: float | None,
        max_response_bytes: int,
        cancellation=None,
        deadline: float | None = None,
        nonce: str | None = None,
    ) -> TransportResponse:
        """Send metadata and PCM separately so audio is never hex/base64 copied."""
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        if cancellation is not None and cancellation.is_cancelled():
            raise CancellationError("sidecar request was cancelled")
        request_path = _origin_path(path)
        authenticated_nonce = self._effective_nonce(nonce)
        parsed = urlsplit(self._endpoint)
        if parsed.port is None:
            raise ConfigurationError("transport endpoint must include a port")
        operation_deadline = (
            deadline
            if deadline is not None
            else self._now() + timeout
            if timeout is not None
            else None
        )
        connection = self._connect_cancellable(
            parsed.hostname, parsed.port, operation_deadline, cancellation
        )
        connection.setblocking(False)
        try:
            if len(metadata) > _MAX_RESPONSE_HEADER_BYTES or b"\r" in metadata or b"\n" in metadata:
                raise ProtocolError("sidecar metadata is not a valid header value")
            if parsed.path.rstrip("/"):
                request_path = f"{parsed.path.rstrip('/')}{request_path}"
            host = parsed.hostname
            host_header = f"[{host}]" if ":" in host else host
            request = (
                f"POST {request_path} HTTP/1.1\r\n"
                f"Host: {host_header}:{parsed.port}\r\n"
                "Connection: close\r\n"
                "Content-Type: application/octet-stream\r\n"
                f"Content-Length: {len(pcm)}\r\n"
                f"{ASR_NONCE_HEADER}: {authenticated_nonce}\r\n"
                "X-VoiceInk-ASR-Metadata: "
            ).encode("ascii")
            request += metadata + b"\r\n\r\n"
            with selectors.DefaultSelector() as selector:
                selector.register(connection, selectors.EVENT_WRITE)
                self._send_cancellable(
                    selector, connection, request, operation_deadline, cancellation
                )
                self._send_cancellable(selector, connection, pcm, operation_deadline, cancellation)
                selector.modify(connection, selectors.EVENT_READ)
                return self._receive_response(
                    selector, connection, operation_deadline, cancellation, max_response_bytes
                )
        finally:
            connection.close()

    def _connect_cancellable(self, host, port, deadline, cancellation) -> socket.socket:
        if host in {"localhost", "127.0.0.1"}:
            addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port))]
        else:
            addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", port, 0, 0))]
        last_error: OSError | None = None
        for family, socket_type, protocol, _, address in addresses:
            if cancellation is not None and cancellation.is_cancelled():
                raise CancellationError("sidecar request was cancelled")
            connection = socket.socket(family, socket_type, protocol)
            connection.setblocking(False)
            error_code = connection.connect_ex(address)
            if error_code == 0:
                return connection
            if error_code not in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                last_error = OSError(error_code, "sidecar connection failed")
                connection.close()
                continue
            with selectors.DefaultSelector() as selector:
                selector.register(connection, selectors.EVENT_WRITE)
                while True:
                    self._wait_for_io(selector, selectors.EVENT_WRITE, deadline, cancellation)
                    socket_error = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    if socket_error == 0:
                        return connection
                    last_error = OSError(socket_error, "sidecar connection failed")
                    break
            connection.close()
        if last_error is not None:
            raise last_error
        raise OSError("sidecar connection failed")

    def _send_cancellable(
        self,
        selector,
        connection: socket.socket,
        payload: bytes | memoryview,
        deadline: float | None,
        cancellation,
    ) -> None:
        pending = memoryview(payload)
        while pending:
            self._wait_for_io(selector, selectors.EVENT_WRITE, deadline, cancellation)
            try:
                sent = connection.send(pending)
            except BlockingIOError:
                continue
            pending = pending[sent:]

    def _receive_response(
        self, selector, connection, deadline, cancellation, max_response_bytes: int
    ) -> TransportResponse:
        raw = bytearray()
        while True:
            header_end = raw.find(b"\r\n\r\n")
            if header_end < 0:
                if len(raw) >= _MAX_RESPONSE_HEADER_BYTES:
                    raise ProtocolError("sidecar response headers exceed the configured limit")
                read_limit = min(_RECV_CHUNK_BYTES, _MAX_RESPONSE_HEADER_BYTES - len(raw))
            else:
                _, fields = self._parse_headers(raw, max_response_bytes)
                if b"content-length" in fields:
                    try:
                        expected = int(fields[b"content-length"])
                    except ValueError as error:
                        raise ProtocolError(
                            "sidecar response content length is malformed"
                        ) from error
                    if expected < 0 or expected > max_response_bytes:
                        raise ProtocolError("sidecar response exceeds the configured byte limit")
                    if expected == 0:
                        status_code, _ = self._parse_headers(raw, max_response_bytes)
                        return TransportResponse(status_code, b"")
                    read_limit = header_end + 4 + expected - len(raw)
                else:
                    overhead = (
                        _MAX_RESPONSE_CHUNK_OVERHEAD_BYTES
                        if fields.get(b"transfer-encoding", b"").lower() == b"chunked"
                        else 1
                    )
                    read_limit = header_end + 4 + max_response_bytes + overhead - len(raw)
                if read_limit <= 0:
                    raise ProtocolError("sidecar response exceeds the configured byte limit")
                read_limit = min(_RECV_CHUNK_BYTES, read_limit)
            self._wait_for_io(selector, selectors.EVENT_READ, deadline, cancellation)
            try:
                chunk = connection.recv(read_limit)
            except BlockingIOError:
                continue
            if chunk:
                if len(chunk) > read_limit:
                    raise ProtocolError("sidecar response buffer limit was exceeded")
                raw.extend(chunk)
                response = self._parse_response(raw, max_response_bytes, complete=False)
                if response is not None:
                    return response
                continue
            response = self._parse_response(raw, max_response_bytes, complete=True)
            if response is None:
                raise ProtocolError("sidecar response ended before the complete body was received")
            return response

    def _wait_for_io(self, selector, event, deadline: float | None, cancellation) -> None:
        if cancellation is not None and cancellation.is_cancelled():
            raise CancellationError("sidecar request was cancelled")
        if deadline is not None and self._now() >= deadline:
            raise TimeoutError("sidecar request timed out")
        remaining = None if deadline is None else max(0.0, deadline - self._now())
        while not selector.select(0.05 if remaining is None else min(0.05, remaining)):
            if cancellation is not None and cancellation.is_cancelled():
                raise CancellationError("sidecar request was cancelled")
            if deadline is not None and self._now() >= deadline:
                raise TimeoutError("sidecar request timed out")

    def _now(self) -> float:
        return self._clock.monotonic() if self._clock is not None else monotonic()

    def _parse_response(
        self, raw: bytearray, max_response_bytes: int, *, complete: bool
    ) -> TransportResponse | None:
        header_end = raw.find(b"\r\n\r\n")
        if header_end < 0:
            if len(raw) >= _MAX_RESPONSE_HEADER_BYTES:
                raise ProtocolError("sidecar response headers exceed the configured limit")
            return None
        status_code, fields = self._parse_headers(raw, max_response_bytes)
        body = raw[header_end + 4 :]
        if b"content-length" in fields:
            try:
                expected = int(fields[b"content-length"])
            except ValueError as error:
                raise ProtocolError("sidecar response content length is malformed") from error
            if expected < 0 or expected > max_response_bytes:
                raise ProtocolError("sidecar response exceeds the configured byte limit")
            if len(body) > expected:
                raise ProtocolError("sidecar response body exceeds content length")
            if len(body) < expected:
                if complete:
                    raise ProtocolError("sidecar response ended before the complete body")
                return None
            return TransportResponse(status_code, bytes(body))
        if b"transfer-encoding" in fields and fields[b"transfer-encoding"].lower() == b"chunked":
            decoded = self._decode_chunked(body, max_response_bytes, complete)
            if decoded is None and complete:
                raise ProtocolError("sidecar response ended before the complete body")
            return None if decoded is None else TransportResponse(status_code, decoded)
        if len(body) > max_response_bytes:
            raise ProtocolError("sidecar response exceeds the configured byte limit")
        return TransportResponse(status_code, bytes(body)) if complete else None

    @staticmethod
    def _parse_headers(raw: bytearray, max_response_bytes: int) -> tuple[int, dict[bytes, bytes]]:
        del max_response_bytes
        header_end = raw.find(b"\r\n\r\n")
        if header_end < 0:
            raise ProtocolError("sidecar response headers are incomplete")
        if header_end + 4 > _MAX_RESPONSE_HEADER_BYTES:
            raise ProtocolError("sidecar response headers exceed the configured limit")
        header = bytes(raw[:header_end]).split(b"\r\n")
        try:
            status_code = int(header[0].split(b" ", 2)[1])
            fields = {
                name.lower(): value.strip()
                for name, value in (line.split(b":", 1) for line in header[1:])
            }
        except (IndexError, ValueError) as error:
            raise ProtocolError("sidecar response headers are malformed") from error
        return status_code, fields

    def _decode_chunked(self, body: bytes, max_response_bytes: int, complete: bool) -> bytes | None:
        decoded = bytearray()
        offset = 0
        while True:
            line_end = body.find(b"\r\n", offset)
            if line_end < 0:
                return None
            try:
                size = int(body[offset:line_end].split(b";", 1)[0], 16)
            except ValueError as error:
                raise ProtocolError("sidecar chunk size is malformed") from error
            if size < 0 or size > max_response_bytes - len(decoded):
                raise ProtocolError("sidecar response exceeds the configured byte limit")
            offset = line_end + 2
            if len(body) < offset + size + 2:
                return None
            decoded.extend(body[offset : offset + size])
            if len(decoded) > max_response_bytes:
                raise ProtocolError("sidecar response exceeds the configured byte limit")
            offset += size
            if body[offset : offset + 2] != b"\r\n":
                raise ProtocolError("sidecar chunk terminator is malformed")
            offset += 2
            if size == 0:
                return bytes(decoded)
            if complete and offset == len(body):
                return None

    def _read_bounded(self, response, max_response_bytes: int) -> bytes:
        headers = getattr(response, "headers", None)
        declared = headers.get("Content-Length") if headers is not None else None
        if declared is not None:
            try:
                if int(declared) < 0 or int(declared) > max_response_bytes:
                    raise ProtocolError("sidecar response exceeds the configured byte limit")
            except ValueError as error:
                raise ProtocolError("sidecar response content length is malformed") from error
        body = bytearray()
        while len(body) <= max_response_bytes:
            chunk = response.read(min(_RECV_CHUNK_BYTES, max_response_bytes + 1 - len(body)))
            if not chunk:
                return bytes(body)
            if len(chunk) > max_response_bytes + 1 - len(body):
                raise ProtocolError("sidecar response buffer limit was exceeded")
            body.extend(chunk)
            if len(body) > max_response_bytes:
                raise ProtocolError("sidecar response exceeds the configured byte limit")
        raise ProtocolError("sidecar response exceeds the configured byte limit")

    def close(self) -> None:
        pass

    def _effective_nonce(self, nonce: str | None) -> str:
        value = nonce if nonce is not None else self._nonce
        if not isinstance(value, str) or not value:
            raise ConfigurationError("sidecar transport nonce is not configured")
        try:
            value.encode("ascii")
        except UnicodeEncodeError as error:
            raise ConfigurationError("sidecar transport nonce must be ASCII") from error
        return value

    def _headers(self, content_type: str, nonce: str | None) -> dict[str, str]:
        headers = {"Content-Type": content_type, ASR_NONCE_HEADER: self._effective_nonce(nonce)}
        if self._api_key is not None:
            headers[ASR_AUTHORIZATION_HEADER] = f"Bearer {self._api_key}"
        return headers


def _wav_bytes(pcm: memoryview, sample_rate: int) -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm)
    return stream.getvalue()
