from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.scripts import build_operator_dashboard as dashboard

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "build_operator_dashboard.py"


def test_operator_dashboard_renders_from_synthetic_doctor_report(tmp_path: Path) -> None:
    fixture = tmp_path / "doctor.json"
    fixture.write_text(
        json.dumps(
            {
                "schema_version": "local-doctor-report.v1",
                "summary": {
                    "status": "pass",
                    "finding_count": 0,
                    "operator_action_required_count": 0,
                },
                "checks": {
                    "crown_jewel_backup_posture": "pass",
                    "db_integrity_smoke": "pass",
                    "scheduler_eligibility": "pass",
                    "public_private_gate": "pass",
                    "workspace_locks": "pass",
                    "graph_closure": "no_rows",
                },
                "backup_posture": {"policy_status": "pass", "status": "pass"},
                "scheduler": {"selector_status": "pass", "status": "pass"},
                "canonical_store": {
                    "status": "initialized_empty",
                    "schema_version": 1,
                    "total_rows": 0,
                    "last_ingest_event_type": None,
                    "last_ingest_at": None,
                    "last_provenance_event_at": None,
                    "family_counts": {
                        "entity": 0,
                        "relationship": 0,
                        "assertion": 0,
                        "provenance_event": 0,
                        "confidence_assessment": 0,
                        "review_annotation": 0,
                    },
                    "table_counts": {
                        "work": 0,
                        "source_claim": 0,
                        "provenance_event": 0,
                    },
                    "warnings": [],
                    "errors": [],
                    "recommended_interpretation": "Store is initialized and valid, but contains no canonical records yet.",
                },
                "loop_health": {
                    "health_status": "insufficient_data",
                    "lookback_cycles": 5,
                    "aggregate_metrics": {"yield_trend": "insufficient_data"},
                    "review_backlog": {
                        "pending_review_count": 0,
                        "oldest_pending_age_days": None,
                        "median_pending_age_days": None,
                    },
                    "contradictions": {
                        "total_contradictions": 0,
                        "new_contradictions": 0,
                        "contradictions_per_new_source_claim": None,
                    },
                    "ingestion_resolution": {
                        "reviewable_ingested_count": 0,
                        "review_decision_applied_count": None,
                        "resolution_coverage": None,
                    },
                    "per_cycle_metrics": [],
                    "warnings": [],
                    "limitations": ["cycle_history_unavailable"],
                },
                "graph_closure": {
                    "status": "no_rows",
                    "orphan_error_count": 0,
                    "unresolved_tracked_count": 0,
                    "repairable_count": 0,
                    "quarantined_count": 0,
                    "read_only": True,
                    "top_issues": [],
                },
                "public_gates": {
                    "surfaces": {
                        "public_presentation_schema": "present",
                        "public_presentation_validator": "present",
                    }
                },
                "workspaces": [],
                "databases": [],
                "locks": [],
                "findings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "dashboard.html"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--doctor-report",
            str(fixture),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    body = output.read_text(encoding="utf-8")

    assert report["status"] == "pass"
    assert report["read_only"] is True
    assert "Summa Operator Health" in body
    for label in [
        "Canonical Store",
        "Loop Health",
        "Graph Closure",
        "Workspaces",
        "Databases",
        "Locks",
        "Findings",
    ]:
        assert f"<h2>{label}</h2>" in body
    for health in [
        "crown_jewel_backup_posture",
        "db_integrity_smoke",
        "scheduler_eligibility",
        "public_private_gate",
        "workspace_locks",
    ]:
        assert health in body
    assert "initialized_empty" in body
    assert "contains no canonical records yet" in body
    assert "<form" not in body
    assert "<button" not in body


def test_operator_dashboard_rejects_wrong_report_schema(tmp_path: Path) -> None:
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"schema_version": "not-doctor.v1"}) + "\n", encoding="utf-8")
    output = tmp_path / "dashboard.html"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--doctor-report",
            str(wrong),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 1
    assert "local-doctor-report.v1" in proc.stderr
    assert not output.exists()


