from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.scripts import select_scheduled_workspaces as selector

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "select_scheduled_workspaces.py"


def run_selector(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def write_manifest(workspace_root: Path, *, subject_id: str) -> Path:
    manifest_path = workspace_root / ".indexer" / "subject_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "subject-manifest.v1",
                "subject_id": subject_id,
                "display_name": subject_id.replace(".", " ").title(),
                "domain_pack": "general.v1",
                "scope_statement": "Synthetic selector fixture.",
                "languages": ["en"],
                "aliases": ["Synthetic fixture"],
                "disambiguation_terms": ["selector"],
                "excluded_senses": ["non-fixture"],
                "enabled_facets": ["sources"],
                "query_families": ["web_search"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def workspace_record(
    *,
    workspace_id: str,
    workspace_root: Path,
    schedule_posture: str = "scheduled",
    lifecycle_state: str = "active",
    manifest_path: Path | None,
    scheduler_policy: dict[str, object] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "workspace_id": workspace_id,
        "topic_label": workspace_id.replace("_", " ").title(),
        "workspace_root": str(workspace_root),
        "domain_pack": "general.v1",
        "lifecycle_state": lifecycle_state,
        "schedule_posture": schedule_posture,
        "workspace_policy_class": "private_local",
    }
    if manifest_path is not None:
        record["default_subject_manifest"] = str(manifest_path)
    if scheduler_policy is not None:
        record["scheduler_policy"] = scheduler_policy
    return record


def write_registry(tmp_path: Path, workspaces: list[dict[str, object]]) -> Path:
    registry_path = tmp_path / "topic_workspaces.local.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "topic-workspace-registry.v1",
                "workspaces": workspaces,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return registry_path


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_selector_emits_and_persists_planned_run_records(tmp_path: Path) -> None:
    selected_root = tmp_path / "workspaces" / "selected"
    manual_root = tmp_path / "workspaces" / "manual"
    missing_manifest_root = tmp_path / "workspaces" / "missing_manifest"
    inactive_root = tmp_path / "workspaces" / "inactive"
    for root in [selected_root, manual_root, missing_manifest_root, inactive_root]:
        root.mkdir(parents=True)

    selected_manifest = write_manifest(selected_root, subject_id="subject.selected")
    manual_manifest = write_manifest(manual_root, subject_id="subject.manual")
    inactive_manifest = write_manifest(inactive_root, subject_id="subject.inactive")
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="selected_workspace",
                workspace_root=selected_root,
                manifest_path=selected_manifest,
            ),
            workspace_record(
                workspace_id="manual_workspace",
                workspace_root=manual_root,
                schedule_posture="manual",
                manifest_path=manual_manifest,
            ),
            workspace_record(
                workspace_id="missing_manifest_workspace",
                workspace_root=missing_manifest_root,
                manifest_path=None,
            ),
            workspace_record(
                workspace_id="inactive_workspace",
                workspace_root=inactive_root,
                lifecycle_state="paused",
                manifest_path=inactive_manifest,
            ),
        ],
    )
    planned_runs = tmp_path / "planned-runs.jsonl"
    input_paths = [registry_path, selected_manifest, manual_manifest, inactive_manifest]
    mtimes_before = {path: path.stat().st_mtime_ns for path in input_paths}

    proc = run_selector(
        [
            "--registry",
            str(registry_path),
            "--planner-run-id",
            "planner-fixture",
            "--planned-at",
            "2026-01-01T00:00:00Z",
            "--run-budget-max-attempts",
            "2",
            "--run-budget-max-runtime-seconds",
            "900",
            "--planned-runs-jsonl",
            str(planned_runs),
            "--format",
            "json",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert {path: path.stat().st_mtime_ns for path in input_paths} == mtimes_before
    payload = json.loads(proc.stdout)
    records = payload["planned_run_records"]
    records_by_workspace = {record["workspace_id"]: record for record in records}

    assert payload["selected_count"] == 1
    assert payload["skipped_count"] == 3
    assert payload["planned_run_record_count"] == 4
    explanation = payload["selection_explanation"]
    assert explanation["schema_version"] == "selection-explanation.v1"
    assert explanation["selection_kind"] == "scheduled_workspace"
    assert explanation["selected_candidate"]["candidate_id"] == "selected_workspace"
    assert {
        candidate["candidate_id"] for candidate in explanation["considered_candidates"]
    } == {
        "selected_workspace",
        "manual_workspace",
        "missing_manifest_workspace",
        "inactive_workspace",
    }
    assert all(candidate["reason"] for candidate in explanation["excluded_candidates"])
    assert set(records_by_workspace) == {
        "selected_workspace",
        "manual_workspace",
        "missing_manifest_workspace",
        "inactive_workspace",
    }

    selected = records_by_workspace["selected_workspace"]
    assert selected == {
        **selected,
        "schema_version": "planned-run.v1",
        "planner_run_id": "planner-fixture",
        "planned_run_id": "planner-fixture:selected_workspace",
        "planned_at": "2026-01-01T00:00:00Z",
        "workspace_id": "selected_workspace",
        "selection_explanation_id": explanation["explanation_id"],
        "decision": "selected",
        "cadence_reason": "schedule_posture:scheduled",
        "skipped_reason": None,
        "skipped_reasons": [],
        "run_budget": {"max_attempts": 2, "max_runtime_seconds": 900},
    }

    manual = records_by_workspace["manual_workspace"]
    assert manual["decision"] == "skipped"
    assert manual["selection_explanation_id"] == explanation["explanation_id"]
    assert manual["skipped_reason"] == "schedule_posture is manual; pass --include-manual to include it"
    assert manual["skipped_reasons"] == [manual["skipped_reason"]]
    assert manual["cadence_reason"] == "schedule_posture:manual"

    missing_manifest = records_by_workspace["missing_manifest_workspace"]
    assert missing_manifest["decision"] == "skipped"
    assert missing_manifest["skipped_reason"] == (
        "default_subject_manifest is missing or unresolved; scheduled runs need an explicit manifest"
    )

    inactive = records_by_workspace["inactive_workspace"]
    assert inactive["decision"] == "skipped"
    assert inactive["skipped_reason"] == "lifecycle_state is 'paused'; scheduler only selects active workspaces"

    assert read_jsonl(planned_runs) == records


@pytest.mark.parametrize(
    ("initial_text", "expected_first_line"),
    [
        (
            json.dumps(
                {
                    "schema_version": "planned-run.v1",
                    "workspace_id": "seed_workspace",
                    "decision": "selected",
                },
                sort_keys=True,
            )
            + "\n",
            json.dumps(
                {
                    "schema_version": "planned-run.v1",
                    "workspace_id": "seed_workspace",
                    "decision": "selected",
                },
                sort_keys=True,
            ),
        ),
        (
            json.dumps(
                {
                    "schema_version": "planned-run.v1",
                    "workspace_id": "truncated_workspace",
                    "decision": "selected",
                },
                sort_keys=True,
            )[:-1],
            None,
        ),
    ],
)
def test_selector_appends_planned_runs_without_merging_existing_terminal_line(
    tmp_path: Path,
    initial_text: str,
    expected_first_line: str | None,
) -> None:
    workspace_root = tmp_path / "workspaces" / "selected"
    workspace_root.mkdir(parents=True)
    manifest_path = write_manifest(workspace_root, subject_id="subject.selected")
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="selected_workspace",
                workspace_root=workspace_root,
                manifest_path=manifest_path,
            )
        ],
    )
    planned_runs = tmp_path / "planned-runs.jsonl"
    planned_runs.write_text(initial_text, encoding="utf-8")

    proc = run_selector(
        [
            "--registry",
            str(registry_path),
            "--planner-run-id",
            "planner-fixture",
            "--planned-at",
            "2026-01-01T00:00:00Z",
            "--planned-runs-jsonl",
            str(planned_runs),
            "--format",
            "json",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = planned_runs.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    if expected_first_line is not None:
        assert lines[0] == expected_first_line
    else:
        assert "truncated_workspace" in lines[0]
    appended = json.loads(lines[1])
    assert appended["workspace_id"] == "selected_workspace"
    assert appended["decision"] == "selected"
    assert appended["planned_run_id"] == "planner-fixture:selected_workspace"


def test_selector_append_planned_runs_deduplicates_duplicates_in_single_call(tmp_path: Path) -> None:
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="selected_workspace",
                workspace_root=tmp_path / "workspace",
                manifest_path=tmp_path / "manifest.json",
            )
        ],
    )
    registry_root = tmp_path / "workspace"
    registry_root.mkdir()
    manifest = write_manifest(registry_root, subject_id="subject.selected")
    record = {
        "schema_version": "planned-run.v1",
        "planner_run_id": "planner-fixture",
        "planned_run_id": "planner-fixture:selected_workspace",
        "planned_at": "2026-01-01T00:00:00Z",
        "workspace_id": "selected_workspace",
        "decision": "selected",
        "cadence_reason": "schedule_posture:scheduled",
        "skipped_reason": None,
        "skipped_reasons": [],
        "run_budget": {"max_attempts": 1},
        "retry_policy": None,
        "failure_state": None,
        "workspace_root": str(registry_root),
        "resolved_workspace_root": str(registry_root),
        "default_subject_manifest": str(manifest),
        "resolved_default_subject_manifest": str(manifest),
        "selection_explanation_id": "explanation-id",
        "saturation": None,
        "saturation_override": False,
        "registry_path": str(registry_path),
    }
    planned_runs = tmp_path / "planned-runs.jsonl"
    selector.append_planned_run_records(planned_runs, [record, record])

    assert read_jsonl(planned_runs) == [record]


