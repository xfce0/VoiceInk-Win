"""Run each pytest file in a fresh process for Windows isolation."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    test_files = sorted(Path("tests").glob("test_*.py"))
    if not test_files:
        print("No test files found", file=sys.stderr)
        return 1

    for test_file in test_files:
        print(f"=== {test_file} ===", flush=True)
        result = subprocess.run([sys.executable, "-m", "pytest", str(test_file)], check=False)
        if result.returncode not in (0, 5):
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
