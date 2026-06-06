from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "repo-hygiene.yml"


def test_repo_hygiene_workflow_tests_all_declared_python_versions() -> None:
    body = WORKFLOW.read_text(encoding="utf-8")

    assert "matrix:" in body
    assert "python-version:" in body
    assert '- "3.11"' in body
    assert '- "3.12"' in body
    assert '- "3.13"' in body
    assert 'python-version: ${{ matrix.python-version }}' in body
