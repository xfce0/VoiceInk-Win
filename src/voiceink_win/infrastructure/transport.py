"""Injected loopback transport boundary and its stdlib implementation."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from voiceink_win.domain import ConfigurationError, ProtocolError


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status_code: int
    body: bytes


class UrllibLoopbackTransport:
    def __init__(self, endpoint: str) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ConfigurationError("transport endpoint must be an HTTP loopback URL")
        self._endpoint = endpoint.rstrip("/")

    def post(
        self,
        path: str,
        body: bytes,
        timeout: float | None,
        max_response_bytes: int,
    ) -> TransportResponse:
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        request = Request(
            f"{self._endpoint}/{path.lstrip('/')}",
            data=body,
            headers={"Content-Type": "application/json"},
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

    def _read_bounded(self, response, max_response_bytes: int) -> bytes:
        body = response.read(max_response_bytes + 1)
        if len(body) > max_response_bytes:
            raise ProtocolError("sidecar response exceeds the configured byte limit")
        return body

    def close(self) -> None:
        pass
