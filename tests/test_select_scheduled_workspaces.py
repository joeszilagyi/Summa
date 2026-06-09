from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.scripts import select_scheduled_workspaces as selector
from tools.source_db_tools import canonical_store

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
    assert payload["selected_workspaces"][0] == {
        "workspace_id": "selected_workspace",
        "topic_label": "Selected Workspace",
        "lifecycle_state": "active",
        "schedule_posture": "scheduled",
    }

    manual = records_by_workspace["manual_workspace"]
    assert manual["decision"] == "skipped"
    assert manual["selection_explanation_id"] == explanation["explanation_id"]
    assert manual["skipped_reason"] == "schedule_posture is manual; pass --include-manual to include it"
    assert manual["skipped_reasons"] == [manual["skipped_reason"]]
    assert manual["cadence_reason"] == "schedule_posture:manual"
    assert payload["skipped_workspaces"][0] == {
        "workspace_id": "manual_workspace",
        "topic_label": "Manual Workspace",
        "lifecycle_state": "active",
        "schedule_posture": "manual",
        "reasons": ["schedule_posture is manual; pass --include-manual to include it"],
    }

    missing_manifest = records_by_workspace["missing_manifest_workspace"]
    assert missing_manifest["decision"] == "skipped"
    assert missing_manifest["skipped_reason"] == (
        "default_subject_manifest is missing or unresolved; scheduled runs need an explicit manifest"
    )
    assert "default_subject_manifest" not in payload["selected_workspaces"][0]
    assert "resolved_workspace_root" not in payload["selected_workspaces"][0]
    assert "default_subject_manifest" not in payload["skipped_workspaces"][0]
    assert "resolved_workspace_root" not in payload["skipped_workspaces"][0]
    assert explanation["selected_candidate"]["metadata"]["workspace_root"] == str(
        selected_root
    )
    assert records_by_workspace["selected_workspace"]["resolved_workspace_root"] == str(
        selected_root
    )

    inactive = records_by_workspace["inactive_workspace"]
    assert inactive["decision"] == "skipped"
    assert inactive["skipped_reason"] == "lifecycle_state is 'paused'; scheduler only selects active workspaces"

    assert read_jsonl(planned_runs) == records


def test_selector_loads_registry_json_once_during_validation_and_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    load_calls = {"count": 0}
    original_load_registry_json = selector.load_registry_json

    def counting_load_registry_json(path: Path) -> dict[str, object]:
        load_calls["count"] += 1
        return original_load_registry_json(path)

    monkeypatch.setattr(selector, "load_registry_json", counting_load_registry_json)

    payload = selector.build_selection_payload(
        selector.argparse.Namespace(
            registry=str(registry_path),
            workspace_ids=[],
            include_manual=False,
            limit=None,
            format="json",
            planned_runs_jsonl=None,
            planner_run_id="planner-fixture",
            planned_at=None,
            run_budget_max_attempts=None,
            run_budget_max_runtime_seconds=None,
            db=None,
            saturation_policy=None,
            include_saturated=False,
            ignore_saturation=False,
        )
    )

    assert load_calls["count"] == 1
    assert payload["selected_count"] == 1
    assert payload["selected_workspaces"][0]["workspace_id"] == "selected_workspace"


def test_selector_rejects_invalid_subject_manifest_json_by_default(tmp_path: Path) -> None:
    manifest_path = tmp_path / "bad-manifest.json"
    manifest_path.write_text(
        '{"schema_version":"subject-manifest.v1","subject_id":"one","subject_id":"two"}',
        encoding="utf-8",
    )

    with pytest.raises(selector.SelectionError, match="default subject manifest"):
        selector.load_subject_id_from_manifest(str(manifest_path))


