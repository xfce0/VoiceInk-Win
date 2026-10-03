from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_repository_contract_files_exist() -> None:
    required = (
        "AGENTS.md",
        "README.md",
        "Makefile",
        "pyproject.toml",
        "spec/catalog.yaml",
        "spec/constitution.md",
    )

    assert all((ROOT / path).is_file() for path in required)