def test_selector_append_planned_runs_uses_streaming_reader_for_existing_file(tmp_path: Path) -> None:
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="selected_workspace",
                workspace_root=tmp_path / "workspace",
                manifest_path=tmp_path / "manifest.json",
            )
        ],
    )
    registry_root = tmp_path / "workspace"
    registry_root.mkdir()
    manifest = write_manifest(registry_root, subject_id="subject.selected")
    record = {
        "schema_version": "planned-run.v1",
        "planner_run_id": "planner-fixture",
        "planned_run_id": "planner-fixture:selected_workspace",
        "planned_at": "2026-01-01T00:00:00Z",
        "workspace_id": "selected_workspace",
        "decision": "selected",
        "cadence_reason": "schedule_posture:scheduled",
        "skipped_reason": None,
        "skipped_reasons": [],
        "run_budget": {"max_attempts": 1},
        "retry_policy": None,
        "failure_state": None,
        "workspace_root": str(registry_root),
        "resolved_workspace_root": str(registry_root),
        "default_subject_manifest": str(manifest),
        "resolved_default_subject_manifest": str(manifest),
        "selection_explanation_id": "explanation-id",
        "saturation": None,
        "saturation_override": False,
        "registry_path": str(registry_path),
    }
    planned_runs = tmp_path / "planned-runs.jsonl"
    planned_runs.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )

    selector.append_planned_run_records(planned_runs, [record])

    assert read_jsonl(planned_runs) == [record]