def test_operator_dashboard_renders_empty_sections_and_status_classes() -> None:
    report = {
        "schema_version": "local-doctor-report.v1",
        "summary": {
            "status": "warn",
            "finding_count": 1,
            "operator_action_required_count": 1,
        },
        "checks": {"graph_closure": "fail"},
        "backup_posture": {"policy_status": "pass", "status": "warn"},
        "migration_posture": {"status": "fail"},
        "scheduler": {"selector_status": "pass", "status": "pass"},
        "canonical_store": {
            "status": "initialized_empty",
            "schema_version": 1,
            "total_rows": 0,
            "last_ingest_event_type": None,
            "last_ingest_at": None,
            "last_provenance_event_at": None,
            "family_counts": {},
            "table_counts": {},
            "warnings": ["empty_store"],
            "errors": ["missing_seed_data"],
            "recommended_interpretation": "Store is initialized.",
        },
        "loop_health": {
            "health_status": "insufficient_data",
            "lookback_cycles": 3,
            "aggregate_metrics": {},
            "review_backlog": {},
            "contradictions": {},
            "ingestion_resolution": {},
            "per_cycle_metrics": [],
            "warnings": ["cycle_history_missing"],
            "limitations": ["cycle_history_unavailable"],
        },
        "graph_closure": {
            "status": "no_rows",
            "orphan_error_count": 0,
            "unresolved_tracked_count": 0,
            "repairable_count": 0,
            "quarantined_count": 0,
            "read_only": True,
            "top_issues": [],
        },
        "public_gates": {"surfaces": {}},
        "workspaces": [],
        "databases": [],
        "locks": [],
        "findings": [],
    }

    assert dashboard.status_class("pass") == "status-pass"
    assert dashboard.status_class("warn") == "status-warn"
    assert dashboard.status_class("fail") == "status-fail"
    assert dashboard.status_class("mystery") == "status-unknown"

    assert "No resolved workspaces" in dashboard.render_workspaces(report)
    assert "No SQLite stores found" in dashboard.render_databases(report)
    assert "No active lock metadata" in dashboard.render_locks(report)
    assert "No findings" in dashboard.render_findings(report)
    assert "No canonical family counts available" in dashboard.render_canonical_family_counts(report)
    assert "No canonical table counts available" in dashboard.render_canonical_table_counts(report)
    assert "No loop cycle metrics available" in dashboard.render_loop_health_cycle_rows(report)
    assert "No graph-closure issues available" in dashboard.render_graph_closure_issues(report)

    empty_notes_report = dict(report)
    empty_notes_report["canonical_store"] = {
        "status": "initialized_empty",
        "schema_version": 1,
        "total_rows": 0,
        "last_ingest_event_type": None,
        "last_ingest_at": None,
        "last_provenance_event_at": None,
        "family_counts": {},
        "table_counts": {},
        "warnings": [],
        "errors": [],
        "recommended_interpretation": None,
    }
    assert "No canonical store notes" in dashboard.render_canonical_notes(empty_notes_report)

    empty_loop_report = dict(report)
    empty_loop_report["loop_health"] = {
        "health_status": "insufficient_data",
        "lookback_cycles": 3,
        "aggregate_metrics": {},
        "review_backlog": {},
        "contradictions": {},
        "ingestion_resolution": {},
        "per_cycle_metrics": [],
        "warnings": [],
        "limitations": [],
    }
    assert (
        "No loop-health warnings or limitations"
        in dashboard.render_loop_health_notes(empty_loop_report)
    )

    body = dashboard.render_dashboard(report, title="Custom Operator Health")
    assert "Custom Operator Health" in body
    assert "status-pass" in body
    assert "status-warn" in body
    assert "status-fail" in body
    assert "No resolved workspaces" in body
    assert "No SQLite stores found" in body
    assert "No findings" in body


def test_operator_dashboard_renders_text_format_and_rejects_invalid_reports(tmp_path: Path) -> None:
    fixture = tmp_path / "doctor.json"
    fixture.write_text(
        json.dumps(
            {
                "schema_version": "local-doctor-report.v1",
                "summary": {
                    "status": "pass",
                    "finding_count": 0,
                    "operator_action_required_count": 0,
                },
                "checks": {"graph_closure": "no_rows"},
                "backup_posture": {"policy_status": "pass", "status": "pass"},
                "migration_posture": {"status": "pass"},
                "scheduler": {"selector_status": "pass", "status": "pass"},
                "canonical_store": {
                    "status": "initialized_empty",
                    "schema_version": 1,
                    "total_rows": 0,
                    "last_ingest_event_type": None,
                    "last_ingest_at": None,
                    "last_provenance_event_at": None,
                    "family_counts": {},
                    "table_counts": {},
                    "warnings": [],
                    "errors": [],
                    "recommended_interpretation": None,
                },
                "loop_health": {
                    "health_status": "insufficient_data",
                    "lookback_cycles": 0,
                    "aggregate_metrics": {},
                    "review_backlog": {},
                    "contradictions": {},
                    "ingestion_resolution": {},
                    "per_cycle_metrics": [],
                    "warnings": [],
                    "limitations": [],
                },
                "graph_closure": {
                    "status": "no_rows",
                    "orphan_error_count": 0,
                    "unresolved_tracked_count": 0,
                    "repairable_count": 0,
                    "quarantined_count": 0,
                    "read_only": True,
                    "top_issues": [],
                },
                "public_gates": {"surfaces": {}},
                "workspaces": [],
                "databases": [],
                "locks": [],
                "findings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "dashboard.html"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--doctor-report",
            str(fixture),
            "--output",
            str(output),
            "--format",
            "text",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "schema_version=operator-dashboard-build-report.v1" in proc.stdout
    assert "read_only=True" in proc.stdout
    assert output.read_text(encoding="utf-8").startswith("<!doctype html>")

    non_object = tmp_path / "non-object.json"
    non_object.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a JSON object"):
        dashboard.load_doctor_report(non_object)

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="could not read doctor report"):
        dashboard.load_doctor_report(invalid)
