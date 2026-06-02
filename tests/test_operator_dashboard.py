from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


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
                },
                "backup_posture": {"policy_status": "pass", "status": "pass"},
                "scheduler": {"selector_status": "pass", "status": "pass"},
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
    for label in ["Workspaces", "Databases", "Locks", "Findings"]:
        assert f"<h2>{label}</h2>" in body
    for health in [
        "crown_jewel_backup_posture",
        "db_integrity_smoke",
        "scheduler_eligibility",
        "public_private_gate",
        "workspace_locks",
    ]:
        assert health in body
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