def test_selector_append_is_idempotent_when_planned_run_ids_repeat(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces" / "selected"
    workspace_root.mkdir(parents=True)
    manifest_path = write_manifest(workspace_root, subject_id="subject.selected")
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="selected_workspace",
                workspace_root=workspace_root,
                manifest_path=manifest_path,
            )
        ],
    )
    planned_runs = tmp_path / "planned-runs.jsonl"

    first_run = run_selector(
        [
            "--registry",
            str(registry_path),
            "--planner-run-id",
            "planner-idempotent",
            "--planned-at",
            "2026-01-01T00:00:00Z",
            "--planned-runs-jsonl",
            str(planned_runs),
            "--format",
            "json",
        ]
    )
    assert first_run.returncode == 0, first_run.stdout + first_run.stderr

    second_run = run_selector(
        [
            "--registry",
            str(registry_path),
            "--planner-run-id",
            "planner-idempotent",
            "--planned-at",
            "2026-01-01T00:00:00Z",
            "--planned-runs-jsonl",
            str(planned_runs),
            "--format",
            "json",
        ]
    )
    assert second_run.returncode == 0, second_run.stdout + second_run.stderr

    lines = [line for line in planned_runs.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["planner_run_id"] == "planner-idempotent"
    assert record["planned_run_id"] == "planner-idempotent:selected_workspace"


def test_selector_can_plan_manual_workspace_without_executing_it(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces" / "manual"
    workspace_root.mkdir(parents=True)
    manifest_path = write_manifest(workspace_root, subject_id="subject.manual")
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="manual_workspace",
                workspace_root=workspace_root,
                schedule_posture="manual",
                manifest_path=manifest_path,
            )
        ],
    )
    mtimes_before = {path: path.stat().st_mtime_ns for path in [registry_path, manifest_path]}

    proc = run_selector(
        [
            "--registry",
            str(registry_path),
            "--include-manual",
            "--planner-run-id",
            "planner-manual",
            "--planned-at",
            "2026-01-01T00:00:00Z",
            "--format",
            "json",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert {path: path.stat().st_mtime_ns for path in [registry_path, manifest_path]} == mtimes_before
    payload = json.loads(proc.stdout)
    assert payload["selected_count"] == 1
    assert payload["skipped_count"] == 0
    assert payload["selected_workspaces"][0]["workspace_id"] == "manual_workspace"
    explanation = payload["selection_explanation"]
    assert explanation["selected_candidate"]["candidate_id"] == "manual_workspace"
    assert payload["planned_run_records"] == [
        {
            **payload["planned_run_records"][0],
            "schema_version": "planned-run.v1",
            "planner_run_id": "planner-manual",
            "planned_run_id": "planner-manual:manual_workspace",
            "planned_at": "2026-01-01T00:00:00Z",
            "workspace_id": "manual_workspace",
            "selection_explanation_id": explanation["explanation_id"],
            "decision": "selected",
            "cadence_reason": "schedule_posture:manual",
            "skipped_reason": None,
            "skipped_reasons": [],
            "run_budget": {"max_attempts": 1},
        }
    ]


def test_selector_canonicalizes_planned_at_across_payloads(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces" / "selected"
    workspace_root.mkdir(parents=True)
    manifest_path = write_manifest(workspace_root, subject_id="subject.selected")
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="selected_workspace",
                workspace_root=workspace_root,
                manifest_path=manifest_path,
            )
        ],
    )

    proc = run_selector(
        [
            "--registry",
            str(registry_path),
            "--planned-at",
            "2026-01-01T00:00:00.250000+00:00",
            "--planner-run-id",
            "planner-fractional",
            "--format",
            "json",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    expected_at = "2026-01-01T00:00:00Z"

    assert payload["planned_run_records"][0]["planned_at"] == expected_at
    assert payload["selection_explanation"]["created_at"] == expected_at


def test_selector_records_limit_deferred_workspace(tmp_path: Path) -> None:
    first_root = tmp_path / "workspaces" / "first"
    second_root = tmp_path / "workspaces" / "second"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    first_manifest = write_manifest(first_root, subject_id="subject.first")
    second_manifest = write_manifest(second_root, subject_id="subject.second")
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(workspace_id="first_workspace", workspace_root=first_root, manifest_path=first_manifest),
            workspace_record(
                workspace_id="second_workspace",
                workspace_root=second_root,
                manifest_path=second_manifest,
            ),
        ],
    )

    proc = run_selector(
        [
            "--registry",
            str(registry_path),
            "--planner-run-id",
            "planner-limit",
            "--planned-at",
            "2026-01-01T00:00:00Z",
            "--limit",
            "1",
            "--format",
            "json",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    records_by_workspace = {record["workspace_id"]: record for record in payload["planned_run_records"]}

    assert payload["selected_count"] == 1
    assert payload["skipped_count"] == 1
    assert records_by_workspace["first_workspace"]["decision"] == "selected"
    assert records_by_workspace["second_workspace"]["decision"] == "skipped"
    assert records_by_workspace["second_workspace"]["skipped_reason"] == "selection limit reached"


def test_selector_derives_deterministic_planner_run_id_when_not_provided(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces" / "selected"
    workspace_root.mkdir(parents=True)
    manifest_path = write_manifest(workspace_root, subject_id="subject.selected")
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="selected_workspace",
                workspace_root=workspace_root,
                manifest_path=manifest_path,
            )
        ],
    )

    run = run_selector(
        [
            "--registry",
            str(registry_path),
            "--planned-at",
            "2026-01-01T00:00:00Z",
            "--format",
            "json",
        ]
    )
    assert run.returncode == 0, run.stdout + run.stderr

    run_second = run_selector(
        [
            "--registry",
            str(registry_path),
            "--planned-at",
            "2026-01-01T00:00:00Z",
            "--format",
            "json",
        ]
    )
    assert run_second.returncode == 0, run_second.stdout + run_second.stderr

    payload = json.loads(run.stdout)
    payload_second = json.loads(run_second.stdout)
    assert payload["planner_run_id"] == payload_second["planner_run_id"]
    assert payload["planner_run_id"].startswith("planner-")
    assert payload["planned_run_records"][0]["planned_run_id"] == (
        f"{payload['planner_run_id']}:selected_workspace"
    )