def test_selector_can_allow_unresolved_subject_manifest_when_explicitly_enabled(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "bad-manifest.json"
    manifest_path.write_text(
        '{"schema_version":"subject-manifest.v1","subject_id":"one","subject_id":"two"}',
        encoding="utf-8",
    )

    assert selector.load_subject_id_from_manifest(
        str(manifest_path),
        allow_unresolved=True,
    ) is None


def test_selector_uses_cached_subject_id_for_saturation_without_reloading_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at="2026-01-01T00:00:00Z",
        applied_by="pytest.selector",
    )
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            event = canonical_store.record_provenance_event(
                conn,
                object_namespace="gather_candidate_batch",
                object_id="run-1",
                event_type="gather_candidate_batch_ingest",
                tool_name="tests/test_select_scheduled_workspaces.py",
                run_id="run-1",
                event_timestamp="2026-01-01T00:00:00Z",
                source_object_namespace="topic_subject",
                source_object_id="subject.selected",
                note_text=json.dumps(
                    {
                        "subject_id": "subject.selected",
                        "facet": "sources",
                        "cycle_depth": 1,
                    },
                    sort_keys=True,
                ),
                provenance_event_key_v1="prov:selected:run-1",
            )
            canonical_store.record_source_claim(
                conn,
                provenance_event_ref=event.event_key,
                source_claim_key_v1="claim:selected:run-1",
                about_object_ref="subject:selected_workspace",
                claim_text="Reviewable claim",
                claim_type="fixture_claim",
                review_state="needs_review",
                workspace_id="selected_workspace",
                created_at="2026-01-01T00:00:00Z",
                record_last_updated="2026-01-01T00:00:00Z",
            )
    finally:
        conn.close()

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

    def fail_if_reloaded(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("subject manifest should have been cached during registry validation")

    monkeypatch.setattr(selector, "load_subject_id_from_manifest", fail_if_reloaded)

    payload = selector.build_selection_payload(
        selector.argparse.Namespace(
            registry=str(registry_path),
            workspace_ids=[],
            include_manual=False,
            limit=None,
            format="json",
            planned_runs_jsonl=None,
            planner_run_id="planner-fixture",
            planned_at="2026-01-01T00:00:00Z",
            run_budget_max_attempts=None,
            run_budget_max_runtime_seconds=None,
            db=str(db_path),
            saturation_policy=str(REPO_ROOT / "config" / "topic_saturation_policy.v1.json"),
            include_saturated=False,
            ignore_saturation=False,
        )
    )

    assert payload["selected_count"] == 1
    assert payload["selected_workspaces"][0]["workspace_id"] == "selected_workspace"


def test_selector_batches_saturation_evaluation_across_workspaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "canonical.sqlite"
    selector.canonical_store.init_canonical_store(
        db_path,
        applied_at="2026-01-01T00:00:00Z",
        applied_by="pytest.selector",
    )

    selected_root = tmp_path / "workspaces" / "selected"
    other_root = tmp_path / "workspaces" / "other"
    selected_root.mkdir(parents=True)
    other_root.mkdir(parents=True)
    selected_manifest = write_manifest(selected_root, subject_id="subject.selected")
    other_manifest = write_manifest(other_root, subject_id="subject.other")
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="selected_workspace",
                workspace_root=selected_root,
                manifest_path=selected_manifest,
            ),
            workspace_record(
                workspace_id="other_workspace",
                workspace_root=other_root,
                manifest_path=other_manifest,
            ),
        ],
    )

    call_count = {"count": 0}
    original_evaluate_saturations = selector.topic_saturation.evaluate_saturations

    def counting_evaluate_saturations(*args: object, **kwargs: object) -> dict[str, object]:
        call_count["count"] += 1
        return original_evaluate_saturations(*args, **kwargs)

    monkeypatch.setattr(selector.topic_saturation, "evaluate_saturations", counting_evaluate_saturations)

    payload = selector.build_selection_payload(
        selector.argparse.Namespace(
            registry=str(registry_path),
            workspace_ids=[],
            include_manual=False,
            limit=None,
            format="json",
            planned_runs_jsonl=None,
            planner_run_id="planner-fixture",
            planned_at="2026-01-01T00:00:00Z",
            run_budget_max_attempts=None,
            run_budget_max_runtime_seconds=None,
            db=str(db_path),
            saturation_policy=str(REPO_ROOT / "config" / "topic_saturation_policy.v1.json"),
            include_saturated=False,
            ignore_saturation=False,
        )
    )

    assert call_count["count"] == 1
    assert payload["selected_count"] == 2
    assert {workspace["workspace_id"] for workspace in payload["selected_workspaces"]} == {
        "selected_workspace",
        "other_workspace",
    }


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


def test_selector_append_planned_runs_can_skip_fsync_when_relaxed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    fsync_calls = {"count": 0}
    original_fsync = selector.os.fsync

    def counting_fsync(fd: int) -> None:
        fsync_calls["count"] += 1
        original_fsync(fd)

    monkeypatch.setattr(selector.os, "fsync", counting_fsync)

    selector.append_planned_run_records(planned_runs, [record], sync=False)

    assert fsync_calls["count"] == 0
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


