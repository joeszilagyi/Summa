from __future__ import annotations

import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
REPO_PATH_RULES = REPO_ROOT / "tools" / "pipeline_registry" / "contracts" / "repo_path_rules.jsonl"
OPERATOR_SMOKE = REPO_ROOT / "tools" / "scripts" / "operator_path_smoke.py"


def load_pyproject() -> dict[str, object]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_bug_fixer_root_surface_is_removed() -> None:
    assert not (REPO_ROOT / "Bug_Fixer.sh").exists()
    assert not (REPO_ROOT / "docs" / "tools" / "bug_fixer.md").exists()


def test_bug_fixer_is_not_exposed_as_supported_tooling() -> None:
    pyproject = load_pyproject()
    scripts = pyproject["project"].get("scripts", {})
    assert "bug-fixer" not in scripts
    assert "summa-bug-fixer" not in scripts
    assert all("bug_fixer" not in str(target) for target in scripts.values())

    operator_smoke = OPERATOR_SMOKE.read_text(encoding="utf-8")
    assert "Bug_Fixer.sh" not in operator_smoke
    assert "bug fixer" not in operator_smoke.lower()

    repo_path_rules = REPO_PATH_RULES.read_text(encoding="utf-8")
    assert "rule.root_bug_fixer" not in repo_path_rules
    assert "Bug_Fixer.sh" not in repo_path_rules


def test_supported_docs_do_not_reference_bug_fixer() -> None:
    doc_roots = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CONTRIBUTING.md",
        *sorted((REPO_ROOT / "docs").rglob("*.md")),
    ]
    for path in doc_roots:
        body = path.read_text(encoding="utf-8")
        assert "Bug_Fixer.sh" not in body, path
        assert "BUG-FIXER" not in body, path
        assert "bug_fixer" not in body, path
