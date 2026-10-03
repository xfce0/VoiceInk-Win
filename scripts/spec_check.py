from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "spec" / "catalog.yaml"
REQUIRED_HEADINGS = {
    "feature": (
        "## Status and Scope",
        "## User Scenarios",
        "## Functional Requirements",
        "## Non-Functional Requirements",
        "## Error and Cancellation Behavior",
        "## Acceptance Criteria",
        "## Test Plan",
        "## Open Questions and Deferred Work",
    ),
}


def catalogued_paths() -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    current_path: str | None = None
    current_kind: str | None = None

    for raw_line in CATALOG.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("- path: "):
            current_path = line.removeprefix("- path: ").strip()
        elif line.startswith("kind: ") and current_path:
            current_kind = line.removeprefix("kind: ").strip()
        elif line.startswith("status: ") and current_path and current_kind:
            entries.append((current_path, current_kind))
            current_path = None
            current_kind = None

    return entries


def main() -> int:
    errors: list[str] = []

    if not CATALOG.is_file():
        errors.append("missing spec/catalog.yaml")
    else:
        for relative_path, kind in catalogued_paths():
            path = ROOT / relative_path
            if not path.is_file():
                errors.append(f"catalogued specification is missing: {relative_path}")
                continue
            if not path.read_text(encoding="utf-8").strip():
                errors.append(f"catalogued specification is empty: {relative_path}")
            if kind == "feature":
                content = path.read_text(encoding="utf-8")
                for heading in REQUIRED_HEADINGS["feature"]:
                    if heading not in content:
                        errors.append(f"{relative_path} is missing heading: {heading}")

    if errors:
        for error in errors:
            print(f"spec-check: {error}")
        return 1

    print("spec-check: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
