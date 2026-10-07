"""Run the VoiceInk-Win local ASR backend."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
from threading import Event

from voiceink_win.composition import (
    DEFAULT_READINESS_TIMEOUT_SECONDS,
    BackendApplication,
    build_application_from_environment,
)
from voiceink_win.domain import (
    AsrError,
    ConfigurationError,
    MissingModelError,
    RuntimeUnavailableError,
)


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a finite positive number") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the VoiceInk-Win local ASR backend")
    parser.add_argument("--endpoint", help="owned HTTP loopback endpoint")
    parser.add_argument(
        "--readiness-timeout",
        type=_positive_float,
        default=os.environ.get(
            "VOICEINK_READINESS_TIMEOUT", str(DEFAULT_READINESS_TIMEOUT_SECONDS)
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="start the sidecar, print readiness, and exit",
    )
    return parser


def _health_payload(application: BackendApplication) -> dict[str, object]:
    health = application.health()
    capabilities = application.capabilities()
    return {
        "status": health.status.value,
        "message": health.message,
        "backend": health.backend,
        "model_id": capabilities.model_id,
        "endpoint": application.endpoint,
    }


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True), flush=True)


def _exit_code(error: BaseException) -> int:
    if isinstance(error, (ConfigurationError, MissingModelError)):
        return 2
    return 3


def _build_from_args(args: argparse.Namespace) -> BackendApplication:
    return build_application_from_environment(
        endpoint=args.endpoint,
        readiness_timeout=args.readiness_timeout,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    application: BackendApplication | None = None
    stop = Event()
    handlers: dict[int, object] = {}
    return_code = 3
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda _signum, _frame: stop.set())
        application = _build_from_args(args)
        application.start()
        payload = _health_payload(application)
        if payload["status"] != "ready":
            raise RuntimeUnavailableError(payload["message"] or "sidecar is not ready")
        _print_json(payload)
        if args.once:
            return_code = 0
        else:
            stop.wait()
            return_code = 0
    except KeyboardInterrupt:
        return_code = 0
    except AsrError as error:
        _print_json({"error": {"code": error.code.value, "message": error.message}})
        return_code = _exit_code(error)
    except Exception:
        _print_json({"error": {"code": "execution", "message": "backend startup failed"}})
        return_code = 3
    finally:
        if application is not None:
            try:
                application.close()
            except AsrError as error:
                _print_json({"error": {"code": error.code.value, "message": error.message}})
                if return_code == 0:
                    return_code = 4
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