def test_selector_planned_run_record_reuses_policy_snapshots_without_deepcopy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspaces" / "selected"
    manifest_path = tmp_path / "manifest.json"
    run_budget = {"max_attempts": 2, "max_runtime_seconds": 900}
    retry_policy = {"backoff_seconds": 60, "max_retryable_failures": 3}
    failure_state = {"status": "retryable", "attempt_count": 1}
    saturation = {
        "schema_version": "topic-saturation.v1",
        "policy_id": "topic-saturation.test",
        "workspace_id": "selected_workspace",
        "subject_id": "subject.selected",
    }
    entry = {
        "workspace_id": "selected_workspace",
        "schedule_posture": "scheduled",
        "reasons": [],
        "workspace_root": str(workspace_root),
        "resolved_workspace_root": str(workspace_root),
        "default_subject_manifest": str(manifest_path),
        "resolved_default_subject_manifest": str(manifest_path),
        "saturation": saturation,
        "saturation_override": True,
    }
    registry_path = tmp_path / "registry.json"

    def fail_deepcopy(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("planned_run_record should reuse existing policy snapshots")

    monkeypatch.setattr(selector.copy, "deepcopy", fail_deepcopy)

    record = selector.planned_run_record(
        entry=entry,
        decision="selected",
        registry_path=registry_path,
        planner_run_id="planner-fixture",
        planned_at="2026-01-01T00:00:00Z",
        run_budget=run_budget,
        retry_policy=retry_policy,
        failure_state=failure_state,
    )

    assert record["run_budget"] is run_budget
    assert record["retry_policy"] is retry_policy
    assert record["failure_state"] is failure_state
    assert record["saturation"] is saturation
    assert record["workspace_root"] == str(workspace_root)
    assert record["resolved_workspace_root"] == str(workspace_root)
    assert record["default_subject_manifest"] == str(manifest_path)
    assert record["resolved_default_subject_manifest"] == str(manifest_path)


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


def test_selector_append_planned_runs_uses_file_lock_and_deduplicates_existing_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    record = {
        "schema_version": "planned-run.v1",
        "planner_run_id": "planner-lock",
        "planned_run_id": "planner-lock:selected_workspace",
        "planned_at": "2026-01-01T00:00:00Z",
        "workspace_id": "selected_workspace",
        "decision": "selected",
        "cadence_reason": "schedule_posture:scheduled",
        "skipped_reason": None,
        "skipped_reasons": [],
        "run_budget": {"max_attempts": 1},
        "retry_policy": None,
        "failure_state": None,
        "workspace_root": str(workspace_root),
        "resolved_workspace_root": str(workspace_root),
        "default_subject_manifest": str(manifest_path),
        "resolved_default_subject_manifest": str(manifest_path),
        "selection_explanation_id": "explanation-id",
        "saturation": None,
        "saturation_override": False,
        "registry_path": str(registry_path),
    }
    planned_runs = tmp_path / "planned-runs.jsonl"
    planned_runs.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    flock_flags: list[int] = []
    original_flock = selector.fcntl.flock

    def counting_flock(fd: int, flag: int) -> None:
        flock_flags.append(flag)
        return original_flock(fd, flag)

    monkeypatch.setattr(selector.fcntl, "flock", counting_flock)

    selector.append_planned_run_records(planned_runs, [record, record])

    assert read_jsonl(planned_runs) == [record]
    assert flock_flags[0] == fcntl.LOCK_EX
    assert flock_flags[-1] == fcntl.LOCK_UN


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


def test_selector_text_output_uses_planned_run_records_for_workspace_details(tmp_path: Path) -> None:
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
            "--planner-run-id",
            "planner-text",
            "--planned-at",
            "2026-01-01T00:00:00Z",
            "--format",
            "text",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "selected[0].workspace_id=selected_workspace" in proc.stdout
    assert f"selected[0].workspace_root={workspace_root}" in proc.stdout
    assert f"selected[0].subject_manifest={manifest_path}" in proc.stdout


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


def test_selector_helper_paths_cover_validation_and_policy_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = write_manifest(tmp_path, subject_id="subject.helper")
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="helper_workspace",
                workspace_root=tmp_path,
                manifest_path=manifest_path,
                scheduler_policy={
                    "run_budget": {"max_attempts": 5},
                    "retry_policy": {"backoff_seconds": 60, "max_retryable_failures": 2},
                    "failure_state": {
                        "status": "retryable",
                        "attempt_count": 3,
                        "last_failure_at": "2026-01-01T00:00:00Z",
                    },
                },
            )
        ],
    )
    policy_path = REPO_ROOT / "config" / "topic_saturation_policy.v1.json"
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at="2026-01-01T00:00:00Z",
        applied_by="pytest.selector",
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "select_scheduled_workspaces.py",
            "--registry",
            str(registry_path),
            "--workspace-id",
            "helper_workspace",
            "--include-manual",
            "--limit",
            "2",
            "--format",
            "text",
            "--planned-runs-jsonl",
            str(tmp_path / "planned.jsonl"),
            "--planner-run-id",
            "planner-helper",
            "--planned-at",
            "2026-01-01T00:00:00Z",
            "--run-budget-max-attempts",
            "2",
            "--run-budget-max-runtime-seconds",
            "900",
            "--db",
            str(db_path),
            "--saturation-policy",
            str(policy_path),
            "--include-saturated",
            "--ignore-saturation",
        ],
    )
    parsed = selector.parse_args()
    assert parsed.registry == str(registry_path)
    assert parsed.workspace_ids == ["helper_workspace"]
    assert parsed.include_manual is True
    assert parsed.limit == 2
    assert parsed.format == "text"
    assert parsed.planned_runs_jsonl == tmp_path / "planned.jsonl"
    assert parsed.planner_run_id == "planner-helper"
    assert parsed.planned_at == "2026-01-01T00:00:00Z"
    assert parsed.run_budget_max_attempts == 2
    assert parsed.run_budget_max_runtime_seconds == 900
    assert parsed.db == str(db_path)
    assert parsed.saturation_policy == str(policy_path)
    assert parsed.include_saturated is True
    assert parsed.ignore_saturation is True

    with pytest.raises(argparse.ArgumentTypeError, match="must be an integer"):
        selector.positive_int("x", option_name="--limit")
    with pytest.raises(argparse.ArgumentTypeError, match="must be at least 1"):
        selector.positive_int("0", option_name="--limit")
    assert selector.positive_int("2", option_name="--limit") == 2
    positive_limit = selector.positive_int_arg("--limit")
    assert positive_limit("3") == 3
    with pytest.raises(argparse.ArgumentTypeError, match="must be at least 1"):
        positive_limit("0")

    parsed_timestamp = selector.parse_timestamp("2026-01-01T00:00:00Z", label="planned_at")
    assert parsed_timestamp.isoformat() == "2026-01-01T00:00:00+00:00"
    with pytest.raises(selector.SelectionError, match="ISO-8601 timestamp"):
        selector.parse_timestamp("not-a-timestamp", label="planned_at")
    assert selector.normalize_planned_at("2026-01-01T00:00:00.250000+00:00", label="planned_at") == "2026-01-01T00:00:00Z"
    with pytest.raises(selector.SelectionError, match="must include a timezone"):
        selector.normalize_planned_at("2026-01-01T00:00:00", label="planned_at")
    assert selector.utc_now().endswith("Z")

    digest_payload = tmp_path / "payload.txt"
    digest_payload.write_text("alpha", encoding="utf-8")
    assert selector._hash_file_bytes(digest_payload) == hashlib.sha256(b"alpha").hexdigest()

    workspace = {
        "workspace_id": "helper_workspace",
        "topic_label": "Helper Workspace",
        "lifecycle_state": "active",
        "schedule_posture": "scheduled",
        "resolved_workspace_root": tmp_path,
        "resolved_default_subject_manifest": manifest_path,
        "scheduler_policy": {
            "run_budget": {"max_attempts": 3},
            "retry_policy": {"backoff_seconds": 60, "max_retryable_failures": 2},
            "failure_state": {
                "status": "retryable",
                "attempt_count": 3,
                "last_failure_at": "2026-01-01T00:00:00Z",
            },
        },
    }
    validated = selector.validate_registry_or_raise(registry_path)
    assert validated["workspaces"][0]["workspace_id"] == "helper_workspace"
    summary = selector.workspace_selection_summary(workspace, reasons=["manual"])
    assert summary["workspace_id"] == "helper_workspace"
    assert summary["reasons"] == ["manual"]
    assert "saturation" not in summary
    assert selector.cadence_reason({"schedule_posture": "scheduled"}) == "schedule_posture:scheduled"
    assert selector.cadence_reason({}) == "schedule_posture:unknown"

    output_entry = selector.workspace_output_entry(
        {
            "workspace_id": "helper_workspace",
            "topic_label": "Helper Workspace",
            "domain_pack": "general.v1",
            "lifecycle_state": "active",
            "schedule_posture": "scheduled",
            "workspace_policy_class": "private_local",
            "workspace_root": str(tmp_path),
            "resolved_workspace_root": tmp_path,
            "default_subject_manifest": str(manifest_path),
            "resolved_default_subject_manifest": manifest_path,
            "scheduler_policy": {"run_budget": {"max_attempts": 3}},
        }
    )
    assert output_entry["resolved_workspace_root"] == str(tmp_path)
    assert output_entry["resolved_default_subject_manifest"] == str(manifest_path)
    assert output_entry["scheduler_policy"] == {"run_budget": {"max_attempts": 3}}
    assert output_entry["scheduler_policy"] is not workspace["scheduler_policy"]
    with pytest.raises(TypeError, match="workspace entry must be a JSON object"):
        selector.workspace_output_entry("not-a-workspace")

    effective = selector.effective_scheduler_policy(
        {"scheduler_policy": {"run_budget": {"max_attempts": 4}}},
        argparse.Namespace(run_budget_max_attempts=2, run_budget_max_runtime_seconds=900),
    )
    assert effective["run_budget"] == {"max_attempts": 2, "max_runtime_seconds": 900}
    effective = selector.effective_scheduler_policy(
        {"scheduler_policy": {"run_budget": {}}},
        argparse.Namespace(run_budget_max_attempts=None, run_budget_max_runtime_seconds=None),
    )
    assert effective["run_budget"] == {"max_attempts": 1}

    explicit_retry = selector.derived_next_retry_at(
        {
            "failure_state": {"next_retry_at": "2026-01-01T00:05:00Z"},
            "retry_policy": {"backoff_seconds": 60},
        }
    )
    assert explicit_retry == "2026-01-01T00:05:00Z"
    computed_retry = selector.derived_next_retry_at(
        {
            "failure_state": {"last_failure_at": "2026-01-01T00:00:00Z"},
            "retry_policy": {"backoff_seconds": 60},
        }
    )
    assert computed_retry == "2026-01-01T00:01:00Z"
    assert selector.derived_next_retry_at({"failure_state": {}, "retry_policy": {}}) is None

    assert selector.scheduler_ineligibility_reasons(
        {
            "lifecycle_state": "paused",
            "schedule_posture": "manual",
        },
        include_manual=False,
    ) == [
        "lifecycle_state is 'paused'; scheduler only selects active workspaces",
        "schedule_posture is manual; pass --include-manual to include it",
        "default_subject_manifest is missing or unresolved; scheduled runs need an explicit manifest",
    ]
    assert selector.scheduler_ineligibility_reasons(
        {
            "lifecycle_state": "active",
            "schedule_posture": "adhoc",
            "resolved_default_subject_manifest": manifest_path,
        },
        include_manual=False,
    ) == ["schedule_posture is 'adhoc'; scheduler only selects scheduled"]

    blocked_reasons = selector.scheduler_policy_ineligibility_reasons(
        {
            "scheduler_policy": {
                "run_budget": {"max_attempts": 2},
                "failure_state": {
                    "status": "blocked",
                    "attempt_count": 2,
                    "blocked_reason": "manual review required",
                },
            }
        },
        args=argparse.Namespace(
            run_budget_max_attempts=None,
            run_budget_max_runtime_seconds=None,
        ),
        planned_at=selector.parse_timestamp("2026-01-01T00:20:00Z", label="planned_at"),
    )
    assert blocked_reasons == [
        "failure_state is blocked: manual review required",
        "attempt_count 2 reached run_budget.max_attempts 2",
    ]
    retryable_reasons = selector.scheduler_policy_ineligibility_reasons(
        {
            "scheduler_policy": {
                "run_budget": {"max_attempts": 3},
                "retry_policy": {"max_retryable_failures": 2, "backoff_seconds": 900},
                "failure_state": {
                    "status": "retryable",
                    "attempt_count": 3,
                    "last_failure_at": "2026-01-01T00:00:00Z",
                },
            }
        },
        args=argparse.Namespace(
            run_budget_max_attempts=None,
            run_budget_max_runtime_seconds=None,
        ),
        planned_at=selector.parse_timestamp("2026-01-01T00:05:00Z", label="planned_at"),
    )
    assert retryable_reasons == [
        "attempt_count 3 reached run_budget.max_attempts 3",
        "retryable failure count 3 exceeded retry_policy.max_retryable_failures 2",
        "retry backoff active until 2026-01-01T00:15:00Z",
    ]

    subject_manifest = selector.load_subject_id_from_manifest(str(manifest_path))
    assert subject_manifest == "subject.helper"
    assert selector.load_subject_id_from_manifest(None) is None
    assert selector.load_subject_id_from_manifest("", allow_unresolved=True) is None
    unresolved_manifest = tmp_path / "bad-manifest.json"
    unresolved_manifest.write_text(
        '{"schema_version":"subject-manifest.v1","subject_id":"one","subject_id":"two"}',
        encoding="utf-8",
    )
    with pytest.raises(selector.SelectionError, match="failed validation"):
        selector.load_subject_id_from_manifest(str(unresolved_manifest))
    assert selector.load_subject_id_from_manifest(
        str(unresolved_manifest),
        allow_unresolved=True,
    ) is None
    assert selector.workspace_allows_unresolved_subject_manifest(
        {
            "scheduler_policy": {"extensions": {"allow_unresolved_subject_manifest": True}}
        }
    )
    assert not selector.workspace_allows_unresolved_subject_manifest({})
    assert selector.resolve_saturation_subject_id(
        {"resolved_default_subject_id": "subject.cached"}
    ) == "subject.cached"
    assert selector.resolve_saturation_subject_id(
        {"default_subject_manifest": str(manifest_path)}
    ) == "subject.helper"

    ignored_policy, ignored_conn = selector.saturation_context(
        argparse.Namespace(ignore_saturation=True, saturation_policy=None, db=None)
    )
    assert ignored_policy is None and ignored_conn is None
    with pytest.raises(selector.SelectionError, match="--db is required"):
        selector.saturation_context(
            argparse.Namespace(
                ignore_saturation=False,
                saturation_policy=str(policy_path),
                db=None,
            )
        )
    policy, conn = selector.saturation_context(
        argparse.Namespace(
            ignore_saturation=False,
            saturation_policy=str(policy_path),
            db=str(db_path),
        )
    )
    try:
        assert policy.policy_id == selector.topic_saturation.load_policy(str(policy_path)).policy_id
        assert conn is not None
    finally:
        if conn is not None:
            conn.close()

    validated = selector.validate_registry_or_raise(registry_path)
    assert validated["workspaces"][0]["workspace_id"] == "helper_workspace"
    with monkeypatch.context() as patch:
        patch.setattr(
            selector,
            "load_registry_json",
            lambda path: (_ for _ in ()).throw(selector.TopicWorkspaceRegistryError("bad load")),
        )
        with pytest.raises(selector.SelectionError, match="bad load"):
            selector.validate_registry_or_raise(registry_path)
    with monkeypatch.context() as patch:
        patch.setattr(
            selector.validate_topic_workspace_registry,
            "validate_topic_workspace_registry",
            lambda registry_path, payload=None: ({"errors": [{"message": "bad registry"}]}, 1),
        )
        with pytest.raises(selector.SelectionError, match="bad registry"):
            selector.validate_registry_or_raise(registry_path)


