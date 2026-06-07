from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import importlib.util


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "validators" / "source_adapter"
VALIDATOR = REPO_ROOT / "tools" / "validators" / "validate_source_adapter.py"
SOURCE_INTAKE_VIEW = REPO_ROOT / "tools" / "scripts" / "build_source_intake_status_view.py"
SOURCE_ADAPTER_SCHEMA = REPO_ROOT / "config" / "source_adapter.schema.json"

source_intake_spec = importlib.util.spec_from_file_location("source_intake_status_view_for_tests", SOURCE_INTAKE_VIEW)
assert source_intake_spec is not None
source_intake_status_view = importlib.util.module_from_spec(source_intake_spec)
assert source_intake_spec.loader is not None
source_intake_spec.loader.exec_module(source_intake_status_view)

sys.path.insert(0, str(REPO_ROOT))
from tools.common.source_adapter_contract import (  # noqa: E402
    INPUT_FAMILIES,
    INPUT_FAMILY_ALLOWED_LOCATOR_KEYS,
    INPUT_FAMILY_LOCATOR_KEYS,
)

EXIT_PASS = 0
EXIT_VALIDATION_FAILED = 1


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_fixture(tmp_path: Path, scenario: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    source_dir = FIXTURE_ROOT / scenario
    scenario_dir = tmp_path / scenario
    shutil.copytree(source_dir, scenario_dir)
    actual_dir = scenario_dir / "actual"
    actual_dir.mkdir()

    target = Path("inputs/source_adapter.json")
    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            str(target),
            "--scenario",
            scenario,
            "--target-id",
            str(target),
            "--report-json",
            "actual/report.json",
            "--report-text",
            "actual/report.txt",
        ],
        cwd=scenario_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    return proc, scenario_dir


def assert_matches_golden(scenario_dir: Path) -> None:
    expected_dir = scenario_dir / "expected"
    actual_dir = scenario_dir / "actual"

    expected_json_path = expected_dir / "report.json"
    expected_text_path = expected_dir / "report.txt"
    assert expected_json_path.is_file(), f"missing golden report.json for {scenario_dir.name}"
    assert expected_text_path.is_file(), f"missing golden report.txt for {scenario_dir.name}"

    expected_json = json.loads(expected_json_path.read_text(encoding="utf-8"))
    actual_json = json.loads((actual_dir / "report.json").read_text(encoding="utf-8"))
    assert actual_json == expected_json

    expected_text = expected_text_path.read_text(encoding="utf-8")
    actual_text = (actual_dir / "report.txt").read_text(encoding="utf-8")
    assert actual_text == expected_text


def schema_locator_contracts() -> dict[str, dict[str, object]]:
    schema = json.loads(SOURCE_ADAPTER_SCHEMA.read_text(encoding="utf-8"))
    defs = schema["$defs"]
    families = {}
    for family in INPUT_FAMILIES:
        ref_name = f"{family}_locator"
        locator_def = defs[ref_name]
        families[family] = {
            "required": set(locator_def.get("required", [])),
            "allowed": set(locator_def.get("properties", {}).keys()),
        }
    return families


def test_source_adapter_validator_fixtures_match_golden(tmp_path: Path) -> None:
    for source_dir in sorted(path for path in FIXTURE_ROOT.iterdir() if path.is_dir()):
        input_path = source_dir / "inputs" / "source_adapter.json"
        input_hash_before = sha256(input_path)

        proc, scenario_dir = run_fixture(tmp_path, source_dir.name)
        input_hash_after = sha256(scenario_dir / "inputs" / "source_adapter.json")

        expected_report = json.loads((source_dir / "expected" / "report.json").read_text(encoding="utf-8"))
        expected_status = expected_report["status"]
        expected_exit = EXIT_PASS if expected_status == "pass" else EXIT_VALIDATION_FAILED

        assert proc.returncode == expected_exit, source_dir.name + proc.stdout + proc.stderr
        assert_matches_golden(scenario_dir)
        assert proc.stdout == (scenario_dir / "actual" / "report.txt").read_text(encoding="utf-8")
    assert input_hash_after == input_hash_before


