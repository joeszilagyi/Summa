from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

from tools.common.source_adapter_handoff import validate_source_adapter_handoff_record  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = REPO_ROOT / "tools" / "validators" / "validate_source_adapter_handoff.py"
LOCAL_SOURCE_PLANNER = REPO_ROOT / "tools" / "scripts" / "plan_local_source_adapter.py"
STRUCTURED_DATA_PLANNER = REPO_ROOT / "tools" / "scripts" / "plan_structured_data_source_adapter.py"
REMOTE_URL_MANIFEST_PLANNER = REPO_ROOT / "tools" / "scripts" / "plan_remote_url_manifest_adapter.py"
LOCAL_GIT_REPO_PLANNER = REPO_ROOT / "tools" / "scripts" / "plan_local_git_repo_adapter.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "source_adapter_runtime"


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def assert_report_contract(report: dict[str, object]) -> None:
    assert report["contract_version"] == "1"
    assert report["validator"] == "source_adapter_handoff"
    assert report["status"] in {"pass", "fail"}
    assert set(report["counts"]) == {"inspected", "accepted", "rejected", "deferred"}
    assert set(report["output_artifacts"]) == {"report_json", "report_text"}
    assert report["errors"] is not None
    assert report["warnings"] == []


def init_local_git_repo_fixture(tmp_path: Path) -> tuple[Path, Path]:
    source_dir = FIXTURE_ROOT / "local_git_repo"
    scenario_dir = tmp_path / "local_git_repo"
    shutil.copytree(source_dir, scenario_dir)
    repo_dir = scenario_dir / "repo"
    subprocess.run(["git", "-C", str(repo_dir), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Fixture User"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "fixture@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "fixture commit"], check=True)
    adapter_path = scenario_dir / "source_adapter.json"
    adapter_path.write_text(
        json.dumps(
            {
                "schema_version": "source-adapter.v1",
                "adapter_id": "runtime_local_git_repo",
                "display_name": "Runtime local git repo",
                "workspace_id": "alpha_subject",
                "description": "Runtime fixture for local git planning.",
                "input_family": "local_git_repo",
                "locator": {
                    "local_path": "repo",
                    "ref": "main",
                    "include_globs": ["**/*.md", "**/*.json"],
                    "exclude_globs": ["ignored/**"],
                },
                "content_profile": {
                    "content_kinds": ["json", "markdown", "git_history"],
                    "hazard_flags": [],
                },
                "provenance": {
                    "discovery_provenance": "runtime fixture repo",
                    "acquisition_method": "local_clone",
                    "source_description": "Local git repository used for dry-run planning tests.",
                },
                "rights_and_storage": {
                    "payload_storage_policy_class": "tracked_source",
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
                        "byte_retention_status",
                        "discard_metadata",
                        "refetchability_status",
                        "transform_lineage",
                        "source_metadata",
                    ],
                    "source_specific_fields": ["git_ref", "git_commit"],
                },
                "transform_lineage": [
                    {
                        "step_id": "inspect",
                        "step_kind": "inspect_local_repo",
                        "description": "Inspect a local git checkout without mutating it.",
                        "deterministic": True,
                        "review_required": False,
                    },
                    {
                        "step_id": "handoff",
                        "step_kind": "emit_handoff",
                        "description": "Emit one source-lead handoff record for the local checkout.",
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
    return scenario_dir, adapter_path


def test_handoff_validator_accepts_current_outputs_from_all_planners(tmp_path: Path) -> None:
    local_adapter = FIXTURE_ROOT / "local_directory" / "source_adapter.json"
    local_handoff = tmp_path / "local_handoff.jsonl"
    local_proc = run_command([str(LOCAL_SOURCE_PLANNER), "--adapter", str(local_adapter), "--handoff-jsonl", str(local_handoff), "--format", "json"])
    assert local_proc.returncode == 0, local_proc.stdout + local_proc.stderr

    structured_adapter = tmp_path / "structured_adapter.json"
    structured_adapter.write_text(
        json.dumps(
            {
                "schema_version": "source-adapter.v1",
                "adapter_id": "runtime_structured_data",
                "display_name": "Runtime structured data",
                "workspace_id": "alpha_subject",
                "description": "Runtime fixture for structured-data planning.",
                "input_family": "local_file",
                "locator": {
                    "local_path": str(FIXTURE_ROOT / "structured_data" / "records.csv"),
                    "format_hint": "csv",
                },
                "content_profile": {
                    "content_kinds": ["structured_data", "json", "jsonl", "csv", "xml"],
                    "hazard_flags": ["prompt_injection_text"],
                },
                "provenance": {
                    "discovery_provenance": "runtime fixture corpus",
                    "acquisition_method": "manual_drop",
                    "source_description": "Local structured-data runtime fixtures.",
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
                        "byte_retention_status",
                        "discard_metadata",
                        "refetchability_status",
                        "transform_lineage",
                        "source_metadata",
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
                        "description": "Parse structured local source inputs without retaining raw payload.",
                        "deterministic": True,
                        "review_required": False,
                    },
                    {
                        "step_id": "handoff",
                        "step_kind": "emit_handoff",
                        "description": "Emit one source-lead handoff record per parsed structured record.",
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
    structured_handoff = tmp_path / "structured_handoff.jsonl"
    structured_proc = run_command(
        [str(STRUCTURED_DATA_PLANNER), "--adapter", str(structured_adapter), "--handoff-jsonl", str(structured_handoff), "--format", "json"]
    )
    assert structured_proc.returncode == 0, structured_proc.stdout + structured_proc.stderr

    remote_adapter = FIXTURE_ROOT / "remote_url_manifest" / "source_adapter.json"
    remote_manifest = FIXTURE_ROOT / "remote_url_manifest" / "manifest.jsonl"
    remote_handoff = tmp_path / "remote_handoff.jsonl"
    remote_proc = run_command(
        [
            str(REMOTE_URL_MANIFEST_PLANNER),
            "--adapter",
            str(remote_adapter),
            "--manifest-jsonl",
            str(remote_manifest),
            "--handoff-jsonl",
            str(remote_handoff),
            "--format",
            "json",
        ]
    )
    assert remote_proc.returncode == 0, remote_proc.stdout + remote_proc.stderr

    _, git_adapter = init_local_git_repo_fixture(tmp_path)
    git_handoff = tmp_path / "git_handoff.jsonl"
    git_proc = run_command([str(LOCAL_GIT_REPO_PLANNER), "--adapter", str(git_adapter), "--handoff-jsonl", str(git_handoff), "--format", "json"])
    assert git_proc.returncode == 0, git_proc.stdout + git_proc.stderr
    assert git(json.loads(git_proc.stdout)["resolved_repo_path"], "rev-parse", "--verify", "main^{commit}").returncode == 0

    for handoff_path, adapter_path in (
        (local_handoff, local_adapter),
        (structured_handoff, structured_adapter),
        (remote_handoff, remote_adapter),
        (git_handoff, git_adapter),
    ):
        proc = run_command(
            [
                str(VALIDATOR),
                str(handoff_path),
                "--adapter",
                str(adapter_path),
                "--report-json",
                str(tmp_path / f"{handoff_path.stem}.report.json"),
                "--report-text",
                str(tmp_path / f"{handoff_path.stem}.report.txt"),
            ]
        )
        assert proc.returncode == 0, handoff_path.name + proc.stdout + proc.stderr
        report = json.loads((tmp_path / f"{handoff_path.stem}.report.json").read_text(encoding="utf-8"))
        assert_report_contract(report)
        assert report["status"] == "pass"
        assert report["counts"]["accepted"] >= 1
        assert report["counts"]["rejected"] == 0


def test_remote_url_manifest_handoff_requires_explicit_source_identity_fields(tmp_path: Path) -> None:
    remote_adapter = json.loads((FIXTURE_ROOT / "remote_url_manifest" / "source_adapter.json").read_text(encoding="utf-8"))
    handoff_path = tmp_path / "remote_handoff.jsonl"
    proc = run_command(
        [
            str(REMOTE_URL_MANIFEST_PLANNER),
            "--adapter",
            str(FIXTURE_ROOT / "remote_url_manifest" / "source_adapter.json"),
            "--manifest-jsonl",
            str(FIXTURE_ROOT / "remote_url_manifest" / "manifest.jsonl"),
            "--handoff-jsonl",
            str(handoff_path),
            "--format",
            "json",
        ]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    handoff = [json.loads(line) for line in handoff_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    valid_record = handoff[0]
    assert validate_source_adapter_handoff_record(valid_record, remote_adapter) == []

    missing_identity = copy.deepcopy(valid_record)
    missing_identity.pop("source_identity")
    assert "source_identity is required for remote_url_manifest handoff records" in validate_source_adapter_handoff_record(
        missing_identity,
        remote_adapter,
    )

    missing_locator_field = copy.deepcopy(valid_record)
    missing_locator_field["preserved"]["original_locator"].pop("manifest_input_path")
    errors = validate_source_adapter_handoff_record(missing_locator_field, remote_adapter)
    assert "preserved.original_locator.manifest_input_path must be a non-blank string" in errors


def test_handoff_validator_rejects_incomplete_original_locator_fields(tmp_path: Path) -> None:
    def first_handoff_record(path: Path) -> dict[str, object]:
        text = path.read_text(encoding="utf-8")
        for raw_line in text.splitlines():
            if raw_line.strip():
                return json.loads(raw_line)
        raise AssertionError(f"missing handoff line in {path}")

    local_adapter = FIXTURE_ROOT / "local_directory" / "source_adapter.json"
    local_handoff = tmp_path / "local_handoff.jsonl"
    local_proc = run_command(
        [
            str(LOCAL_SOURCE_PLANNER),
            "--adapter",
            str(local_adapter),
            "--handoff-jsonl",
            str(local_handoff),
            "--format",
            "json",
        ]
    )
    assert local_proc.returncode == 0, local_proc.stdout + local_proc.stderr
    local_record = first_handoff_record(local_handoff)
    local_adapter_payload = json.loads(local_adapter.read_text(encoding="utf-8"))
    local_mutated = copy.deepcopy(local_record)
    local_mutated["preserved"]["original_locator"].pop("adapter_local_path", None)
    local_errors = validate_source_adapter_handoff_record(local_mutated, local_adapter_payload)
    assert "preserved.original_locator.adapter_local_path must be a non-blank string" in local_errors

    _, git_adapter = init_local_git_repo_fixture(tmp_path)
    git_handoff = tmp_path / "git_handoff.jsonl"
    git_proc = run_command([str(LOCAL_GIT_REPO_PLANNER), "--adapter", str(git_adapter), "--handoff-jsonl", str(git_handoff), "--format", "json"])
    assert git_proc.returncode == 0, git_proc.stdout + git_proc.stderr
    git_record = first_handoff_record(git_handoff)
    git_adapter_payload = json.loads(git_adapter.read_text(encoding="utf-8"))
    git_mutated = copy.deepcopy(git_record)
    git_mutated["preserved"]["original_locator"].pop("configured_ref", None)
    git_errors = validate_source_adapter_handoff_record(git_mutated, git_adapter_payload)
    assert "preserved.original_locator.configured_ref must be a non-blank string" in git_errors


def test_handoff_validator_rejects_missing_structured_source_specific_fields() -> None:
    adapter_payload = {
        "schema_version": "source-adapter.v1",
        "adapter_id": "runtime_structured_data",
        "workspace_id": "alpha_subject",
        "input_family": "local_directory",
        "normalized_handoff": {
            "record_family": "source_lead",
            "batch_unit": "per_record",
            "source_specific_fields": [
                "relative_path",
                "source_filename",
                "structured_format",
                "record_locator",
                "record_kind",
            ],
        },
    }

    valid_record = {
        "schema_version": "source-adapter-handoff.v1",
        "adapter_id": "runtime_structured_data",
        "workspace_id": "alpha_subject",
        "record_family": "source_lead",
        "batch_unit": "per_record",
        "adapter_path": "/tmp/source_adapter.json",
        "emitted_at": "2026-06-07T00:00:00Z",
        "sequence": 1,
        "resolved_source_path": "/tmp/records.json",
        "relative_path": "records.json",
        "preserved": {
            "original_locator": {
                "adapter_local_path": "dataset",
                "resolved_source_path": "/tmp/records.json",
                "relative_path": "records.json",
            }
        },
        "source_specific": {
            "relative_path": "records.json",
            "source_filename": "records.json",
            "structured_format": "json",
            "record_locator": "line:1",
            "record_kind": "object",
        },
    }

    missing_structured_format = copy.deepcopy(valid_record)
    missing_structured_format["source_specific"].pop("structured_format")
    errors = validate_source_adapter_handoff_record(missing_structured_format, adapter_payload)
    assert "source_specific is missing required field: structured_format" in errors

    missing_record_locator = copy.deepcopy(valid_record)
    missing_record_locator["source_specific"].pop("record_locator")
    errors = validate_source_adapter_handoff_record(missing_record_locator, adapter_payload)
    assert "source_specific is missing required field: record_locator" in errors


def test_handoff_validator_rejects_family_specific_state_mismatch(tmp_path: Path) -> None:
    adapter_path = FIXTURE_ROOT / "local_directory" / "source_adapter.json"
    handoff_path = tmp_path / "invalid_handoff.json"
    handoff_path.write_text(
        json.dumps(
            {
                "schema_version": "source-adapter-handoff.v1",
                "adapter_id": "alpha_subject_local_drop",
                "workspace_id": "alpha_subject",
                "record_family": "capture",
                "batch_unit": "per_file",
                "adapter_path": str(adapter_path),
                "emitted_at": "2026-06-02T00:00:00Z",
                "sequence": 1,
                "resolved_source_path": "/tmp/example.pdf",
                "relative_path": "example.pdf",
                "remote_state": "configured_remote",
                "network_access_attempted": False,
                "preserved": {
                    "discovery_provenance": "validator fixture",
                    "rights_posture": "private_local_only",
                    "byte_retention_status": "retained_private_only",
                    "discard_metadata": {"discard_required": False, "discard_reason": None},
                    "refetchability_status": "local_replayable",
                    "source_metadata": {"content_kinds": ["pdf"], "hazard_flags": []},
                    "transform_lineage": [],
                    "original_locator": {
                        "adapter_local_path": "corpus",
                        "resolved_source_path": "/tmp/example.pdf",
                        "relative_path": "example.pdf",
                    },
                },
                "source_specific": {"relative_path": "example.pdf", "source_filename": "example.pdf"},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    proc = run_command([str(VALIDATOR), str(handoff_path), "--adapter", str(adapter_path)])

    assert proc.returncode == 1
    assert "remote_state is not allowed for local_source handoff records" in proc.stdout


def test_handoff_validator_reports_jsonl_line_numbers_for_parse_failures(tmp_path: Path) -> None:
    adapter_path = FIXTURE_ROOT / "local_directory" / "source_adapter.json"
    handoff_path = tmp_path / "invalid.jsonl"
    handoff_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "schema_version": "source-adapter-handoff.v1",
                        "adapter_id": "alpha_subject_local_drop",
                        "workspace_id": "alpha_subject",
                        "record_family": "capture",
                        "batch_unit": "per_file",
                        "adapter_path": str(adapter_path),
                        "emitted_at": "2026-06-02T00:00:00Z",
                        "sequence": 1,
                        "resolved_source_path": "/tmp/example.pdf",
                        "relative_path": "example.pdf",
                        "preserved": {
                            "original_locator": {
                                "adapter_local_path": "corpus",
                                "resolved_source_path": "/tmp/example.pdf",
                                "relative_path": "example.pdf",
                            },
                            "discovery_provenance": "validator fixture",
                            "rights_posture": "private_local_only",
                            "byte_retention_status": "retained_private_only",
                            "discard_metadata": {"discard_required": False, "discard_reason": None},
                            "refetchability_status": "local_replayable",
                            "source_metadata": {"content_kinds": ["pdf"], "hazard_flags": []},
                            "transform_lineage": [],
                        },
                        "source_specific": {
                            "relative_path": "example.pdf",
                            "source_filename": "example.pdf",
                        },
                    },
                    sort_keys=True,
                ),
                '{"schema_version": "source-adapter-handoff.v1",',
                json.dumps(
                    {
                        "schema_version": "source-adapter-handoff.v1",
                        "adapter_id": "alpha_subject_local_drop",
                        "workspace_id": "alpha_subject",
                        "record_family": "capture",
                        "batch_unit": "per_file",
                        "adapter_path": str(adapter_path),
                        "emitted_at": "2026-06-02T00:00:00Z",
                        "sequence": 2,
                        "resolved_source_path": "/tmp/example-2.pdf",
                        "relative_path": "example-2.pdf",
                        "preserved": {
                            "original_locator": {
                                "adapter_local_path": "corpus",
                                "resolved_source_path": "/tmp/example-2.pdf",
                                "relative_path": "example-2.pdf",
                            },
                            "discovery_provenance": "validator fixture",
                            "rights_posture": "private_local_only",
                            "byte_retention_status": "retained_private_only",
                            "discard_metadata": {"discard_required": False, "discard_reason": None},
                            "refetchability_status": "local_replayable",
                            "source_metadata": {"content_kinds": ["pdf"], "hazard_flags": []},
                            "transform_lineage": [],
                        },
                        "source_specific": {
                            "relative_path": "example-2.pdf",
                            "source_filename": "example-2.pdf",
                        },
                    },
                    sort_keys=True,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    report_json = tmp_path / "report.json"

    proc = run_command(
        [
            str(VALIDATOR),
            str(handoff_path),
            "--adapter",
            str(adapter_path),
            "--report-json",
            str(report_json),
        ]
    )

    assert proc.returncode == 1
    report = json.loads(report_json.read_text(encoding="utf-8"))
    assert_report_contract(report)
    assert report["status"] == "fail"
    assert report["counts"] == {"inspected": 0, "accepted": 0, "rejected": 0, "deferred": 0}
    assert report["errors"][0]["line"] == 2
    assert "invalid JSON syntax" in report["errors"][0]["message"]
    assert "Traceback" not in proc.stdout


def test_handoff_validator_rejects_duplicate_sequences(tmp_path: Path) -> None:
    adapter_path = FIXTURE_ROOT / "local_directory" / "source_adapter.json"
    handoff_path = tmp_path / "invalid.jsonl"

    proc = run_command(
        [
            str(LOCAL_SOURCE_PLANNER),
            "--adapter",
            str(adapter_path),
            "--handoff-jsonl",
            str(handoff_path),
            "--format",
            "json",
        ]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    handoff_lines = [line for line in handoff_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    first_record = json.loads(handoff_lines[0])
    second_record = json.loads(handoff_lines[1])
    second_record["sequence"] = first_record["sequence"]
    handoff_path.write_text(
        "\n".join([json.dumps(first_record, sort_keys=True), json.dumps(second_record, sort_keys=True)])
        + "\n",
        encoding="utf-8",
    )

    report_json = tmp_path / "report.json"
    proc = run_command(
        [
            str(VALIDATOR),
            str(handoff_path),
            "--adapter",
            str(adapter_path),
            "--report-json",
            str(report_json),
        ]
    )

    assert proc.returncode == 1
    report = json.loads(report_json.read_text(encoding="utf-8"))
    messages = {error["message"] for error in report["errors"]}
    assert "handoff sequence values must be unique: 1 appears more than once" in messages


def test_handoff_validator_rejects_noncontiguous_sequences(tmp_path: Path) -> None:
    adapter_path = FIXTURE_ROOT / "local_directory" / "source_adapter.json"
    handoff_path = tmp_path / "invalid.jsonl"

    proc = run_command(
        [
            str(LOCAL_SOURCE_PLANNER),
            "--adapter",
            str(adapter_path),
            "--handoff-jsonl",
            str(handoff_path),
            "--format",
            "json",
        ]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    handoff_lines = [line for line in handoff_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    first_record = json.loads(handoff_lines[0])
    second_record = json.loads(handoff_lines[1])
    second_record["sequence"] = first_record["sequence"] + 2
    handoff_path.write_text(
        "\n".join([json.dumps(first_record, sort_keys=True), json.dumps(second_record, sort_keys=True)])
        + "\n",
        encoding="utf-8",
    )

    report_json = tmp_path / "report.json"
    proc = run_command(
        [
            str(VALIDATOR),
            str(handoff_path),
            "--adapter",
            str(adapter_path),
            "--report-json",
            str(report_json),
        ]
    )

    assert proc.returncode == 1
    report = json.loads(report_json.read_text(encoding="utf-8"))
    messages = {error["message"] for error in report["errors"]}
    assert "handoff sequence values must be contiguous starting at 1 (missing 2)" in messages


def test_handoff_validator_rejects_duplicate_sequences_in_json_array_payload(tmp_path: Path) -> None:
    adapter_path = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "source_adapter_runtime" / "local_directory" / "source_adapter.json"
    report_json = tmp_path / "report.json"
    payload = {
        "schema_version": "source-adapter-handoff.v1",
        "adapter_id": "array-adapter",
        "workspace_id": "alpha_subject",
        "record_family": "capture",
        "batch_unit": "per_file",
        "adapter_path": "/tmp/source_adapter.json",
        "emitted_at": "2026-06-02T00:00:00Z",
        "sequence": 1,
        "resolved_source_path": "/tmp/example.pdf",
        "relative_path": "example.pdf",
        "preserved": {
            "discovery_provenance": "validator fixture",
            "rights_posture": "private_local_only",
            "byte_retention_status": "retained_private_only",
            "discard_metadata": {"discard_required": False, "discard_reason": None},
            "refetchability_status": "local_replayable",
            "source_metadata": {"content_kinds": ["pdf"], "hazard_flags": []},
            "transform_lineage": [],
            "original_locator": {
                "adapter_local_path": "corpus",
                "resolved_source_path": "/tmp/example.pdf",
                "relative_path": "example.pdf",
            },
        },
        "source_specific": {"relative_path": "example.pdf", "source_filename": "example.pdf"},
    }
    duplicate_payload = [payload, {**payload, "sequence": 1, "resolved_source_path": "/tmp/example2.pdf", "relative_path": "example2.pdf", "source_specific": {"relative_path": "example2.pdf", "source_filename": "example2.pdf"}}]
    (tmp_path / "handoff.json").write_text(json.dumps(duplicate_payload), encoding="utf-8")

    proc = run_command(
        [
            str(VALIDATOR),
            str(tmp_path / "handoff.json"),
            "--adapter",
            str(adapter_path),
            "--report-json",
            str(report_json),
        ]
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = json.loads(report_json.read_text(encoding="utf-8"))
    messages = {error["message"] for error in report["errors"]}
    assert "handoff sequence values must be unique: 1 appears more than once" in messages


def test_handoff_validator_rejects_noncontiguous_sequences_in_json_array_payload(tmp_path: Path) -> None:
    adapter_path = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "source_adapter_runtime" / "local_directory" / "source_adapter.json"
    report_json = tmp_path / "report.json"
    payload = {
        "schema_version": "source-adapter-handoff.v1",
        "adapter_id": "array-adapter",
        "workspace_id": "alpha_subject",
        "record_family": "capture",
        "batch_unit": "per_file",
        "adapter_path": "/tmp/source_adapter.json",
        "emitted_at": "2026-06-02T00:00:00Z",
        "sequence": 1,
        "resolved_source_path": "/tmp/example.pdf",
        "relative_path": "example.pdf",
        "preserved": {
            "discovery_provenance": "validator fixture",
            "rights_posture": "private_local_only",
            "byte_retention_status": "retained_private_only",
            "discard_metadata": {"discard_required": False, "discard_reason": None},
            "refetchability_status": "local_replayable",
            "source_metadata": {"content_kinds": ["pdf"], "hazard_flags": []},
            "transform_lineage": [],
            "original_locator": {
                "adapter_local_path": "corpus",
                "resolved_source_path": "/tmp/example.pdf",
                "relative_path": "example.pdf",
            },
        },
        "source_specific": {"relative_path": "example.pdf", "source_filename": "example.pdf"},
    }
    noncontiguous_payload = [payload, {**payload, "sequence": 3, "resolved_source_path": "/tmp/example2.pdf", "relative_path": "example2.pdf", "source_specific": {"relative_path": "example2.pdf", "source_filename": "example2.pdf"}}]
    (tmp_path / "handoff.json").write_text(json.dumps(noncontiguous_payload), encoding="utf-8")

    proc = run_command(
        [
            str(VALIDATOR),
            str(tmp_path / "handoff.json"),
            "--adapter",
            str(adapter_path),
            "--report-json",
            str(report_json),
        ]
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = json.loads(report_json.read_text(encoding="utf-8"))
    messages = {error["message"] for error in report["errors"]}
    assert "handoff sequence values must be contiguous starting at 1 (missing 2)" in messages
