from pathlib import Path

from scripts.spec_check import validate_catalog

ROOT = Path(__file__).resolve().parents[1]


def test_repository_contract_files_exist() -> None:
    required = (
        "PROJECT_CONTEXT.md",
        "README.md",
        "Makefile",
        "pyproject.toml",
        "spec/catalog.yaml",
        "spec/constitution.md",
    )

    assert all((ROOT / path).is_file() for path in required)


def test_spec_catalog_rejects_missing_required_fields(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text("version: 1\ndocuments:\n  - path: spec/example.md\n", encoding="utf-8")

    errors = validate_catalog(catalog, ROOT)

    assert any("missing: kind, status" in error for error in errors)


def test_spec_catalog_rejects_empty_documents(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text("version: 1\nsource_of_truth: true\ndocuments: []\n", encoding="utf-8")

    errors = validate_catalog(catalog, ROOT)

    assert any("must not be empty" in error for error in errors)
    assert any("missing required document" in error for error in errors)


def test_spec_catalog_rejects_duplicate_and_invalid_entries(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """version: 1
documents:
  - path: README.md
    kind: unknown
    status: broken
  - path: README.md
    kind: template
    status: active
""",
        encoding="utf-8",
    )

    errors = validate_catalog(catalog, ROOT)

    assert any("invalid kind" in error for error in errors)
    assert any("invalid status" in error for error in errors)
    assert any("duplicate path" in error for error in errors)
