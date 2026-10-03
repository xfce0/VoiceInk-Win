from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "spec" / "catalog.yaml"
ALLOWED_KINDS = {"architecture", "constitution", "feature", "rfc", "template"}
ALLOWED_STATUSES = {"active", "approved", "complete", "draft", "implemented", "proposed"}
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
    "rfc": (
        "## Status",
        "## Summary",
        "## Goals",
        "## Non-Goals",
        "## Proposed Architecture",
        "## Exit Criteria",
        "## Open Questions",
    ),
}
REQUIRED_DOCUMENTS = {
    "spec/constitution.md",
    "spec/architecture/overview.md",
    "spec/features/001-foundation.md",
    "spec/templates/feature-spec.md",
    "rfcs/foundation-runtime-spike.md",
}


def validate_catalog(catalog_path: Path, root: Path) -> list[str]:
    try:
        document = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        return [f"cannot parse specification catalog: {error}"]

    if not isinstance(document, dict):
        return ["specification catalog must contain a mapping"]
    if document.get("version") != 1:
        errors = ["specification catalog version must be 1"]
    elif document.get("source_of_truth") is not True:
        errors = ["specification catalog must declare source_of_truth: true"]
    else:
        errors = []

    entries = document.get("documents")
    if not isinstance(entries, list):
        return errors + ["specification catalog must contain a documents list"]
    if not entries:
        errors.append("specification catalog must not be empty")

    seen_paths: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"catalog entry {index} must be a mapping")
            continue

        missing = {"path", "kind", "status"} - entry.keys()
        if missing:
            errors.append(f"catalog entry {index} is missing: {', '.join(sorted(missing))}")
            continue

        relative_path = entry["path"]
        kind = entry["kind"]
        status = entry["status"]
        if not all(isinstance(value, str) for value in (relative_path, kind, status)):
            errors.append(f"catalog entry {index} path, kind, and status must be strings")
            continue

        if relative_path in seen_paths:
            errors.append(f"catalog contains duplicate path: {relative_path}")
        seen_paths.add(relative_path)
        if kind not in ALLOWED_KINDS:
            errors.append(f"catalog entry {relative_path} has invalid kind: {kind}")
        if status not in ALLOWED_STATUSES:
            errors.append(f"catalog entry {relative_path} has invalid status: {status}")

        path = (root / relative_path).resolve()
        if root.resolve() not in path.parents:
            errors.append(f"catalog entry escapes repository root: {relative_path}")
            continue
        if not path.is_file():
            errors.append(f"catalogued specification is missing: {relative_path}")
            continue

        content = path.read_text(encoding="utf-8")
        if not content.strip():
            errors.append(f"catalogued specification is empty: {relative_path}")
        for heading in REQUIRED_HEADINGS.get(kind, ()):
            if heading not in content:
                errors.append(f"{relative_path} is missing heading: {heading}")

    missing_documents = REQUIRED_DOCUMENTS - seen_paths
    for relative_path in sorted(missing_documents):
        errors.append(f"catalog is missing required document: {relative_path}")

    return errors


def main() -> int:
    errors = (
        ["missing spec/catalog.yaml"] if not CATALOG.is_file() else validate_catalog(CATALOG, ROOT)
    )

    if errors:
        for error in errors:
            print(f"spec-check: {error}")
        return 1

    print("spec-check: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