def test_selector_planner_id_derives_from_selected_workspace_ids(tmp_path: Path) -> None:
    first_root = tmp_path / "workspaces" / "first"
    second_root = tmp_path / "workspaces" / "second"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    first_manifest = write_manifest(first_root, subject_id="subject.first")
    second_manifest = write_manifest(second_root, subject_id="subject.second")
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="first_workspace",
                workspace_root=first_root,
                manifest_path=first_manifest,
            ),
            workspace_record(
                workspace_id="second_workspace",
                workspace_root=second_root,
                manifest_path=second_manifest,
            ),
        ],
    )

    full = run_selector(
        [
            "--registry",
            str(registry_path),
            "--planned-at",
            "2026-01-01T00:00:00Z",
            "--format",
            "json",
        ]
    )
    limited = run_selector(
        [
            "--registry",
            str(registry_path),
            "--planned-at",
            "2026-01-01T00:00:00Z",
            "--limit",
            "1",
            "--format",
            "json",
        ]
    )

    assert full.returncode == 0, full.stdout + full.stderr
    assert limited.returncode == 0, limited.stdout + limited.stderr
    assert json.loads(full.stdout)["planner_run_id"] != json.loads(limited.stdout)["planner_run_id"]


def test_selector_rejects_invalid_run_budget() -> None:
    proc = run_selector(["--run-budget-max-attempts", "0"])

    assert proc.returncode == 2
    assert "--run-budget-max-attempts must be at least 1" in proc.stderr


