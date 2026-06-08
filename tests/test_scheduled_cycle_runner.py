from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.common import scheduler_failure_reconciliation
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
                "parser.add_argument('--skip-workspace-lock', action='store_true')",
                "args = parser.parse_args()",
                "run_dir = pathlib.Path(args.run_dir)",
                "run_dir.mkdir(parents=True, exist_ok=True)",
                "payload = {'schema_version': 'topic-cycle-run.v1', 'run_id': args.run_id, 'cycle_event_id': 'cycle:' + args.run_id, 'status': 'completed'}",
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


def run_scheduled_in_dir(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def test_scheduled_runner_rejects_invalid_child_manifest_json(tmp_path: Path) -> None:
    child_manifest_path = tmp_path / "topic-cycle-run.json"
    child_manifest_path.write_text(
        '{"schema_version":"topic-cycle-run.v1","status":"completed","status":"failed"}',
        encoding="utf-8",
    )

    with pytest.raises(scheduled_runner.ScheduledCycleError, match="child manifest"):
        scheduled_runner.resolve_child_manifest(child_manifest_path=child_manifest_path, proc=None)


def test_scheduled_runner_write_json_uses_atomic_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "scheduled-topic-cycles-run.json"
    payload = {"schema_version": "scheduled-topic-cycles-run.v1", "status": "completed"}
    calls: list[tuple[Path, dict[str, object]]] = []

    def fake_atomic_write_json(target: Path, body: dict[str, object]) -> None:
        calls.append((target, dict(body)))
        target.write_text(
            json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    monkeypatch.setattr(scheduled_runner, "atomic_write_json", fake_atomic_write_json)

    scheduled_runner.write_json(path, payload)

    assert calls == [(path, payload)]
    assert json.loads(path.read_text(encoding="utf-8")) == payload


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
    assert not Path(payload["selection_artifact"]["path"]).is_absolute()
    assert not Path(payload["workspace_results"][0]["ledger_path"]).is_absolute()
    assert not Path(payload["workspace_results"][0]["cycle_manifest_path"]).is_absolute()
    assert not Path(payload["workspace_results"][0]["scheduler_failure_state_record"]).is_absolute()
    assert payload["workspace_results"][0]["cycle_event_id"].startswith(
        "cycle:scheduled-run.scheduled_subject.1."
    )
    ledger = ledger_root / "scheduled_subject.runtime-ledger.jsonl"
    lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [line["event_type"] for line in lines] == ["command_start", "command_end"]


def test_scheduled_runner_manifest_paths_are_run_relative_even_outside_cwd(tmp_path: Path) -> None:
    workspace, manifest = write_workspace(tmp_path, "scheduled_subject")
    write_selection(
        tmp_path,
        [planned_record(workspace_id="scheduled_subject", workspace=workspace, manifest=manifest)],
    )
    runner = write_fake_cycle_runner(tmp_path)
    run_dir = tmp_path / "scheduled-root" / "run"
    ledger_root = tmp_path / "ledger-storage"
    db_path = tmp_path / "canonical.sqlite"
    db_path.write_text("fixture\n", encoding="utf-8")

    relative_selection = tmp_path.relative_to(tmp_path).joinpath("selection.json").as_posix()
    relative_run_dir = tmp_path.relative_to(tmp_path).joinpath("scheduled-root", "run").as_posix()
    proc = run_scheduled_in_dir(
        [
            "--selection",
            relative_selection,
            "--db",
            str(db_path),
            "--run-dir",
            relative_run_dir,
            "--run-id",
            "scheduled-run",
            "--timestamp",
            "2026-06-03T12:00:00Z",
            "--cycle-runner",
            str(runner),
            "--ledger-root",
            str(ledger_root),
        ],
        cwd=tmp_path,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    output_manifest = run_dir / "scheduled-topic-cycles-run.json"
    payload = json.loads(output_manifest.read_text(encoding="utf-8"))
    assert payload["selection_artifact"]["path"] == "../../selection.json"
    assert not Path(payload["workspace_results"][0]["ledger_path"]).is_absolute()
    assert not Path(payload["workspace_results"][0]["cycle_manifest_path"]).is_absolute()
    assert not Path(payload["workspace_results"][0]["scheduler_failure_state_record"]).is_absolute()


def test_scheduled_runner_uses_fresh_timestamp_per_child_cycle(tmp_path: Path, monkeypatch) -> None:
    first_workspace, first_manifest = write_workspace(tmp_path, "first_subject")
    second_workspace, second_manifest = write_workspace(tmp_path, "second_subject")
    selection = write_selection(
        tmp_path,
        [
            planned_record(
                workspace_id="first_subject",
                workspace=first_workspace,
                manifest=first_manifest,
            ),
            planned_record(
                workspace_id="second_subject",
                workspace=second_workspace,
                manifest=second_manifest,
            ),
        ],
    )
    runner = write_fake_cycle_runner(tmp_path)
    db_path = tmp_path / "canonical.sqlite"
    db_path.write_text("fixture\n", encoding="utf-8")
    run_dir = tmp_path / "scheduled-run"
    ledger_root = tmp_path / "ledgers"
    timestamps = iter(
        [
            "2026-06-03T12:00:00Z",
            "2026-06-03T12:00:50Z",
        ]
    )
    monkeypatch.setattr(scheduled_runner, "utc_now", lambda: next(timestamps))
    args = scheduled_runner.parse_args(
        [
            "--selection",
            str(selection),
            "--db",
            str(db_path),
            "--run-dir",
            str(run_dir),
            "--run-id",
            "scheduled-run",
            "--cycle-runner",
            str(runner),
            "--ledger-root",
            str(ledger_root),
        ]
    )
    captured_commands: list[list[str]] = []

    def invoker(command: list[str]) -> subprocess.CompletedProcess[str]:
        captured_commands.append(command)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    payload, exit_code = scheduled_runner.run_scheduled_cycles(args, cycle_invoker=invoker)

    assert exit_code == 0
    assert payload["started_at"] == "2026-06-03T12:00:00Z"
    assert payload["ended_at"] == "2026-06-03T12:00:50Z"
    timestamps_by_workspace = {}
    for command in captured_commands:
        workspace = command[command.index("--workspace") + 1]
        timestamps_by_workspace[workspace] = command[command.index("--timestamp") + 1]

    assert set(timestamps_by_workspace.values()) == {
        "2026-06-03T12:00:10Z",
        "2026-06-03T12:00:30Z",
    }
    assert (
        captured_commands[0][captured_commands[0].index("--timestamp") + 1] != payload["started_at"]
    )
    first_ledger_lines = [
        json.loads(line)
        for line in (ledger_root / "first_subject.runtime-ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    second_ledger_lines = [
        json.loads(line)
        for line in (ledger_root / "second_subject.runtime-ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [line["event_type"] for line in first_ledger_lines] == ["command_start", "command_end"]
    assert [line["event_type"] for line in second_ledger_lines] == ["command_start", "command_end"]
    assert [line["occurred_at"] for line in first_ledger_lines] == [
        "2026-06-03T12:00:10Z",
        "2026-06-03T12:00:20Z",
    ]
    assert [line["occurred_at"] for line in second_ledger_lines] == [
        "2026-06-03T12:00:30Z",
        "2026-06-03T12:00:40Z",
    ]


def test_scheduled_runner_rejects_workspace_id_path_traversal(tmp_path: Path) -> None:
    workspace, manifest = write_workspace(tmp_path, "safe_subject")
    selection = write_selection(
        tmp_path,
        [
            planned_record(
                workspace_id="../escape",
                workspace=workspace,
                manifest=manifest,
            )
        ],
    )
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
            "--timestamp",
            "2026-06-03T12:00:00Z",
        ]
    )

    assert proc.returncode == scheduled_runner.EXIT_VALIDATION_FAILED
    assert "workspace_id must match the workspace identifier pattern" in proc.stderr


def test_normalize_timestamp_preserves_utc_and_rejects_invalid_values() -> None:
    assert scheduled_runner.normalize_timestamp("2026-06-03T12:00:00Z") == "2026-06-03T12:00:00Z"
    assert (
        scheduled_runner.normalize_timestamp("2026-06-03T12:00:00-07:00") == "2026-06-03T19:00:00Z"
    )

    with pytest.raises(scheduled_runner.ScheduledCycleError, match="RFC3339"):
        scheduled_runner.normalize_timestamp("not-a-timestamp")


def test_scheduled_runner_enforces_max_attempts(tmp_path: Path) -> None:
    workspace, manifest = write_workspace(tmp_path, "blocked_subject")
    selection = write_selection(
        tmp_path,
        [
            planned_record(
                workspace_id="blocked_subject",
                workspace=workspace,
                manifest=manifest,
                max_attempts=1,
            )
        ],
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


def test_read_runtime_ledger_ignores_truncated_final_line(tmp_path: Path) -> None:
    ledger = tmp_path / "ledgers" / "workspace.runtime-ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "schema_version": "runtime-ledger.v1",
                        "event_id": "event-1",
                        "run_id": "run-1",
                        "workspace_id": "workspace",
                        "event_type": "command_end",
                        "occurred_at": "2026-06-03T12:00:00Z",
                        "status": "ok",
                    },
                    sort_keys=True,
                ),
                '{"schema_version": "runtime-ledger.v1", "event_id": "event-2"',
            ]
        ),
        encoding="utf-8",
    )

    events = scheduler_failure_reconciliation.read_runtime_ledger(ledger, workspace_id="workspace")

    assert [event["event_id"] for event in events] == ["event-1"]


