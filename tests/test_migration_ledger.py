from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_COMMON = REPO_ROOT / "tools" / "common"
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "validators" / "migration_ledger"

if str(TOOLS_COMMON) not in sys.path:
    sys.path.insert(0, str(TOOLS_COMMON))
if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

import migration_ledger

spec = importlib.util.spec_from_file_location("migration_ledger_validator_for_tests", VALIDATORS_DIR / "validate_migration_ledger.py")
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)


def load_fixture(name: str) -> Path:
    return FIXTURE_ROOT / name / "inputs" / "migration_ledger.jsonl"


def test_append_event_writes_valid_fixture_like_ledger(tmp_path: Path) -> None:
    ledger_path = tmp_path / "runtime" / "ledgers" / "fixture_workspace.migration-ledger.jsonl"
    first = migration_ledger.build_event(
        workspace_id="fixture_workspace",
        migration_id="mig:source-db.schema.v5",
        migration_type="schema_migration",
        subject_ref="sqlite/source_db",
        tool_surface="tool.sqlite_migrate_schema",
        tool_version="2026.06.02",
        input_version="source-db.schema.v4",
        output_version="source-db.schema.v5",
        input_artifact_refs=[{"role": "sqlite_schema", "path": "dbs/index/source.db", "version": "source-db.schema.v4"}],
        output_artifact_refs=[{"role": "sqlite_schema", "path": "dbs/index/source.db", "version": "source-db.schema.v5"}],
        occurred_at="2026-06-02T12:00:00Z",
        event_id="mle:schema.001",
        note="Promote new source claim typing constraints.",
    )
    second = migration_ledger.build_event(
        workspace_id="fixture_workspace",
        migration_id="mig:source-db.rollback.v4",
        migration_type="rollback_reference",
        subject_ref="sqlite/source_db",
        tool_surface="tool.topic_backup_drill_py",
        tool_version="2026.06.02",
        input_version="source-db.schema.v5",
        output_version="source-db.schema.v4",
        input_artifact_refs=[{"role": "sqlite_schema", "path": "dbs/index/source.db", "version": "source-db.schema.v5"}],
        output_artifact_refs=[{"role": "sqlite_schema", "path": "dbs/index/source.db", "version": "source-db.schema.v4"}],
        backup_ref="runtime/backups/crown_jewels/fixture_workspace/20260602T120900Z/files/repo/dbs/index/source.db",
        snapshot_ref="runtime/backups/crown_jewels/fixture_workspace/20260602T120900Z/manifest.json",
        rollback_of_event_id="mle:schema.001",
        occurred_at="2026-06-02T12:10:00Z",
        event_id="mle:rollback.001",
        note="Reference the restore path used to return to the prior schema.",
    )

    migration_ledger.append_event(ledger_path, first)
    migration_ledger.append_event(ledger_path, second)

    loaded = migration_ledger.load_events(ledger_path)
    result, exit_code = validator.validate_migration_ledger(ledger_path)

    assert len(loaded) == 2
    assert exit_code == validator.EXIT_PASS
    assert result["counts"]["accepted"] == 2
    assert result["latest_event"]["event_id"] == "mle:rollback.001"


def test_valid_fixture_passes_and_reports_latest_event() -> None:
    result, exit_code = validator.validate_migration_ledger(load_fixture("valid_append_only"))

    assert exit_code == validator.EXIT_PASS
    assert result["errors"] == []
    assert result["latest_event"] == {
        "event_id": "mle:rollback.001",
        "workspace_id": "fixture_workspace",
        "migration_id": "mig:source-db.rollback.v4",
        "migration_type": "rollback_reference",
        "occurred_at": "2026-06-02T12:10:00Z",
        "tool_surface": "tool.topic_backup_drill_py",
        "input_version": "source-db.schema.v5",
        "output_version": "source-db.schema.v4",
        "backup_ref": "runtime/backups/crown_jewels/fixture_workspace/20260602T120900Z/files/repo/dbs/index/source.db",
        "snapshot_ref": "runtime/backups/crown_jewels/fixture_workspace/20260602T120900Z/manifest.json",
        "rollback_of_event_id": "mle:schema.001",
    }


def test_invalid_duplicate_event_id_fails() -> None:
    result, exit_code = validator.validate_migration_ledger(load_fixture("invalid_duplicate_event_id"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "DUPLICATE_EVENT_ID"


def test_invalid_rollback_without_snapshot_or_backup_fails() -> None:
    result, exit_code = validator.validate_migration_ledger(load_fixture("invalid_rollback_without_snapshot_or_backup"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "ROLLBACK_EVIDENCE_REQUIRED"


def test_validator_cli_writes_reports(tmp_path: Path) -> None:
    target = load_fixture("valid_append_only")
    report_json = tmp_path / "report.json"
    report_text = tmp_path / "report.txt"

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATORS_DIR / "validate_migration_ledger.py"),
            str(target),
            "--scenario",
            "valid_append_only",
            "--target-id",
            "inputs/migration_ledger.jsonl",
            "--report-json",
            str(report_json),
            "--report-text",
            str(report_text),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == validator.EXIT_PASS, proc.stdout + proc.stderr
    report = json.loads(report_json.read_text(encoding="utf-8"))
    assert report["validator"] == "migration_ledger"
    assert report["status"] == "pass"
    assert report["latest_event"]["event_id"] == "mle:rollback.001"
    assert "accepted=2" in report_text.read_text(encoding="utf-8")