def test_selector_skips_workspace_over_attempt_budget(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces" / "budgeted"
    workspace_root.mkdir(parents=True)
    manifest_path = write_manifest(workspace_root, subject_id="subject.budgeted")
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="budgeted_workspace",
                workspace_root=workspace_root,
                manifest_path=manifest_path,
                scheduler_policy={
                    "run_budget": {"max_attempts": 2},
                    "failure_state": {
                        "status": "retryable",
                        "attempt_count": 2,
                        "last_failure_at": "2026-01-01T00:00:00Z",
                        "last_failure_reason": "fixture failure",
                    },
                },
            )
        ],
    )

    proc = run_selector(
        [
            "--registry",
            str(registry_path),
            "--planner-run-id",
            "planner-budget",
            "--planned-at",
            "2026-01-01T00:05:00Z",
            "--format",
            "json",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["selected_count"] == 0
    assert payload["skipped_count"] == 1
    record = payload["planned_run_records"][0]
    assert record["decision"] == "skipped"
    assert record["skipped_reason"] == "attempt_count 2 reached run_budget.max_attempts 2"
    assert record["failure_state"]["status"] == "retryable"
    assert record["run_budget"] == {"max_attempts": 2}


def test_selector_skips_retryable_workspace_in_backoff_window(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces" / "backoff"
    workspace_root.mkdir(parents=True)
    manifest_path = write_manifest(workspace_root, subject_id="subject.backoff")
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="backoff_workspace",
                workspace_root=workspace_root,
                manifest_path=manifest_path,
                scheduler_policy={
                    "run_budget": {"max_attempts": 3},
                    "retry_policy": {"max_retryable_failures": 2, "backoff_seconds": 900},
                    "failure_state": {
                        "status": "retryable",
                        "attempt_count": 1,
                        "last_failure_at": "2026-01-01T00:00:00Z",
                        "last_failure_reason": "fixture failure",
                    },
                },
            )
        ],
    )

    proc = run_selector(
        [
            "--registry",
            str(registry_path),
            "--planner-run-id",
            "planner-backoff",
            "--planned-at",
            "2026-01-01T00:10:00Z",
            "--format",
            "json",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["selected_count"] == 0
    assert payload["skipped_count"] == 1
    record = payload["planned_run_records"][0]
    assert record["decision"] == "skipped"
    assert record["skipped_reason"] == "retry backoff active until 2026-01-01T00:15:00Z"
    assert record["retry_policy"] == {"backoff_seconds": 900, "max_retryable_failures": 2}


def test_selector_skips_blocked_workspace_and_surfaces_failure_state(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces" / "blocked"
    workspace_root.mkdir(parents=True)
    manifest_path = write_manifest(workspace_root, subject_id="subject.blocked")
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="blocked_workspace",
                workspace_root=workspace_root,
                manifest_path=manifest_path,
                scheduler_policy={
                    "run_budget": {"max_attempts": 5},
                    "failure_state": {
                        "status": "blocked",
                        "attempt_count": 3,
                        "blocked_reason": "manual investigation required",
                    },
                },
            )
        ],
    )

    proc = run_selector(
        [
            "--registry",
            str(registry_path),
            "--planner-run-id",
            "planner-blocked",
            "--planned-at",
            "2026-01-01T00:20:00Z",
            "--format",
            "json",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["selected_count"] == 0
    assert payload["skipped_count"] == 1
    record = payload["planned_run_records"][0]
    assert record["decision"] == "skipped"
    assert record["skipped_reason"] == "failure_state is blocked: manual investigation required"
    assert record["failure_state"] == {
        "attempt_count": 3,
        "blocked_reason": "manual investigation required",
        "status": "blocked",
    }
