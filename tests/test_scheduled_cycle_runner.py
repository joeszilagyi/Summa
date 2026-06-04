from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tools.scripts import run_scheduled_topic_cycles as scheduled_runner


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "run_scheduled_topic_cycles.py"
WRAPPER = REPO_ROOT / "tools" / "scripts" / "Index_Run_Scheduled_Topic_Cycles.sh"


def write_workspace(tmp_path: Path, workspace_id: str) -> tuple[Path, Path]:
    workspace = tmp_path / "workspaces" / workspace_id
    manifest = workspace / ".indexer" / "subject_manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "subject-manifest.v1",
                "subject_id": workspace_id,
                "display_name": workspace_id.replace("_", " ").title(),
                "domain_pack": "general.v1",
                "scope_statement": "Scheduled runner fixture.",
                "languages": ["en"],
                "aliases": [workspace_id],
                "disambiguation_terms": ["fixture"],
                "excluded_senses": ["non-fixture"],
                "enabled_facets": ["sources"],
                "query_families": ["general_research"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return workspace, manifest


def planned_record(
    *,
    workspace_id: str,
    workspace: Path,
    manifest: Path,
    decision: str = "selected",
    max_attempts: int = 2,
    max_runtime_seconds: int | None = 60,
    skipped_reason: str | None = None,
) -> dict[str, object]:
    run_budget: dict[str, object] = {"max_attempts": max_attempts}
    if max_runtime_seconds is not None:
        run_budget["max_runtime_seconds"] = max_runtime_seconds
    return {
        "schema_version": "planned-run.v1",
        "planner_run_id": "planner-test",
        "planned_run_id": f"planner-test:{workspace_id}",
        "planned_at": "2026-06-03T12:00:00Z",
        "registry_path": str(workspace.parent / "registry.json"),
        "workspace_id": workspace_id,
        "decision": decision,
        "cadence_reason": "schedule_posture:scheduled",
        "skipped_reason": skipped_reason,
        "skipped_reasons": [skipped_reason] if skipped_reason else [],
        "run_budget": run_budget,
        "retry_policy": None,
        "failure_state": None,
        "workspace_root": str(workspace),
        "resolved_workspace_root": str(workspace),
        "default_subject_manifest": str(manifest),
        "resolved_default_subject_manifest": str(manifest),
    }


def write_selection(tmp_path: Path, records: list[dict[str, object]]) -> Path:
    selection = tmp_path / "selection.json"
    selection.write_text(
        json.dumps({"planned_run_records": records}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return selection


def write_fake_cycle_runner(tmp_path: Path, *, exit_code: int = 0) -> Path:
    script = tmp_path / "fake_cycle.py"
    script.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import argparse, json, pathlib, sys",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--run-dir', required=True)",
                "parser.add_argument('--run-id', required=True)",
                "parser.add_argument('--workspace')",
                "parser.add_argument('--subject')",
                "parser.add_argument('--db')",
                "parser.add_argument('--timestamp')",
                "parser.add_argument('--mode')",
                "parser.add_argument('--format')",
                "parser.add_argument('--candidate-batch-fixture')",
                "parser.add_argument('--execution-run-fixture')",
                "parser.add_argument('--build-next-feedback-plan', action='store_true')",
                "args = parser.parse_args()",
                "run_dir = pathlib.Path(args.run_dir)",
                "run_dir.mkdir(parents=True, exist_ok=True)",
                "payload = {'schema_version': 'topic-cycle-run.v1', 'run_id': args.run_id, 'status': 'completed'}",
                "(run_dir / 'topic-cycle-run.json').write_text(json.dumps(payload) + '\\n', encoding='utf-8')",
                "print(json.dumps(payload))",
                f"raise SystemExit({exit_code})",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return script


def run_scheduled(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_scheduled_runner_python_and_wrapper_help() -> None:
    proc = run_scheduled(["--help"])
    assert proc.returncode == 0, proc.stderr
    assert "planned-run" in proc.stdout

    wrapper = subprocess.run(
        ["bash", str(WRAPPER), "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert wrapper.returncode == 0, wrapper.stderr
    assert "--selection" in wrapper.stdout


def test_scheduled_runner_consumes_selection_runs_cycles_and_writes_ledgers(tmp_path: Path) -> None:
    workspace, manifest = write_workspace(tmp_path, "scheduled_subject")
    selection = write_selection(
        tmp_path,
        [planned_record(workspace_id="scheduled_subject", workspace=workspace, manifest=manifest)],
    )
    runner = write_fake_cycle_runner(tmp_path)
    run_dir = tmp_path / "scheduled-run"
    ledger_root = tmp_path / "ledgers"
    db_path = tmp_path / "canonical.sqlite"
    db_path.write_text("fixture\n", encoding="utf-8")

    proc = run_scheduled(
        [
            "--selection",
            str(selection),
            "--db",
            str(db_path),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "scheduled-run",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--cycle-runner",
            str(runner),
            "--ledger-root",
            str(ledger_root),
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads((run_dir / "scheduled-topic-cycles-run.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "scheduled-topic-cycles-run.v1"
    assert payload["status"] == "completed"
    assert payload["attempted_workspace_count"] == 1
    assert payload["completed_workspace_count"] == 1
    assert payload["workspace_results"][0]["outcome"] == "completed"
    ledger = ledger_root / "scheduled_subject.runtime-ledger.jsonl"
    lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [line["event_type"] for line in lines] == ["command_start", "command_end"]


def test_scheduled_runner_enforces_max_attempts(tmp_path: Path) -> None:
    workspace, manifest = write_workspace(tmp_path, "blocked_subject")
    selection = write_selection(
        tmp_path,
        [planned_record(workspace_id="blocked_subject", workspace=workspace, manifest=manifest, max_attempts=1)],
    )
    ledger_root = tmp_path / "ledgers"
    ledger = ledger_root / "blocked_subject.runtime-ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "schema_version": "runtime-ledger.v1",
                "event_id": "event-fixture",
                "run_id": "prior-run",
                "workspace_id": "blocked_subject",
                "event_type": "command_failure",
                "occurred_at": "2026-06-03T11:00:00Z",
                "failure": {"message": "prior failure"},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    runner = write_fake_cycle_runner(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    db_path.write_text("fixture\n", encoding="utf-8")

    proc = run_scheduled(
        [
            "--selection",
            str(selection),
            "--db",
            str(db_path),
            "--run-dir",
            str(tmp_path / "scheduled-run"),
            "--cycle-runner",
            str(runner),
            "--ledger-root",
            str(ledger_root),
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["attempted_workspace_count"] == 0
    assert payload["deferred_workspace_count"] == 1
    assert payload["workspace_results"][0]["outcome"] == "deferred"
    assert "max_attempts" in payload["workspace_results"][0]["failure_reason"]


def test_scheduled_runner_defers_saturated_workspace_from_selection(tmp_path: Path) -> None:
    workspace, manifest = write_workspace(tmp_path, "saturated_subject")
    record = planned_record(workspace_id="saturated_subject", workspace=workspace, manifest=manifest)
    record["saturation"] = {
        "schema_version": "topic-saturation.v1",
        "workspace_id": "saturated_subject",
        "subject_id": "saturated_subject",
        "policy_id": "topic-saturation.test",
        "state": "saturated",
        "scheduler_action": "halt",
        "reason_codes": ["consecutive_low_yield"],
        "recent_yield_summary": {"cycle_count": 2},
    }
    selection = write_selection(tmp_path, [record])
    runner = write_fake_cycle_runner(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    db_path.write_text("fixture\n", encoding="utf-8")

    proc = run_scheduled(
        [
            "--selection",
            str(selection),
            "--db",
            str(db_path),
            "--run-dir",
            str(tmp_path / "scheduled-run"),
            "--cycle-runner",
            str(runner),
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["attempted_workspace_count"] == 0
    assert payload["deferred_workspace_count"] == 1
    result = payload["workspace_results"][0]
    assert result["outcome"] == "deferred"
    assert "saturation policy deferred workspace" in result["failure_reason"]


def test_scheduled_runner_records_cycle_failure_through_runtime_ledger(tmp_path: Path) -> None:
    workspace, manifest = write_workspace(tmp_path, "failing_subject")
    selection = write_selection(
        tmp_path,
        [planned_record(workspace_id="failing_subject", workspace=workspace, manifest=manifest)],
    )
    runner = write_fake_cycle_runner(tmp_path, exit_code=2)
    ledger_root = tmp_path / "ledgers"
    db_path = tmp_path / "canonical.sqlite"
    db_path.write_text("fixture\n", encoding="utf-8")

    proc = run_scheduled(
        [
            "--selection",
            str(selection),
            "--db",
            str(db_path),
            "--run-dir",
            str(tmp_path / "scheduled-run"),
            "--cycle-runner",
            str(runner),
            "--ledger-root",
            str(ledger_root),
        ]
    )

    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["failed_workspace_count"] == 1
    ledger = ledger_root / "failing_subject.runtime-ledger.jsonl"
    lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [line["event_type"] for line in lines] == ["command_start", "command_failure"]


def test_scheduled_runner_enforces_max_runtime_seconds_with_injected_clock(tmp_path: Path) -> None:
    workspace, manifest = write_workspace(tmp_path, "slow_subject")
    selection = write_selection(
        tmp_path,
        [
            planned_record(
                workspace_id="slow_subject",
                workspace=workspace,
                manifest=manifest,
                max_runtime_seconds=1,
            )
        ],
    )
    runner = write_fake_cycle_runner(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    db_path.write_text("fixture\n", encoding="utf-8")
    args = scheduled_runner.parse_args(
        [
            "--selection",
            str(selection),
            "--db",
            str(db_path),
            "--run-dir",
            str(tmp_path / "scheduled-run"),
            "--cycle-runner",
            str(runner),
            "--ledger-root",
            str(tmp_path / "ledgers"),
        ]
    )

    clock_values = iter([10.0, 12.5])

    payload, exit_code = scheduled_runner.run_scheduled_cycles(
        args,
        cycle_invoker=lambda command: subprocess.CompletedProcess(command, 0, "{}", ""),
        monotonic=lambda: next(clock_values),
    )

    assert exit_code == 1
    assert payload["failed_workspace_count"] == 1
    result = payload["workspace_results"][0]
    assert result["outcome"] == "failed"
    assert result["runtime_consumed_seconds"] == 2.5
    assert "max_runtime_seconds 1" in result["failure_reason"]
    ledger = tmp_path / "ledgers" / "slow_subject.runtime-ledger.jsonl"
    lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [line["event_type"] for line in lines] == ["command_start", "command_failure"]


def test_scheduled_runner_has_no_direct_canonical_family_inserts() -> None:
    body = SCRIPT.read_text(encoding="utf-8")
    for needle in (
        "INSERT INTO work",
        "INSERT INTO source_claim",
        "INSERT INTO capture_event",
        "INSERT INTO extraction_record",
        "INSERT INTO authority_reconciliation",
        "INSERT INTO source_relationship",
    ):
        assert needle not in body