def test_scheduled_runner_defers_saturated_workspace_from_selection(tmp_path: Path) -> None:
    workspace, manifest = write_workspace(tmp_path, "saturated_subject")
    record = planned_record(
        workspace_id="saturated_subject", workspace=workspace, manifest=manifest
    )
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

    assert proc.returncode == scheduled_runner.EXIT_INTEGRITY_FAILURE
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

    assert exit_code == scheduled_runner.EXIT_INTEGRITY_FAILURE
    assert payload["failed_workspace_count"] == 1
    result = payload["workspace_results"][0]
    assert result["outcome"] == "failed"
    assert result["runtime_consumed_seconds"] == 2.5
    assert "max_runtime_seconds 1" in result["failure_reason"]
    ledger = tmp_path / "ledgers" / "slow_subject.runtime-ledger.jsonl"
    lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert [line["event_type"] for line in lines] == ["command_start", "command_failure"]


def test_scheduled_runner_passes_runtime_budget_to_child_invoker(tmp_path: Path) -> None:
    workspace, manifest = write_workspace(tmp_path, "timed_subject")
    selection = write_selection(
        tmp_path,
        [
            planned_record(
                workspace_id="timed_subject",
                workspace=workspace,
                manifest=manifest,
                max_runtime_seconds=11,
            )
        ],
    )
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
            "--ledger-root",
            str(tmp_path / "ledgers"),
        ]
    )

    observed_timeouts: list[float | None] = []
    commands: list[list[str]] = []

    def invoker(command: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        observed_timeouts.append(timeout)
        run_dir = Path(command[command.index("--run-dir") + 1])
        run_dir.mkdir(parents=True, exist_ok=True)
        run_id = command[command.index("--run-id") + 1]
        (run_dir / "topic-cycle-run.json").write_text(
            json.dumps(
                {
                    "schema_version": "topic-cycle-run.v1",
                    "run_id": run_id,
                    "cycle_event_id": f"cycle:{run_id}",
                    "status": "completed",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "{}", "")

    payload, exit_code = scheduled_runner.run_scheduled_cycles(args, cycle_invoker=invoker)

    assert exit_code == scheduled_runner.EXIT_SUCCESS
    assert observed_timeouts == [11.0]
    assert commands[0][commands[0].index("--format") + 1] == "text"
    assert payload["workspace_results"][0]["outcome"] == "completed"


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


def test_scheduled_runner_stable_failure_reason_contract_fields(tmp_path: Path) -> None:
    selection = write_selection(
        tmp_path,
        [
            {
                "schema_version": "planned-run.v1",
                "planner_run_id": "planner-test",
                "planned_run_id": "planner-test:bad_subject",
                "planned_at": "2026-06-03T12:00:00Z",
                "registry_path": str(tmp_path / "registry.json"),
                "workspace_id": "bad_subject",
                "decision": "selected",
                "cadence_reason": "schedule_posture:scheduled",
                "skipped_reason": None,
                "skipped_reasons": [],
                "run_budget": {"max_attempts": 2},
                "retry_policy": None,
                "failure_state": None,
                "workspace_root": str(tmp_path / "workspaces" / "bad_subject"),
                "resolved_workspace_root": str(tmp_path / "workspaces" / "bad_subject"),
                "default_subject_manifest": str(tmp_path / "bad_manifest.json"),
            }
        ],
    )
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
            "--run-id",
            "scheduled-run",
            "--timestamp",
            "2026-06-03T12:00:00Z",
        ]
    )

    assert proc.returncode == scheduled_runner.EXIT_VALIDATION_FAILED
    assert proc.stdout == ""
    assert (
        "planned-run record is missing required field: resolved_default_subject_manifest"
        in proc.stderr
    )


def test_scheduled_runner_generates_collision_safe_child_run_ids(
    tmp_path: Path, monkeypatch
) -> None:
    workspace, manifest = write_workspace(tmp_path, "duplicate_subject")
    selection = write_selection(
        tmp_path,
        [
            planned_record(
                workspace_id="duplicate_subject",
                workspace=workspace,
                manifest=manifest,
                max_attempts=3,
            ),
            planned_record(
                workspace_id="duplicate_subject",
                workspace=workspace,
                manifest=manifest,
                max_attempts=3,
            ),
        ],
    )
    runner = write_fake_cycle_runner(tmp_path, exit_code=0)
    db_path = tmp_path / "canonical.sqlite"
    db_path.write_text("fixture\n", encoding="utf-8")

    invocations: list[str] = []
    commands: list[list[str]] = []
    monkeypatch.setattr(
        scheduled_runner,
        "_next_workspace_token",
        lambda: "same-token" if len(invocations) == 0 else f"same-token-{len(invocations)}",
    )
    monkeypatch.setattr(
        scheduled_runner,
        "terminal_attempt_count",
        lambda *args, **kwargs: 0,
    )

    def invoker(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        run_id = command[command.index("--run-id") + 1]
        invocations.append(run_id)
        run_dir = Path(command[command.index("--run-dir") + 1])
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "topic-cycle-run.json").write_text(
            json.dumps(
                {
                    "schema_version": "topic-cycle-run.v1",
                    "run_id": run_id,
                    "cycle_event_id": f"cycle:{run_id}",
                    "status": "completed",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "{}", "")

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

    payload, exit_code = scheduled_runner.run_scheduled_cycles(args, cycle_invoker=invoker)

    assert exit_code == scheduled_runner.EXIT_SUCCESS
    assert len(invocations) == 2
    assert all("--skip-workspace-lock" in command for command in commands)
    assert (
        payload["workspace_results"][0]["cycle_run_id"]
        != payload["workspace_results"][1]["cycle_run_id"]
    )
    assert payload["workspace_results"][0]["cycle_run_id"].endswith("same-token")
    assert payload["workspace_results"][1]["cycle_run_id"].endswith("same-token-1")


def test_scheduled_runner_does_not_double_run_locked_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, manifest = write_workspace(tmp_path, "locked_subject")
    selection = write_selection(
        tmp_path,
        [
            planned_record(
                workspace_id="locked_subject",
                workspace=workspace,
                manifest=manifest,
                max_attempts=3,
            ),
            planned_record(
                workspace_id="locked_subject",
                workspace=workspace,
                manifest=manifest,
                max_attempts=3,
            ),
        ],
    )
    db_path = tmp_path / "canonical.sqlite"
    db_path.write_text("fixture\n", encoding="utf-8")

    @contextlib.contextmanager
    def fake_lock(*_args, **_kwargs):
        call_count = fake_lock.__dict__.setdefault("calls", 0)
        if call_count:
            raise scheduled_runner.WorkspaceLockError("workspace lock is already held")
        fake_lock.__dict__["calls"] = call_count + 1
        yield Path("/tmp/scheduled-topic-lock.test")

    invocations: list[list[str]] = []

    def invoker(command: list[str]) -> subprocess.CompletedProcess[str]:
        invocations.append(command)
        assert "--skip-workspace-lock" in command
        run_dir = Path(command[command.index("--run-dir") + 1])
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "topic-cycle-run.json").write_text(
            json.dumps(
                {
                    "schema_version": "topic-cycle-run.v1",
                    "run_id": command[command.index("--run-id") + 1],
                    "cycle_event_id": "cycle-ok",
                    "status": "completed",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(scheduled_runner, "terminal_attempt_count", lambda *args, **kwargs: 0)
    monkeypatch.setattr(scheduled_runner, "acquire_workspace_lock", fake_lock)
    monkeypatch.setattr(scheduled_runner, "_next_workspace_token", lambda: "token-a")

    args = scheduled_runner.parse_args(
        [
            "--selection",
            str(selection),
            "--db",
            str(db_path),
            "--run-dir",
            str(tmp_path / "scheduled-run"),
            "--ledger-root",
            str(tmp_path / "ledgers"),
        ]
    )

    payload, exit_code = scheduled_runner.run_scheduled_cycles(args, cycle_invoker=invoker)

    assert exit_code == scheduled_runner.EXIT_TRANSIENT_ACQUISITION_FAILURE
    assert payload["attempted_workspace_count"] == 1
    assert payload["deferred_workspace_count"] == 1
    assert payload["workspace_results"][0]["outcome"] == "completed"
    assert payload["workspace_results"][1]["outcome"] == "deferred"
    assert payload["workspace_results"][1]["failure_reason_code"] == "workspace_lock_unavailable"
    assert len(invocations) == 1


def test_scheduled_runner_stdout_stderr_contract_validation_and_help(tmp_path: Path) -> None:
    workspace, manifest = write_workspace(tmp_path, "subject")
    selection = write_selection(
        tmp_path, [planned_record(workspace_id="subject", workspace=workspace, manifest=manifest)]
    )
    db_path = tmp_path / "canonical.sqlite"
    db_path.write_text("fixture\n", encoding="utf-8")
    _ = write_fake_cycle_runner(tmp_path)
    missing_run_file = tmp_path / "missing.json"

    help_proc = run_scheduled(["--help"])
    assert help_proc.returncode == 0
    assert "planned-run" in help_proc.stdout
    assert help_proc.stderr == ""

    usage_proc = run_scheduled(
        [
            "--selection",
            str(selection),
            "--run-dir",
            str(tmp_path / "scheduled-run"),
        ]
    )
    assert usage_proc.returncode == scheduled_runner.EXIT_USAGE_ERROR
    assert usage_proc.stdout == ""
    assert "usage:" in usage_proc.stderr.lower()

    validation_proc = run_scheduled(
        [
            "--selection",
            str(missing_run_file),
            "--db",
            str(db_path),
            "--run-dir",
            str(tmp_path / "scheduled-run"),
        ]
    )
    assert validation_proc.returncode == scheduled_runner.EXIT_VALIDATION_FAILED
    assert validation_proc.stdout == ""
    assert "Traceback" not in validation_proc.stderr


def test_scheduled_runner_partial_child_output_exit_code(tmp_path: Path) -> None:
    workspace, manifest = write_workspace(tmp_path, "partial_subject")
    selection = write_selection(
        tmp_path,
        [planned_record(workspace_id="partial_subject", workspace=workspace, manifest=manifest)],
    )
    db_path = tmp_path / "canonical.sqlite"
    db_path.write_text("fixture\n", encoding="utf-8")

    def invoker(command: list[str]) -> subprocess.CompletedProcess[str]:
        run_dir = Path(command[command.index("--run-dir") + 1])
        run_dir.mkdir(parents=True, exist_ok=True)
        run_id = command[command.index("--run-id") + 1]
        (run_dir / "topic-cycle-run.json").write_text(
            json.dumps(
                {
                    "schema_version": "topic-cycle-run.v1",
                    "run_id": run_id,
                    "cycle_event_id": f"cycle:{run_id}",
                    "status": "partial",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 5, "", "partial failure")

    args = scheduled_runner.parse_args(
        [
            "--selection",
            str(selection),
            "--db",
            str(db_path),
            "--run-dir",
            str(tmp_path / "scheduled-run"),
            "--ledger-root",
            str(tmp_path / "ledgers"),
        ]
    )

    payload, exit_code = scheduled_runner.run_scheduled_cycles(args, cycle_invoker=invoker)

    assert exit_code == scheduled_runner.EXIT_PARTIAL_OUTPUT
    result = payload["workspace_results"][0]
    assert result["outcome"] == "failed"
    assert result["failure_reason_code"] == "topic_cycle_partial_output"
    assert result["error_code"] == "topic_cycle_partial_output"
    assert result["stage"] == "child_cycle_exec"
    assert result["recoverability"] == "retryable"


def test_scheduled_runner_truncates_large_child_error_output(tmp_path: Path) -> None:
    workspace, manifest = write_workspace(tmp_path, "partial_subject")
    selection = write_selection(
        tmp_path,
        [planned_record(workspace_id="partial_subject", workspace=workspace, manifest=manifest)],
    )
    db_path = tmp_path / "canonical.sqlite"
    db_path.write_text("fixture\n", encoding="utf-8")

    def invoker(command: list[str]) -> subprocess.CompletedProcess[str]:
        run_dir = Path(command[command.index("--run-dir") + 1])
        run_dir.mkdir(parents=True, exist_ok=True)
        run_id = command[command.index("--run-id") + 1]
        (run_dir / "topic-cycle-run.json").write_text(
            json.dumps(
                {
                    "schema_version": "topic-cycle-run.v1",
                    "run_id": run_id,
                    "cycle_event_id": f"cycle:{run_id}",
                    "status": "partial",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 5, "", "x" * 5000 + " end-marker")

    args = scheduled_runner.parse_args(
        [
            "--selection",
            str(selection),
            "--db",
            str(db_path),
            "--run-dir",
            str(tmp_path / "scheduled-run"),
            "--ledger-root",
            str(tmp_path / "ledgers"),
        ]
    )

    payload, exit_code = scheduled_runner.run_scheduled_cycles(args, cycle_invoker=invoker)

    assert exit_code == scheduled_runner.EXIT_PARTIAL_OUTPUT
    result = payload["workspace_results"][0]
    assert result["failure_reason_code"] == "topic_cycle_partial_output"
    assert "truncated" in result["failure_reason"]
    assert len(result["failure_reason"]) < 2100
    assert result["failure_reason"].endswith("chars omitted)")


def test_scheduled_runner_uses_child_manifest_file_before_stdout_when_available(
    tmp_path: Path,
) -> None:
    workspace, manifest = write_workspace(tmp_path, "stdout_subject")
    selection = write_selection(
        tmp_path,
        [planned_record(workspace_id="stdout_subject", workspace=workspace, manifest=manifest)],
    )
    db_path = tmp_path / "canonical.sqlite"
    db_path.write_text("fixture\n", encoding="utf-8")

    def invoker(command: list[str]) -> subprocess.CompletedProcess[str]:
        run_dir = Path(command[command.index("--run-dir") + 1])
        run_dir.mkdir(parents=True, exist_ok=True)
        run_id = command[command.index("--run-id") + 1]
        payload = {
            "schema_version": "topic-cycle-run.v1",
            "run_id": run_id,
            "cycle_event_id": f"cycle:{run_id}",
            "status": "completed",
        }
        (run_dir / "topic-cycle-run.json").write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "not-json", "")

    args = scheduled_runner.parse_args(
        [
            "--selection",
            str(selection),
            "--db",
            str(db_path),
            "--run-dir",
            str(tmp_path / "scheduled-run"),
            "--ledger-root",
            str(tmp_path / "ledgers"),
        ]
    )
    payload, exit_code = scheduled_runner.run_scheduled_cycles(args, cycle_invoker=invoker)

    assert exit_code == scheduled_runner.EXIT_SUCCESS, payload["warnings"] if payload else ""
    result = payload["workspace_results"][0]
    assert result["outcome"] == "completed"
    assert result["cycle_event_id"] == f"cycle:{result['cycle_run_id']}"
    assert result["failure_reason"] is None
