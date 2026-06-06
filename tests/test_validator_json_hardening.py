from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"


FILE_BASED_VALIDATORS = [
    "validate_candidate_feedback_plan.py",
    "validate_canonical_graph_model_outline.py",
    "validate_correction_ledger.py",
    "validate_crown_jewel_backup_manifest.py",
    "validate_crown_jewel_store_policy.py",
    "validate_evidence_locator.py",
    "validate_field_review_state.py",
    "validate_gather_candidate_batch.py",
    "validate_jsonl.py",
    "validate_knowledge_tree_build_manifest.py",
    "validate_knowledge_tree_export.py",
    "validate_llm_prompt_fixture.py",
    "validate_local_search_projection.py",
    "validate_migration_ledger.py",
    "validate_public_knowledge_tree_presentation.py",
    "validate_public_safekeeping_manifest.py",
    "validate_source_adapter.py",
    "validate_source_adapter_handoff.py",
    "validate_source_locus_jsonl.py",
    "validate_static_knowledge_tree_output.py",
    "validate_static_page_family_query_contract.py",
    "validate_subject_manifest.py",
    "validate_topic_workspace_registry.py",
]


def run_validator(script_name: str, target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATORS_DIR / script_name), str(target)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )


def target_path_for(script_name: str, tmp_path: Path) -> Path:
    if script_name == "validate_source_locus_jsonl.py":
        return tmp_path / "fixture_source_loci.jsonl"
    return tmp_path / "duplicate-keys.json"


@pytest.mark.parametrize("script_name", FILE_BASED_VALIDATORS)
def test_validator_clis_reject_duplicate_json_keys(script_name: str, tmp_path: Path) -> None:
    target = target_path_for(script_name, tmp_path)
    target.write_text(
        "{\"schema_version\": \"fixture.v1\", \"duplicate\": 1, \"duplicate\": 2}\n",
        encoding="utf-8",
    )

    proc = run_validator(script_name, target)

    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "duplicate JSON object key" in combined
