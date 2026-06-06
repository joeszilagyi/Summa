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


def test_repo_hygiene_workflow_measures_the_full_tools_tree() -> None:
    body = WORKFLOW.read_text(encoding="utf-8")

    assert "--cov=tools" in body
    assert "--cov=tools/validators" not in body
    assert "--cov=tools/common" not in body


def test_coverage_threshold_is_no_longer_placeholder_low() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "fail_under = 70" in pyproject
    assert "fail_under = 60" not in pyproject


def test_static_hygiene_targets_include_runtime_spine() -> None:
    body = WORKFLOW.read_text(encoding="utf-8")
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    for expected in (
        "tools/collateral/pdf_extract.py",
        "tools/common/runtime_ledger.py",
        "tools/common/workspace_lock.py",
        "tools/scripts/local_doctor.py",
        "tools/source_db_tools/sqlite_safety.py",
    ):
        assert expected in body
        assert expected in pyproject


def test_validators_job_name_matches_broader_pytest_smoke() -> None:
    body = WORKFLOW.read_text(encoding="utf-8")

    assert "name: Pytest Suite Smoke" in body
    assert "name: Validator Smoke" not in body
