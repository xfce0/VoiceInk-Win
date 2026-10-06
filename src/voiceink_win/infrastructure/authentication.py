"""Authentication primitives for the owned loopback ASR boundary."""

from __future__ import annotations

import hmac
import secrets

ASR_NONCE_HEADER = "X-VoiceInk-ASR-Nonce"
ASR_NONCE_ENV = "VOICEINK_ASR_NONCE"
ASR_AUTHORIZATION_HEADER = "Authorization"


def generate_nonce() -> str:
    return secrets.token_urlsafe(32)


def validate_nonce(candidate: object, expected: object) -> bool:
    if not isinstance(candidate, str) or not isinstance(expected, str):
        return False
    if not candidate or not expected:
        return False
    try:
        candidate_bytes = candidate.encode("ascii", "strict")
        expected_bytes = expected.encode("ascii", "strict")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(candidate_bytes, expected_bytes)
