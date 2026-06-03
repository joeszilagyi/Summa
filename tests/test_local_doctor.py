from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from tools.source_db_tools import canonical_ingest, canonical_store


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "local_doctor.py"
FIXTURE_BATCH = REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "gather-candidate-batch.json"
FIXED_TIMESTAMP = "2026-06-03T12:34:56Z"

sys.path.insert(0, str(REPO_ROOT / "tools" / "scripts"))
import local_doctor
sys.path.insert(0, str(REPO_ROOT / "tools" / "common"))
import migration_ledger


def test_local_doctor_report_is_read_only_and_redacted() -> None:
    report = local_doctor.build_report(REPO_ROOT)
    body = json.dumps(report)

    assert report["schema_version"] == "local-doctor-report.v1"
    assert report["read_only"] is True
    assert report["auto_fix_performed"] is False
    assert set(report["checks"]) == {
        "repo_hygiene",
        "validator_availability",
        "topic_workspace_registry",
        "workspaces",
        "scheduler_eligibility",
        "crown_jewel_backup_posture",
        "migration_ledger_posture",
        "db_integrity_smoke",
        "canonical_store_population",
        "workspace_locks",
        "public_private_sharing_gate",
    }
    assert report["summary"]["status"] in {"pass", "warn", "fail"}
    assert report["checks"]["validator_availability"] == "available"
    assert isinstance(report["workspaces"], list)
    assert isinstance(report["databases"], list)
    assert isinstance(report["canonical_store"], dict)
    assert isinstance(report["locks"], list)
    assert set(report["backup_posture"]).issuperset({"policy_status", "status"})
    assert set(report["migration_posture"]).issuperset({"ledger_count", "status"})
    assert set(report["scheduler"]).issuperset({"selector_status", "status"})
    assert set(report["public_gates"]).issuperset({"surfaces", "status"})
    assert report["canonical_store"]["status"] == "absent"
    assert report["redaction"]["raw_payloads_included"] is False
    assert "/home/" not in body


def bootstrap_db(tmp_path: Path, *, populated: bool = False) -> Path:
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.local_doctor",
    )
    if not populated:
        return db_path
    batch, batch_hash = canonical_ingest.load_validated_candidate_batch(FIXTURE_BATCH)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=FIXTURE_BATCH,
                batch_hash=batch_hash,
                db_path=db_path,
            )
    finally:
        conn.close()
    return db_path


def test_local_doctor_redacts_sensitive_values() -> None:
    value = local_doctor.redact(
        {
            "path": "/home/example/private/file.txt",
            "secret": "token=abc123",
        }
    )

    assert value["path"] == "[redacted-path]"
    assert value["secret"] == "token=[redacted]"


def test_local_doctor_reports_broken_validator_fixture(tmp_path: Path) -> None:
    report = local_doctor.build_report(tmp_path)

    codes = {finding["code"] for finding in report["findings"]}

    assert "VALIDATOR_MISSING" in codes
    assert report["checks"]["validator_availability"] == "missing"


