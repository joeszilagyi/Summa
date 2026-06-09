from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "reconcile_scheduler_failure_state.py"
SELECTOR = REPO_ROOT / "tools" / "scripts" / "select_scheduled_workspaces.py"
VALIDATOR_PATH = (
    REPO_ROOT / "tools" / "validators" / "validate_scheduler_failure_state_reconciliation.py"
)
TOPIC_VALIDATOR_PATH = REPO_ROOT / "tools" / "validators" / "validate_topic_workspace_registry.py"
RUNTIME_LEDGER_PATH = REPO_ROOT / "tools" / "common" / "runtime_ledger.py"
SCHEDULER_RECONCILIATION_PATH = (
    REPO_ROOT / "tools" / "common" / "scheduler_failure_reconciliation.py"
)

for candidate in (REPO_ROOT, REPO_ROOT / "tools" / "validators", REPO_ROOT / "tools" / "common"):
    candidate_text = str(candidate)
    if candidate_text not in sys.path:
        sys.path.insert(0, candidate_text)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


validator = load_module(VALIDATOR_PATH, "scheduler_reconciliation_validator_for_tests")
topic_validator = load_module(TOPIC_VALIDATOR_PATH, "topic_workspace_registry_validator_for_tests")
runtime_ledger = load_module(RUNTIME_LEDGER_PATH, "runtime_ledger_for_tests")
scheduler_reconciliation = load_module(
    SCHEDULER_RECONCILIATION_PATH,
    "scheduler_failure_reconciliation_for_tests",
)


def run_reconciliation(
    args: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd or REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def run_selector(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SELECTOR), *args],
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
                "scope_statement": "Synthetic scheduler reconciliation fixture.",
                "languages": ["en"],
                "aliases": ["Synthetic fixture"],
                "disambiguation_terms": ["reconciliation"],
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
    manifest_path: Path,
    scheduler_policy: dict[str, object] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "workspace_id": workspace_id,
        "topic_label": workspace_id.replace("_", " ").title(),
        "workspace_root": str(workspace_root),
        "domain_pack": "general.v1",
        "lifecycle_state": "active",
        "schedule_posture": "scheduled",
        "workspace_policy_class": "private_local",
        "default_subject_manifest": str(manifest_path),
    }
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


def append_ledger_events(ledger_path: Path, events: list[dict[str, object]]) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def build_failure_event(
    *, workspace_id: str, run_id: str, occurred_at: str, message: str
) -> dict[str, object]:
    return runtime_ledger.build_event(
        workspace_id=workspace_id,
        run_id=run_id,
        event_type="command_failure",
        command="pytest-fixture",
        failure={"message": message},
        occurred_at=occurred_at,
    )


def build_success_event(*, workspace_id: str, run_id: str, occurred_at: str) -> dict[str, object]:
    return runtime_ledger.build_event(
        workspace_id=workspace_id,
        run_id=run_id,
        event_type="command_end",
        command="pytest-fixture",
        status="pass",
        occurred_at=occurred_at,
    )