def test_selector_saturation_and_append_paths_cover_batch_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, subject_id="subject.batch")
    unresolved_workspace = {
        "workspace_id": "unresolved_workspace",
        "schedule_posture": "scheduled",
        "resolved_workspace_root": workspace_root,
        "resolved_default_subject_manifest": manifest_path,
    }
    resolved_workspace = {
        "workspace_id": "resolved_workspace",
        "schedule_posture": "scheduled",
        "resolved_workspace_root": workspace_root,
        "resolved_default_subject_manifest": manifest_path,
    }
    unresolved_entry = selector.workspace_output_entry(
        {
            "workspace_id": "unresolved_workspace",
            "topic_label": "Unresolved Workspace",
            "domain_pack": "general.v1",
            "lifecycle_state": "active",
            "schedule_posture": "scheduled",
            "workspace_policy_class": "private_local",
            "workspace_root": str(workspace_root),
            "resolved_workspace_root": workspace_root,
            "default_subject_manifest": str(manifest_path),
            "resolved_default_subject_manifest": manifest_path,
        }
    )
    resolved_entry = selector.workspace_output_entry(
        {
            "workspace_id": "resolved_workspace",
            "topic_label": "Resolved Workspace",
            "domain_pack": "general.v1",
            "lifecycle_state": "active",
            "schedule_posture": "scheduled",
            "workspace_policy_class": "private_local",
            "workspace_root": str(workspace_root),
            "resolved_workspace_root": workspace_root,
            "default_subject_manifest": str(manifest_path),
            "resolved_default_subject_manifest": manifest_path,
        }
    )

    monkeypatch.setattr(
        selector,
        "resolve_saturation_subject_id",
        lambda workspace: None if workspace["workspace_id"] == "unresolved_workspace" else "subject.resolved",
    )
    fake_policy = argparse.Namespace(policy_id="topic-saturation.helper")
    monkeypatch.setattr(
        selector.topic_saturation,
        "evaluate_saturations",
        lambda conn, workspace_subject_pairs, policy, evaluated_at: {
            workspace_id: {
                "schema_version": selector.topic_saturation.SCHEMA_VERSION,
                "policy_id": policy.policy_id,
                "workspace_id": workspace_id,
                "subject_id": subject_id,
                "state": "cooldown",
                "scheduler_action": "cooldown",
                "reason_codes": ["low_recent_yield"],
                "next_eligible_cycle": 3,
                "recent_yield_summary": selector.topic_saturation.empty_summary(),
            }
            for workspace_id, subject_id in workspace_subject_pairs
        },
    )
    selector.attach_saturation_batch(
        [(unresolved_workspace, unresolved_entry), (resolved_workspace, resolved_entry)],
        policy=fake_policy,
        conn=object(),
        planned_at="2026-01-01T00:00:00Z",
    )
    assert unresolved_entry["saturation"]["state"] == "not_evaluated"
    assert unresolved_entry["saturation"]["reason_codes"] == ["subject_unresolved"]
    assert resolved_entry["saturation"]["scheduler_action"] == "cooldown"
    assert resolved_entry["saturation"]["next_eligible_cycle"] == 3

    single_entry = selector.workspace_output_entry(
        {
            "workspace_id": "single_workspace",
            "topic_label": "Single Workspace",
            "domain_pack": "general.v1",
            "lifecycle_state": "active",
            "schedule_posture": "scheduled",
            "workspace_policy_class": "private_local",
            "workspace_root": str(workspace_root),
            "resolved_workspace_root": workspace_root,
            "default_subject_manifest": str(manifest_path),
            "resolved_default_subject_manifest": manifest_path,
        }
    )
    selector.attach_saturation(
        single_entry,
        workspace={
            "workspace_id": "single_workspace",
            "default_subject_manifest": str(manifest_path),
            "resolved_default_subject_manifest": manifest_path,
        },
        policy=fake_policy,
        conn=object(),
        planned_at="2026-01-01T00:00:00Z",
    )
    assert single_entry["saturation"]["scheduler_action"] == "cooldown"

    entry = selector.workspace_output_entry(
        {
            "workspace_id": "saturated_workspace",
            "topic_label": "Saturated Workspace",
            "domain_pack": "general.v1",
            "lifecycle_state": "active",
            "schedule_posture": "scheduled",
            "workspace_policy_class": "private_local",
            "workspace_root": str(workspace_root),
            "resolved_workspace_root": workspace_root,
            "default_subject_manifest": str(manifest_path),
            "resolved_default_subject_manifest": manifest_path,
        }
    )
    entry["saturation"] = {
        "scheduler_action": "halt",
        "reason_codes": ["too_many_failures"],
    }
    assert selector.saturation_ineligibility_reasons(entry, include_saturated=False) == [
        "saturation_state is halted: too_many_failures"
    ]
    assert selector.saturation_ineligibility_reasons(entry, include_saturated=True) == []
    assert entry["saturation_override"] is True
    entry["saturation"] = {
        "scheduler_action": "cooldown",
        "next_eligible_cycle": 4,
        "reason_codes": ["low_recent_yield"],
    }
    assert selector.saturation_ineligibility_reasons(entry, include_saturated=False) == [
        "saturation_state is cooldown until cycle 4: low_recent_yield"
    ]

    selector.build_planned_run_records(
        selected=[resolved_entry],
        skipped=[unresolved_entry],
        registry_path=tmp_path / "registry.json",
        args=argparse.Namespace(
            planner_run_id="planner-helper",
            planned_at="2026-01-01T00:00:00Z",
            run_budget_max_attempts=None,
            run_budget_max_runtime_seconds=None,
        ),
    )
    with pytest.raises(RuntimeError, match="planned_at must be set"):
        selector.build_planned_run_records(
            selected=[],
            skipped=[],
            registry_path=tmp_path / "registry.json",
            args=argparse.Namespace(planned_at=None),
        )

    planned_runs = tmp_path / "planned-runs.jsonl"
    existing = {
        "schema_version": "planned-run.v1",
        "planner_run_id": "planner-existing",
        "planned_run_id": "planner-existing:existing_workspace",
        "planned_at": "2026-01-01T00:00:00Z",
        "workspace_id": "existing_workspace",
        "decision": "selected",
        "cadence_reason": "schedule_posture:scheduled",
        "skipped_reason": None,
        "skipped_reasons": [],
        "run_budget": {"max_attempts": 1},
        "retry_policy": None,
        "failure_state": None,
        "saturation": None,
        "saturation_override": False,
        "workspace_root": str(workspace_root),
        "resolved_workspace_root": str(workspace_root),
        "default_subject_manifest": str(manifest_path),
        "resolved_default_subject_manifest": str(manifest_path),
        "registry_path": str(tmp_path / "registry.json"),
        "selection_explanation_id": "explanation-id",
    }
    new_record = dict(existing)
    new_record.update(
        {
            "planner_run_id": "planner-new",
            "planned_run_id": "planner-new:new_workspace",
            "workspace_id": "new_workspace",
        }
    )
    planned_runs.write_text(
        json.dumps(existing, ensure_ascii=False, sort_keys=True)
        + "\n"
        + "{not json}\n"
        + json.dumps(existing, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    selector.append_planned_run_records(planned_runs, [existing, new_record, new_record])
    appended_text = planned_runs.read_text(encoding="utf-8")
    assert "{not json}" in appended_text
    assert appended_text.count("planner-existing:existing_workspace") == 2
    assert appended_text.count("planner-new:new_workspace") == 1
    assert appended_text.endswith("\n")


def test_selector_build_selection_payload_render_text_and_main_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    selected_root = tmp_path / "workspaces" / "selected"
    deprioritized_root = tmp_path / "workspaces" / "deprioritized"
    manual_root = tmp_path / "workspaces" / "manual"
    blocked_root = tmp_path / "workspaces" / "blocked"
    missing_root = tmp_path / "workspaces" / "missing"
    for root in (selected_root, deprioritized_root, manual_root, blocked_root, missing_root):
        root.mkdir(parents=True)

    selected_manifest = write_manifest(selected_root, subject_id="subject.selected")
    deprioritized_manifest = write_manifest(deprioritized_root, subject_id="subject.deprioritized")
    manual_manifest = write_manifest(manual_root, subject_id="subject.manual")
    blocked_manifest = write_manifest(blocked_root, subject_id="subject.blocked")

    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="selected_workspace",
                workspace_root=selected_root,
                manifest_path=selected_manifest,
            ),
            workspace_record(
                workspace_id="deprioritized_workspace",
                workspace_root=deprioritized_root,
                manifest_path=deprioritized_manifest,
            ),
            workspace_record(
                workspace_id="manual_workspace",
                workspace_root=manual_root,
                schedule_posture="manual",
                manifest_path=manual_manifest,
            ),
            workspace_record(
                workspace_id="blocked_workspace",
                workspace_root=blocked_root,
                manifest_path=blocked_manifest,
                scheduler_policy={
                    "run_budget": {"max_attempts": 1},
                    "failure_state": {
                        "status": "blocked",
                        "attempt_count": 1,
                        "blocked_reason": "manual review required",
                    },
                },
            ),
            workspace_record(
                workspace_id="missing_manifest_workspace",
                workspace_root=missing_root,
                manifest_path=None,
            ),
        ],
    )

    fake_policy = argparse.Namespace(policy_id="topic-saturation.helper")

    class FakeConn:
        def close(self) -> None:
            return None

    fake_conn = FakeConn()
    monkeypatch.setattr(
        selector,
        "saturation_context",
        lambda args: (fake_policy, fake_conn),
    )
    monkeypatch.setattr(
        selector.topic_saturation,
        "evaluate_saturations",
        lambda conn, workspace_subject_pairs, policy, evaluated_at: {
            workspace_id: {
                "schema_version": selector.topic_saturation.SCHEMA_VERSION,
                "policy_id": policy.policy_id,
                "workspace_id": workspace_id,
                "subject_id": subject_id,
                "state": "eligible",
                "scheduler_action": "run"
                if workspace_id == "selected_workspace"
                else "deprioritize",
                "reason_codes": ["recent_activity"]
                if workspace_id == "selected_workspace"
                else ["low_recent_yield"],
                "next_eligible_cycle": 5 if workspace_id == "deprioritized_workspace" else None,
                "recent_yield_summary": selector.topic_saturation.empty_summary(),
            }
            for workspace_id, subject_id in workspace_subject_pairs
        },
    )
    monkeypatch.setattr(
        selector,
        "resolve_saturation_subject_id",
        lambda workspace: (
            None
            if workspace["workspace_id"]
            in {"manual_workspace", "blocked_workspace", "missing_manifest_workspace"}
            else {
                "selected_workspace": "subject.selected",
                "deprioritized_workspace": "subject.deprioritized",
            }[workspace["workspace_id"]]
        ),
    )

    args = argparse.Namespace(
        registry=str(registry_path),
        workspace_ids=[],
        include_manual=False,
        limit=1,
        format="json",
        planned_runs_jsonl=None,
        planner_run_id="planner-direct",
        planned_at="2026-01-01T00:00:00Z",
        run_budget_max_attempts=1,
        run_budget_max_runtime_seconds=900,
        db=str(tmp_path / "canonical.sqlite"),
        saturation_policy=str(REPO_ROOT / "config" / "topic_saturation_policy.v1.json"),
        include_saturated=False,
        ignore_saturation=False,
        relaxed_planned_runs_write=False,
    )
    payload = selector.build_selection_payload(args)
    assert payload["registry_path"] == str(registry_path)
    assert payload["selected_count"] == 1
    assert payload["skipped_count"] == 4
    assert payload["planned_run_record_count"] == 5
    assert payload["selected_workspaces"][0]["workspace_id"] == "selected_workspace"
    skipped_by_id = {entry["workspace_id"]: entry for entry in payload["skipped_workspaces"]}
    assert skipped_by_id["manual_workspace"]["reasons"] == [
        "schedule_posture is manual; pass --include-manual to include it"
    ]
    assert skipped_by_id["blocked_workspace"]["reasons"] == [
        "failure_state is blocked: manual review required",
        "attempt_count 1 reached run_budget.max_attempts 1",
    ]
    assert skipped_by_id["missing_manifest_workspace"]["reasons"] == [
        "default_subject_manifest is missing or unresolved; scheduled runs need an explicit manifest"
    ]
    assert skipped_by_id["deprioritized_workspace"]["reasons"] == [
        "selection limit reached after saturation deprioritization"
    ]
    assert payload["planned_run_records"][0]["selection_explanation_id"] == payload["selection_explanation"]["explanation_id"]
    assert payload["planned_run_records"][0]["workspace_id"] == "selected_workspace"
    assert payload["planned_run_records"][-1]["workspace_id"] == "deprioritized_workspace"

    rendered = selector.render_text(payload)
    assert f"registry_path={registry_path}" in rendered
    assert "selected_count=1" in rendered
    assert "skipped_count=4" in rendered
    assert "selected[0].workspace_id=selected_workspace" in rendered
    assert "skipped[0].workspace_id=" in rendered
    assert "planned_run[0].workspace_id=selected_workspace" in rendered

    json_args = argparse.Namespace(**{**vars(args), "format": "json"})
    text_args = argparse.Namespace(**{**vars(args), "format": "text"})
    monkeypatch.setattr(selector, "parse_args", lambda: json_args)
    monkeypatch.setattr(selector, "build_selection_payload", lambda parsed: payload)
    assert selector.main() == 0
    assert json.loads(capsys.readouterr().out)["selected_count"] == 1

    monkeypatch.setattr(selector, "parse_args", lambda: text_args)
    assert selector.main() == 0
    assert "selected[0].workspace_id=selected_workspace" in capsys.readouterr().out

    monkeypatch.setattr(
        selector,
        "build_selection_payload",
        lambda parsed: (_ for _ in ()).throw(selector.SelectionError("boom")),
    )
    assert selector.main() == 1
    assert "Error: boom" in capsys.readouterr().err
