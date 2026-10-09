"""PyInstaller entrypoint for the real PySide6 frontend shell."""

from __future__ import annotations

import sys

from voiceink_win.presentation.app import main

if __name__ == "__main__":
    smoke = "--smoke" in sys.argv
    package_smoke = "--package-smoke" in sys.argv
    if smoke:
        sys.argv.remove("--smoke")
    if package_smoke:
        sys.argv.remove("--package-smoke")
    raise SystemExit(main(smoke=smoke, package_smoke=package_smoke))