def test_reconciliation_derives_retryable_recovered_and_blocked_states(tmp_path: Path) -> None:
    workspaces_root = tmp_path / "workspaces"
    ledger_root = tmp_path / "runtime" / "ledgers"
    retryable_root = workspaces_root / "retryable"
    recovered_root = workspaces_root / "recovered"
    blocked_root = workspaces_root / "blocked"
    for root in (retryable_root, recovered_root, blocked_root):
        root.mkdir(parents=True)

    retryable_manifest = write_manifest(retryable_root, subject_id="subject.retryable")
    recovered_manifest = write_manifest(recovered_root, subject_id="subject.recovered")
    blocked_manifest = write_manifest(blocked_root, subject_id="subject.blocked")

    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="retryable_workspace",
                workspace_root=retryable_root,
                manifest_path=retryable_manifest,
                scheduler_policy={
                    "run_budget": {"max_attempts": 4},
                    "retry_policy": {"max_retryable_failures": 2, "backoff_seconds": 600},
                },
            ),
            workspace_record(
                workspace_id="recovered_workspace",
                workspace_root=recovered_root,
                manifest_path=recovered_manifest,
                scheduler_policy={
                    "run_budget": {"max_attempts": 4},
                    "retry_policy": {"max_retryable_failures": 2, "backoff_seconds": 600},
                    "failure_state": {
                        "status": "retryable",
                        "attempt_count": 1,
                        "last_failure_at": "2026-06-01T00:00:00Z",
                        "next_retry_at": "2026-06-01T00:10:00Z",
                        "last_failure_reason": "stale prior failure",
                    },
                },
            ),
            workspace_record(
                workspace_id="blocked_workspace",
                workspace_root=blocked_root,
                manifest_path=blocked_manifest,
                scheduler_policy={
                    "run_budget": {"max_attempts": 3},
                    "retry_policy": {"max_retryable_failures": 2, "backoff_seconds": 300},
                },
            ),
        ],
    )

    append_ledger_events(
        ledger_root / "retryable_workspace.runtime-ledger.jsonl",
        [
            build_failure_event(
                workspace_id="retryable_workspace",
                run_id="retryable-run-1",
                occurred_at="2026-06-01T02:05:00Z",
                message="fixture timeout",
            )
        ],
    )
    append_ledger_events(
        ledger_root / "recovered_workspace.runtime-ledger.jsonl",
        [
            build_failure_event(
                workspace_id="recovered_workspace",
                run_id="recovered-run-1",
                occurred_at="2026-06-01T00:05:00Z",
                message="fixture timeout",
            ),
            build_success_event(
                workspace_id="recovered_workspace",
                run_id="recovered-run-2",
                occurred_at="2026-06-01T01:00:00Z",
            ),
        ],
    )
    append_ledger_events(
        ledger_root / "blocked_workspace.runtime-ledger.jsonl",
        [
            build_failure_event(
                workspace_id="blocked_workspace",
                run_id="blocked-run-1",
                occurred_at="2026-06-01T01:00:00Z",
                message="network budget exceeded",
            ),
            build_failure_event(
                workspace_id="blocked_workspace",
                run_id="blocked-run-2",
                occurred_at="2026-06-01T01:20:00Z",
                message="network budget exceeded",
            ),
            build_failure_event(
                workspace_id="blocked_workspace",
                run_id="blocked-run-3",
                occurred_at="2026-06-01T01:40:00Z",
                message="network budget exceeded",
            ),
        ],
    )

    output_json = tmp_path / "scheduler_reconciliation.json"
    output_registry = tmp_path / "topic_workspaces.reconciled.json"
    proc = run_reconciliation(
        [
            "--registry",
            str(registry_path),
            "--ledger-root",
            str(ledger_root),
            "--generated-at",
            "2026-06-01T02:10:00Z",
            "--output-json",
            str(output_json),
            "--output-registry",
            str(output_registry),
            "--format",
            "json",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["workspace_count"] == 3
    assert payload["changed_count"] == 3
    assert payload["unchanged_count"] == 0

    entries = {entry["workspace_id"]: entry for entry in payload["entries"]}
    retryable_entry = entries["retryable_workspace"]
    assert retryable_entry["recommendation"] == "replace"
    assert retryable_entry["derived_failure_state"] == {
        "status": "retryable",
        "attempt_count": 1,
        "last_failure_at": "2026-06-01T02:05:00Z",
        "next_retry_at": "2026-06-01T02:15:00Z",
        "last_failure_reason": "fixture timeout",
    }

    recovered_entry = entries["recovered_workspace"]
    assert recovered_entry["recommendation"] == "replace"
    assert recovered_entry["derived_failure_state"] == {
        "status": "healthy",
        "attempt_count": 0,
    }
    assert recovered_entry["latest_success_at"] == "2026-06-01T01:00:00Z"

    blocked_entry = entries["blocked_workspace"]
    assert blocked_entry["recommendation"] == "replace"
    assert blocked_entry["derived_failure_state"] == {
        "status": "blocked",
        "attempt_count": 3,
        "last_failure_at": "2026-06-01T01:40:00Z",
        "next_retry_at": "2026-06-01T01:45:00Z",
        "last_failure_reason": "network budget exceeded",
        "blocked_reason": (
            "attempt_count 3 reached run_budget.max_attempts 3; "
            "retryable failure count 3 exceeded retry_policy.max_retryable_failures 2"
        ),
    }

    report, exit_code = validator.validate_scheduler_failure_state_reconciliation(output_json)
    assert exit_code == validator.EXIT_PASS, report
    registry_report, registry_exit = topic_validator.validate_topic_workspace_registry(
        output_registry
    )
    assert registry_exit == topic_validator.EXIT_PASS, registry_report

    selector_proc = run_selector(
        [
            "--registry",
            str(output_registry),
            "--planned-at",
            "2026-06-01T02:10:00Z",
            "--format",
            "json",
        ]
    )
    assert selector_proc.returncode == 0, selector_proc.stdout + selector_proc.stderr
    selector_payload = json.loads(selector_proc.stdout)
    assert [entry["workspace_id"] for entry in selector_payload["selected_workspaces"]] == [
        "recovered_workspace"
    ]
    skipped = {entry["workspace_id"]: entry for entry in selector_payload["skipped_workspaces"]}
    assert skipped["retryable_workspace"]["reasons"] == [
        "retry backoff active until 2026-06-01T02:15:00Z"
    ]
    assert skipped["blocked_workspace"]["reasons"] == [
        "failure_state is blocked: attempt_count 3 reached run_budget.max_attempts 3; retryable failure count 3 exceeded retry_policy.max_retryable_failures 2",
        "attempt_count 3 reached run_budget.max_attempts 3",
    ]


def test_read_runtime_ledger_rejects_malformed_nonterminal_json_after_real_failures(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "runtime" / "ledgers" / "workspace-a.runtime-ledger.jsonl"
    append_ledger_events(
        ledger_path,
        [
            build_failure_event(
                workspace_id="workspace-a",
                run_id="failure-run-1",
                occurred_at="2026-06-01T02:05:00Z",
                message="fixture timeout",
            ),
            build_success_event(
                workspace_id="workspace-a",
                run_id="success-run-2",
                occurred_at="2026-06-01T03:05:00Z",
            ),
        ],
    )
    ledger_path.write_text(
        ledger_path.read_text(encoding="utf-8")
        + '{"schema_version": "runtime-ledger.v1", "event_id": "broken",\n'
        + json.dumps(
            build_success_event(
                workspace_id="workspace-a",
                run_id="success-run-3",
                occurred_at="2026-06-01T04:05:00Z",
            ),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        scheduler_reconciliation.SchedulerFailureReconciliationError,
        match="could not read runtime ledger",
    ):
        scheduler_reconciliation.read_runtime_ledger(ledger_path, workspace_id="workspace-a")


def test_runtime_ledger_load_events_tolerates_truncated_terminal_json_without_newline(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "runtime" / "ledgers" / "workspace-a.runtime-ledger.jsonl"
    first_event = build_success_event(
        workspace_id="workspace-a",
        run_id="success-run-1",
        occurred_at="2026-06-01T01:00:00Z",
    )
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps(first_event, ensure_ascii=False, sort_keys=True)
        + "\n"
        + '{"bad": "json_fragment',
        encoding="utf-8",
    )

    events = runtime_ledger.load_events(ledger_path)
    assert events == [first_event]


def test_runtime_ledger_append_event_does_not_call_fsync(tmp_path: Path, monkeypatch) -> None:
    ledger_path = tmp_path / "runtime" / "ledgers" / "no-fsync.runtime-ledger.jsonl"
    calls: list[int] = []

    monkeypatch.setattr(runtime_ledger.os, "fsync", lambda _: calls.append(0))
    event = runtime_ledger.build_event(
        workspace_id="workspace-a",
        run_id="append-no-fsync",
        event_type="command_start",
        command="pytest-fixture",
    )
    runtime_ledger.append_event(ledger_path, event)

    assert calls == []
    events = runtime_ledger.load_events(ledger_path)
    assert len(events) == 1


def test_runtime_ledger_append_event_updates_metadata_sidecar(tmp_path: Path) -> None:
    ledger_path = tmp_path / "runtime" / "ledgers" / "metadata.runtime-ledger.jsonl"
    first = runtime_ledger.build_event(
        workspace_id="workspace-a",
        run_id="append-metadata-1",
        event_type="command_start",
        command="pytest-fixture",
    )
    second = runtime_ledger.build_event(
        workspace_id="workspace-a",
        run_id="append-metadata-2",
        event_type="command_end",
        command="pytest-fixture",
        status="pass",
    )

    runtime_ledger.append_event(ledger_path, first)
    runtime_ledger.append_event(ledger_path, second)

    metadata_path = runtime_ledger.ledger_metadata_path(ledger_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert metadata["schema_version"] == runtime_ledger.LEDGER_METADATA_SCHEMA_VERSION
    assert metadata["line_count"] == 2


def test_reconciliation_keeps_current_state_without_terminal_runs(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, subject_id="subject.keep")
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="keep_workspace",
                workspace_root=workspace_root,
                manifest_path=manifest_path,
                scheduler_policy={
                    "run_budget": {"max_attempts": 2},
                    "retry_policy": {"max_retryable_failures": 1, "backoff_seconds": 60},
                    "failure_state": {
                        "status": "blocked",
                        "attempt_count": 2,
                        "last_failure_at": "2026-06-01T00:00:00Z",
                        "last_failure_reason": "manual block carry-forward",
                        "blocked_reason": "operator review required",
                    },
                },
            )
        ],
    )

    ledger_root = tmp_path / "runtime" / "ledgers"
    append_ledger_events(
        ledger_root / "keep_workspace.runtime-ledger.jsonl",
        [
            runtime_ledger.build_event(
                workspace_id="keep_workspace",
                run_id="keep-run-1",
                event_type="command_start",
                command="pytest-fixture",
                occurred_at="2026-06-01T00:05:00Z",
            )
        ],
    )

    proc = run_reconciliation(
        [
            "--registry",
            str(registry_path),
            "--ledger-root",
            str(ledger_root),
            "--generated-at",
            "2026-06-01T00:10:00Z",
            "--format",
            "json",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["changed_count"] == 0
    entry = payload["entries"][0]
    assert entry["recommendation"] == "keep"
    assert entry["reasons"] == ["no terminal runtime-ledger outcomes found"]
    assert entry["derived_failure_state"] == entry["registry_failure_state"]


def test_reconciliation_resolves_ledger_root_relative_to_cwd(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, subject_id="relative.subject")
    registry_path = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="relative_workspace",
                workspace_root=workspace_root,
                manifest_path=manifest_path,
            )
        ],
    )

    cwd = tmp_path / "runner-cwd"
    ledger_root = cwd / "runtime" / "ledgers"
    append_ledger_events(
        ledger_root / "relative_workspace.runtime-ledger.jsonl",
        [
            build_failure_event(
                workspace_id="relative_workspace",
                run_id="relative-run-1",
                occurred_at="2026-06-01T03:05:00Z",
                message="fixture timeout",
            )
        ],
    )

    output_json = tmp_path / "relative-reconciliation.json"
    proc = run_reconciliation(
        [
            "--registry",
            str(registry_path),
            "--ledger-root",
            "runtime/ledgers",
            "--generated-at",
            "2026-06-01T03:10:00Z",
            "--output-json",
            str(output_json),
            "--format",
            "json",
        ],
        cwd=cwd,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["workspace_count"] == 1
    entry = payload["entries"][0]
    assert entry["workspace_id"] == "relative_workspace"
    assert entry["latest_failure_at"] == "2026-06-01T03:05:00Z"
