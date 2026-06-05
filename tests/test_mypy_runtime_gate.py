from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
HYGIENE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "repo-hygiene.yml"

RUNTIME_SPINE_MYPY_FILES = {
    "tools/common/search_leak_policy.py",
    "tools/common/standards_profiles.py",
    "tools/scripts/apply_review_decision.py",
    "tools/scripts/build_candidate_feedback_plan.py",
    "tools/scripts/build_release_readiness_bundle.py",
    "tools/scripts/evaluate_network_safety_gate.py",
    "tools/scripts/execute_source_adapter.py",
    "tools/scripts/export_standards_profile.py",
    "tools/scripts/ingest_execution_artifacts.py",
    "tools/scripts/ingest_gather_candidate_batch.py",
    "tools/scripts/operator_path_smoke.py",
    "tools/scripts/replay_canonical_write_spool.py",
    "tools/scripts/run_scheduled_topic_cycles.py",
    "tools/scripts/run_topic_cycle.py",
    "tools/scripts/run_topic_gather.py",
    "tools/scripts/select_scheduled_workspaces.py",
    "tools/source_db_tools/canonical_ingest.py",
    "tools/source_db_tools/canonical_reconciliation.py",
    "tools/source_db_tools/canonical_store.py",
    "tools/source_db_tools/canonical_write_spool.py",
    "tools/source_db_tools/init_canonical_store.py",
    "tools/source_db_tools/review_decision_apply.py",
    "tools/source_db_tools/source_query_plan.py",
}


def load_pyproject() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def workflow_static_targets() -> set[str]:
    workflow = HYGIENE_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"PYTHON_STATIC_TARGETS:\s*>-\n(?P<body>(?:\s{8}\S[^\n]*\n)+)",
        workflow,
    )
    assert match is not None
    return {line.strip() for line in match.group("body").splitlines() if line.strip()}


def test_runtime_spine_files_are_in_mypy_gate() -> None:
    pyproject = load_pyproject()
    mypy_files = set(pyproject["tool"]["mypy"]["files"])

    for path in RUNTIME_SPINE_MYPY_FILES:
        assert (REPO_ROOT / path).is_file(), path
    assert mypy_files >= RUNTIME_SPINE_MYPY_FILES


def test_ci_static_targets_cover_runtime_spine() -> None:
    targets = workflow_static_targets()

    assert targets >= RUNTIME_SPINE_MYPY_FILES
    assert "python -m mypy" in HYGIENE_WORKFLOW.read_text(encoding="utf-8")


def test_mypy_runtime_gate_keeps_strictness_and_no_broad_ignores() -> None:
    pyproject = load_pyproject()
    mypy_config = pyproject["tool"]["mypy"]

    assert mypy_config["check_untyped_defs"] is True
    assert mypy_config["warn_unused_ignores"] is True
    assert mypy_config["warn_redundant_casts"] is True
    assert mypy_config["no_implicit_optional"] is True
    assert mypy_config.get("ignore_errors") is not True

    for key, value in mypy_config.items():
        if key.startswith("mypy-") and isinstance(value, dict):
            assert value.get("ignore_errors") is not True, key