def test_source_adapter_validator_rejects_unknown_nested_fields(tmp_path: Path) -> None:
    source_adapter_payload = json.loads(
        (FIXTURE_ROOT / "valid_local_directory" / "inputs" / "source_adapter.json").read_text(encoding="utf-8")
    )
    source_adapter_payload["content_profile"]["unexpected_profile_key"] = "unexpected"
    source_adapter_payload["normalized_handoff"]["unexpected_handoff_field"] = "unexpected"

    target_path = tmp_path / "source_adapter.json"
    target_path.write_text(json.dumps(source_adapter_payload), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            str(target_path),
            "--report-json",
            "actual/report.json",
            "--report-text",
            "actual/report.txt",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = json.loads((tmp_path / "actual" / "report.json").read_text(encoding="utf-8"))
    messages = {error["message"] for error in report["errors"]}
    assert "unexpected content_profile field: unexpected_profile_key" in messages
    assert "unexpected normalized_handoff field: unexpected_handoff_field" in messages


def test_source_adapter_validator_rejects_unknown_top_level_nested_fields_with_codes(tmp_path: Path) -> None:
    source_adapter_payload = json.loads(
        (FIXTURE_ROOT / "valid_local_directory" / "inputs" / "source_adapter.json").read_text(encoding="utf-8")
    )
    source_adapter_payload["content_profile"]["unexpected_profile_key"] = "unexpected"
    source_adapter_payload["normalized_handoff"]["unexpected_handoff_field"] = "unexpected"

    target_path = tmp_path / "source_adapter.json"
    target_path.write_text(json.dumps(source_adapter_payload), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            str(target_path),
            "--report-json",
            "actual/report.json",
            "--report-text",
            "actual/report.txt",
        ],
        cwd=tmp_path,
        text=True,
        check=False,
    )

    assert proc.returncode != 0, proc.stdout + proc.stderr
    report = json.loads((tmp_path / "actual" / "report.json").read_text(encoding="utf-8"))
    codes = {error["code"] for error in report["errors"]}
    assert "UNKNOWN_CONTENT_PROFILE_FIELD" in codes
    assert "UNKNOWN_HANDOFF_FIELD" in codes


def test_source_adapter_validator_does_not_recreate_missing_expected_reports(tmp_path: Path) -> None:
    expected_report = json.loads(
        (FIXTURE_ROOT / "valid_local_directory" / "expected" / "report.json").read_text(encoding="utf-8")
    )
    expected_status = EXIT_PASS if expected_report["status"] == "pass" else EXIT_VALIDATION_FAILED

    scenario_dir = tmp_path / "valid_local_directory"
    shutil.copytree(FIXTURE_ROOT / "valid_local_directory", scenario_dir)
    expected_json = scenario_dir / "expected" / "report.json"
    expected_text = scenario_dir / "expected" / "report.txt"
    expected_json.unlink()
    expected_text.unlink()

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "inputs/source_adapter.json",
            "--scenario",
            "valid_local_directory",
            "--target-id",
            "inputs/source_adapter.json",
            "--report-json",
            "actual/report.json",
            "--report-text",
            "actual/report.txt",
        ],
        cwd=scenario_dir,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == expected_status, proc.stdout + proc.stderr
    assert not expected_json.exists()
    assert not expected_text.exists()


