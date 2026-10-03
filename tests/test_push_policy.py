from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "githooks" / "pre-push"


def run_hook(
    refs: str,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(HOOK)],
        cwd=cwd,
        input=refs,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_pre_push_rejects_remote_main_without_running_quality_gate() -> None:
    result = run_hook("refs/heads/docs/test 0000000 refs/heads/main 0000000\n")

    assert result.returncode != 0
    assert "direct pushes to main are forbidden" in result.stderr


def test_pre_push_allows_a_pr_branch_after_quality_gate() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        test_repo = temp_root / "repo"
        test_repo.mkdir()
        subprocess.run(
            ["git", "init", "-b", "docs/test"],
            cwd=test_repo,
            capture_output=True,
            check=True,
        )
        fake_make = temp_root / "make"
        marker = temp_root / "make-args"
        fake_make.write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$VOICEINK_TEST_MARKER"\nexit 0\n',
            encoding="utf-8",
        )
        fake_make.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{temp_dir}{os.pathsep}{environment['PATH']}"
        environment["VOICEINK_TEST_MARKER"] = str(marker)
        result = run_hook(
            "refs/heads/docs/test 0000000 refs/heads/docs/test 0000000\n",
            cwd=test_repo,
            env=environment,
        )

        assert result.returncode == 0, result.stderr
        assert marker.read_text(encoding="utf-8").strip() == "check"
