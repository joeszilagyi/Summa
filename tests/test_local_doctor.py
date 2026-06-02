from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "local_doctor.py"

sys.path.insert(0, str(REPO_ROOT / "tools" / "scripts"))
import local_doctor


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
        "db_integrity_smoke",
        "workspace_locks",
        "public_private_sharing_gate",
    }
    assert report["summary"]["status"] in {"pass", "warn", "fail"}
    assert isinstance(report["workspaces"], list)
    assert isinstance(report["databases"], list)
    assert isinstance(report["locks"], list)
    assert set(report["backup_posture"]).issuperset({"policy_status", "status"})
    assert set(report["scheduler"]).issuperset({"selector_status", "status"})
    assert set(report["public_gates"]).issuperset({"surfaces", "status"})
    assert report["redaction"]["raw_payloads_included"] is False
    assert "/home/" not in body


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
