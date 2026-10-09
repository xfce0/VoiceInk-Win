"""Pytest diagnostics for identifying Windows test-suite thread leaks."""

from __future__ import annotations

from threading import enumerate


def pytest_runtest_logreport(report) -> None:
    if report.when != "teardown":
        return

    threads = ", ".join(
        f"{thread.name}[{'daemon' if thread.daemon else 'non-daemon'}]"
        for thread in enumerate()
        if thread.is_alive()
    )
    duration = report.duration
    print(
        f"[thread-diagnostics] {report.nodeid} teardown={duration:.3f}s "
        f"live_threads={len(enumerate())}: {threads}",
        flush=True,
    )