def test_source_intake_status_view_round_trips_valid_and_invalid_adapters(tmp_path: Path) -> None:
    adapters = [
        FIXTURE_ROOT / "valid_local_directory" / "inputs" / "source_adapter.json",
        FIXTURE_ROOT / "valid_remote_archive" / "inputs" / "source_adapter.json",
        FIXTURE_ROOT / "invalid_missing_required_key" / "inputs" / "source_adapter.json",
    ]
    proc = subprocess.run(
        [
            sys.executable,
            str(SOURCE_INTAKE_VIEW),
            "--adapter",
            str(adapters[0]),
            "--adapter",
            str(adapters[1]),
            "--adapter",
            str(adapters[2]),
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)

    assert payload["schema_version"] == "source-intake-status.v1"
    assert payload["counts"]["total_adapters"] == 3
    assert payload["counts"]["contract_pass"] == 2
    assert payload["counts"]["contract_fail"] == 1
    assert payload["counts"]["needs_review"] == 2
    assert payload["counts"]["failed"] == 1

    entries = {entry["adapter_id"] or entry["adapter_path"]: entry for entry in payload["adapters"]}
    assert entries["alpha_subject_local_drop"]["contract_status"] == "pass"
    assert entries["alpha_subject_local_drop"]["intake_state"] == "needs_review"
    assert entries["alpha_subject_local_drop"]["public_export_eligibility"] == "blocked"
    assert entries["alpha_subject_local_drop"]["quote_eligibility"] == "review_required"
    assert entries["alpha_subject_archive_urls"]["contract_status"] == "pass"
    assert entries["alpha_subject_archive_urls"]["intake_state"] == "needs_review"
    assert entries["alpha_subject_archive_urls"]["public_export_eligibility"] == "metadata_only"
    assert entries["alpha_subject_archive_urls"]["quote_eligibility"] == "limited_excerpt"
    invalid_entry = entries[next(key for key in entries if key != "alpha_subject_local_drop" and key != "alpha_subject_archive_urls")]
    assert invalid_entry["contract_status"] == "fail"
    assert invalid_entry["intake_state"] == "failed"
    assert invalid_entry["validation"]["error_count"] == 1


def test_source_intake_status_view_filters_workspace_ids_before_validation(tmp_path: Path, monkeypatch) -> None:
    adapter_paths = []
    for workspace_id in ("alpha", "beta", "gamma"):
        path = tmp_path / f"{workspace_id}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": "source-adapter.v1",
                    "adapter_id": f"{workspace_id}_adapter",
                    "display_name": workspace_id.title(),
                    "workspace_id": workspace_id,
                    "input_family": "remote_url_manifest",
                    "locator": {"manifest_url": f"https://example.invalid/{workspace_id}.jsonl"},
                    "content_profile": {"content_kinds": ["url_observation"], "hazard_flags": []},
                    "provenance": {
                        "discovery_provenance": "test",
                        "acquisition_method": "manual_list",
                        "source_description": "fixture",
                    },
                    "rights_and_storage": {
                        "payload_storage_policy_class": "external_later",
                        "metadata_storage_policy_class": "tracked_derived",
                        "rights_posture": "quote_limited",
                    },
                    "automation_posture": "operator_review_required",
                    "normalized_handoff": {
                        "record_family": "url_observation",
                        "batch_unit": "per_reference",
                        "preserve_fields": ["original_locator"],
                        "source_specific_fields": ["manifest_url"],
                    },
                    "transform_lineage": [
                        {
                            "step_id": "handoff",
                            "step_kind": "emit_handoff",
                            "description": "Emit handoff.",
                            "deterministic": True,
                            "review_required": True,
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        adapter_paths.append(path)

    calls: list[Path] = []

    def fake_adapter_entry(path: Path) -> dict[str, object]:
        calls.append(path)
        return {
            "adapter_path": str(path),
            "adapter_status": "ok",
            "contract_status": "pass",
            "intake_state": "configured",
            "status_detail": None,
            "adapter_id": f"{path.stem}_adapter",
            "display_name": path.stem.title(),
            "workspace_id": "beta",
            "input_family": "remote_url_manifest",
            "locator": {"locator_status": "configured_remote"},
            "content_profile": {"content_kinds": [], "hazard_flags": []},
            "rights_and_storage": {},
            "automation_posture": None,
            "normalized_handoff": {"record_family": None, "batch_unit": None, "preserve_fields": [], "source_specific_fields": []},
            "transform_lineage": {"step_count": 0, "review_required_step_count": 0, "final_step_kind": None},
            "review_required_reasons": [],
            "public_use_blockers": [],
            "public_export_eligibility": None,
            "quote_eligibility": None,
            "validation": {"counts": {}, "error_count": 0, "warning_count": 0, "errors": [], "warnings": []},
        }

    monkeypatch.setattr(source_intake_status_view, "adapter_entry", fake_adapter_entry)

    payload = source_intake_status_view.build_source_intake_status_payload(
        argparse.Namespace(
            adapter=[str(adapter_paths[0]), str(adapter_paths[1]), str(adapter_paths[2])],
            root=[],
            workspace_ids=["beta"],
            limit=1,
            format="json",
        )
    )

    assert calls == [adapter_paths[1]]
    assert payload["counts"]["total_adapters"] == 1
    assert payload["adapters"][0]["workspace_id"] == "beta"


def test_source_adapter_schema_locator_families_match_validator_contract() -> None:
    families = schema_locator_contracts()

    for family in sorted(INPUT_FAMILIES):
        assert families[family]["required"] == {INPUT_FAMILY_LOCATOR_KEYS[family]}
        assert families[family]["allowed"] == INPUT_FAMILY_ALLOWED_LOCATOR_KEYS[family]


def test_source_adapter_validator_rejects_remote_manifest_globs(tmp_path: Path) -> None:
    target = tmp_path / "source_adapter.json"
    target.write_text(
        json.dumps(
            {
                "schema_version": "source-adapter.v1",
                "adapter_id": "remote_manifest_glob_fixture",
                "display_name": "Remote manifest glob fixture",
                "workspace_id": "alpha_subject",
                "description": "Remote URL manifest should reject local glob fields.",
                "input_family": "remote_url_manifest",
                "locator": {
                    "manifest_url": "https://archives.example.gov/subject/alpha/manifest.jsonl",
                    "include_globs": ["**/*.pdf"],
                },
                "content_profile": {
                    "content_kinds": ["pdf"],
                    "hazard_flags": [],
                },
                "provenance": {
                    "discovery_provenance": "validator test",
                    "acquisition_method": "manual_list",
                    "source_description": "Remote manifest fixture for locator-family validation.",
                },
                "rights_and_storage": {
                    "payload_storage_policy_class": "external_later",
                    "metadata_storage_policy_class": "tracked_derived",
                    "rights_posture": "quote_limited",
                },
                "automation_posture": "operator_review_required",
                "normalized_handoff": {
                    "record_family": "url_observation",
                    "batch_unit": "per_reference",
                    "preserve_fields": [
                        "original_locator",
                        "discovery_provenance",
                        "rights_posture",
                    ],
                    "source_specific_fields": [
                        "manifest_url",
                    ],
                },
                "transform_lineage": [
                    {
                        "step_id": "manifest",
                        "step_kind": "read_manifest_snapshot",
                        "description": "Read the remote manifest snapshot.",
                        "deterministic": True,
                        "review_required": False,
                    },
                    {
                        "step_id": "handoff",
                        "step_kind": "emit_handoff",
                        "description": "Emit URL observations.",
                        "deterministic": True,
                        "review_required": True,
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(target)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == EXIT_VALIDATION_FAILED, proc.stdout + proc.stderr
    assert "LOCATOR_FIELD_NOT_ALLOWED" in proc.stdout


def test_source_adapter_validator_rejects_invalid_remote_manifest_url_hostnames(tmp_path: Path) -> None:
    target = tmp_path / "source_adapter.json"
    target.write_text(
        json.dumps(
            {
                "schema_version": "source-adapter.v1",
                "adapter_id": "invalid_manifest_url_hostnames",
                "display_name": "Invalid manifest URL fixture",
                "workspace_id": "alpha_subject",
                "description": "Validate hostname and URL strictness for remote URL manifest.",
                "input_family": "remote_url_manifest",
                "locator": {
                    "manifest_url": "https://exa mple.com/manifest.jsonl",
                },
                "content_profile": {
                    "content_kinds": ["url_observation"],
                    "hazard_flags": [],
                },
                "provenance": {
                    "discovery_provenance": "validator test",
                    "acquisition_method": "manual_list",
                    "source_description": "Invalid hostname in manifest URL.",
                },
                "rights_and_storage": {
                    "payload_storage_policy_class": "external_later",
                    "metadata_storage_policy_class": "tracked_derived",
                    "rights_posture": "quote_limited",
                },
                "automation_posture": "operator_review_required",
                "normalized_handoff": {
                    "record_family": "url_observation",
                    "batch_unit": "per_reference",
                    "preserve_fields": [
                        "original_locator",
                        "discovery_provenance",
                        "rights_posture",
                    ],
                    "source_specific_fields": [
                        "manifest_url",
                    ],
                },
                "transform_lineage": [
                    {
                        "step_id": "manifest",
                        "step_kind": "read_manifest_snapshot",
                        "description": "Read remote manifest.",
                        "deterministic": True,
                        "review_required": False,
                    },
                    {
                        "step_id": "handoff",
                        "step_kind": "emit_handoff",
                        "description": "Emit URL observations.",
                        "deterministic": True,
                        "review_required": True,
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(target)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        check=False,
    )

    assert proc.returncode == EXIT_VALIDATION_FAILED, proc.stdout + proc.stderr
    assert "INVALID_REMOTE_URL" in proc.stdout


def test_source_adapter_validator_accepts_structured_locator_hints(tmp_path: Path) -> None:
    target = tmp_path / "source_adapter.json"
    target.write_text(
        json.dumps(
            {
                "schema_version": "source-adapter.v1",
                "adapter_id": "structured_hint_fixture",
                "display_name": "Structured hint fixture",
                "workspace_id": "alpha_subject",
                "description": "Structured-data locator hint validation fixture.",
                "input_family": "local_file",
                "locator": {
                    "local_path": "records.json",
                    "format_hint": "json",
                    "record_path": "records",
                },
                "content_profile": {
                    "content_kinds": ["structured_data", "json"],
                    "hazard_flags": [],
                },
                "provenance": {
                    "discovery_provenance": "validator test",
                    "acquisition_method": "manual_drop",
                    "source_description": "Structured-data locator hint fixture.",
                },
                "rights_and_storage": {
                    "payload_storage_policy_class": "private_only",
                    "metadata_storage_policy_class": "tracked_derived",
                    "rights_posture": "private_local_only",
                },
                "automation_posture": "operator_review_required",
                "normalized_handoff": {
                    "record_family": "source_lead",
                    "batch_unit": "per_record",
                    "preserve_fields": [
                        "original_locator",
                        "discovery_provenance",
                        "rights_posture",
                    ],
                    "source_specific_fields": [
                        "relative_path",
                        "source_filename",
                        "structured_format",
                        "record_locator",
                        "record_kind",
                    ],
                },
                "transform_lineage": [
                    {
                        "step_id": "parse",
                        "step_kind": "parse_structured_data",
                        "description": "Parse structured source records.",
                        "deterministic": True,
                        "review_required": False,
                    },
                    {
                        "step_id": "handoff",
                        "step_kind": "emit_handoff",
                        "description": "Emit structured-data handoff records.",
                        "deterministic": True,
                        "review_required": True,
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(target)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == EXIT_PASS, proc.stdout + proc.stderr


def test_source_adapter_validator_rejects_invalid_rights_retention_combination(tmp_path: Path) -> None:
    target = tmp_path / "source_adapter.json"
    target.write_text(
        json.dumps(
            {
                "schema_version": "source-adapter.v1",
                "adapter_id": "invalid_policy_fixture",
                "display_name": "Invalid policy fixture",
                "workspace_id": "alpha_subject",
                "description": "Policy-combination validation fixture.",
                "input_family": "local_git_repo",
                "locator": {
                    "local_path": "repo",
                    "ref": "main",
                },
                "content_profile": {
                    "content_kinds": ["git_history"],
                    "hazard_flags": [],
                },
                "provenance": {
                    "discovery_provenance": "validator test",
                    "acquisition_method": "manual_checkout",
                    "source_description": "Local git checkout for policy validation.",
                },
                "rights_and_storage": {
                    "payload_storage_policy_class": "private_only",
                    "metadata_storage_policy_class": "tracked_source",
                    "rights_posture": "redistributable",
                },
                "automation_posture": "operator_review_required",
                "normalized_handoff": {
                    "record_family": "source_lead",
                    "batch_unit": "per_snapshot",
                    "preserve_fields": [
                        "original_locator",
                        "discovery_provenance",
                        "rights_posture",
                    ],
                    "source_specific_fields": [
                        "git_ref",
                        "git_commit",
                    ],
                },
                "transform_lineage": [
                    {
                        "step_id": "inspect",
                        "step_kind": "inspect_local_repo",
                        "description": "Inspect local repository metadata.",
                        "deterministic": True,
                        "review_required": False,
                    },
                    {
                        "step_id": "handoff",
                        "step_kind": "emit_handoff",
                        "description": "Emit source-lead handoff records.",
                        "deterministic": True,
                        "review_required": True,
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(target)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == EXIT_VALIDATION_FAILED, proc.stdout + proc.stderr
    assert "INVALID_RIGHTS_RETENTION_COMBINATION" in proc.stdout