def test_local_doctor_resolves_fixture_registry_without_mutating(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    manifest_path = workspace_root / ".indexer" / "subject_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "subject-manifest.v1",
                "subject_id": "subject.fixture",
                "display_name": "Fixture",
                "domain_pack": "general.v1",
                "scope_statement": "Fixture workspace for doctor test.",
                "languages": ["en"],
                "aliases": ["Fixture"],
                "disambiguation_terms": ["doctor"],
                "excluded_senses": ["non-fixture"],
                "enabled_facets": ["sources"],
                "query_families": ["web_search"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry = tmp_path / "topic_workspaces.local.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": "topic-workspace-registry.v1",
                "default_workspace_id": "fixture_workspace",
                "workspaces": [
                    {
                        "workspace_id": "fixture_workspace",
                        "topic_label": "Fixture",
                        "domain_pack": "general.v1",
                        "lifecycle_state": "active",
                        "schedule_posture": "scheduled",
                        "workspace_policy_class": "private_local",
                        "workspace_root": str(workspace_root),
                        "default_subject_manifest": str(manifest_path),
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    before = {path: path.stat().st_mtime_ns for path in [registry, manifest_path]}

    report = local_doctor.build_report(REPO_ROOT, registry=registry)

    assert report["checks"]["topic_workspace_registry"] == "pass"
    assert report["workspaces"][0]["workspace_id"] == "fixture_workspace"
    assert report["workspaces"][0]["workspace_root_status"] == "ok"
    assert report["workspaces"][0]["default_subject_manifest_status"] == "ok"
    assert {path: path.stat().st_mtime_ns for path in [registry, manifest_path]} == before


def test_local_doctor_reports_stale_workspace_lock(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    lock_root = repo_root / "runtime" / "locks"
    lock_root.mkdir(parents=True)
    lock_path = lock_root / "fixture_workspace.lock"
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": "workspace-lock.v1",
                "workspace_id": "fixture_workspace",
                "pid": -1,
                "host": "fixture-host",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    old = time.time() - 7200
    os.utime(lock_path, (old, old))

    locks, findings = local_doctor.inspect_locks(repo_root)

    assert locks == [
        {
            "path": str(lock_path),
            "workspace_id": "fixture_workspace",
            "pid": -1,
            "heartbeat_at": None,
            "status": "stale",
            "stale_reason": "heartbeat_expired",
        }
    ]
    assert [entry["code"] for entry in findings] == ["STALE_WORKSPACE_LOCK"]


def test_local_doctor_reports_latest_migration_posture(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    ledger_path = repo_root / "runtime" / "ledgers" / "fixture_workspace.migration-ledger.jsonl"
    migration_ledger.append_event(
        ledger_path,
        migration_ledger.build_event(
            workspace_id="fixture_workspace",
            migration_id="mig:build-manifest.v2",
            migration_type="artifact_contract_migration",
            subject_ref="contract/knowledge_tree_build_manifest",
            tool_surface="tool.build_static_knowledge_tree_py",
            tool_version="2026.06.02",
            input_version="knowledge-tree-build-manifest.v1",
            output_version="knowledge-tree-build-manifest.v2",
            input_artifact_refs=[{"role": "schema_contract", "path": "config/knowledge_tree_build_manifest.schema.json", "version": "knowledge-tree-build-manifest.v1"}],
            output_artifact_refs=[{"role": "schema_contract", "path": "config/knowledge_tree_build_manifest.schema.json", "version": "knowledge-tree-build-manifest.v2"}],
            occurred_at="2026-06-02T15:00:00Z",
            event_id="mle:build-manifest.001",
            note="Promote publish-root hash coverage.",
        ),
    )

    posture, findings = local_doctor.inspect_migration_posture(repo_root)

    assert posture["status"] == "pass"
    assert posture["ledger_count"] == 1
    assert posture["event_count"] == 1
    assert posture["latest_event_id"] == "mle:build-manifest.001"
    assert posture["latest_tool_surface"] == "tool.build_static_knowledge_tree_py"
    assert findings == []


def test_local_doctor_text_summary_cli(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(REPO_ROOT), "--format", "text"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "schema_version=local-doctor-report.v1" in proc.stdout
    assert "check.public_private_sharing_gate=" in proc.stdout
    assert "canonical_store.status=" in proc.stdout


def test_local_doctor_cli_writes_report_without_fixing(tmp_path: Path) -> None:
    output = tmp_path / "doctor.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(REPO_ROOT), "--output", str(output)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["read_only"] is True
    assert payload["auto_fix_performed"] is False


def test_local_doctor_reports_initialized_empty_canonical_store(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)

    report = local_doctor.build_report(REPO_ROOT, canonical_db=db_path)

    assert report["canonical_store"]["status"] == "initialized_empty"
    assert report["canonical_store"]["total_rows"] == 0
    assert report["canonical_store"]["family_counts"]["entity"] == 0
    assert report["checks"]["canonical_store_population"] == "warn"


def test_local_doctor_reports_populated_canonical_store_and_last_ingest(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path, populated=True)

    report = local_doctor.build_report(REPO_ROOT, canonical_db=db_path)

    assert report["canonical_store"]["status"] == "populated"
    assert report["canonical_store"]["total_rows"] > 0
    assert report["canonical_store"]["last_ingest_event_type"] == "gather_candidate_batch_ingest"
    assert report["canonical_store"]["last_ingest_at"] is not None
    assert report["checks"]["canonical_store_population"] == "pass"
