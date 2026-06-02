from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
VALIDATOR_PATH = VALIDATORS_DIR / "validate_release_readiness.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "validators" / "release_readiness"

if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

spec = importlib.util.spec_from_file_location("release_readiness_validator_for_tests", VALIDATOR_PATH)
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)


def load_fixture(name: str) -> Path:
    return FIXTURE_ROOT / name / "inputs"


def test_pass_bundle_reports_pass() -> None:
    report = validator.aggregate_release_readiness(load_fixture("pass"))

    assert report["status"] == "pass"
    assert report["summary"] == {
        "check_count": 5,
        "pass_count": 5,
        "warn_count": 0,
        "block_count": 0,
        "finding_count": 0,
    }


def test_warn_bundle_reports_warn_with_actionable_findings() -> None:
    report = validator.aggregate_release_readiness(load_fixture("warn"))

    assert report["status"] == "warn"
    assert report["summary"]["warn_count"] == 2
    assert [item["code"] for item in report["findings"]] == [
        "CROWN_JEWEL_BACKUP_EVIDENCE_MISSING",
        "PRIVATE_NOTE_MARKER",
    ]


def test_block_bundle_reports_block_with_actionable_findings() -> None:
    report = validator.aggregate_release_readiness(load_fixture("block"))

    assert report["status"] == "block"
    assert report["summary"]["block_count"] == 3
    assert [item["code"] for item in report["findings"]] == [
        "FIELD_REVIEW_NOT_REVIEWED",
        "RAW_PAYLOAD_FIELD_INDEXED",
        "SECRET_MARKER",
    ]


def test_validator_cli_writes_reports(tmp_path: Path) -> None:
    target = load_fixture("warn")
    report_json = tmp_path / "report.json"
    report_text = tmp_path / "report.txt"

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_PATH),
            str(target),
            "--scenario",
            "warn_bundle",
            "--target-id",
            "inputs",
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
    assert report["status"] == "warn"
    assert report["schema_version"] == "release-readiness-report.v1"
    assert "check[0]=" in report_text.read_text(encoding="utf-8")


def test_validator_cli_returns_failure_for_blocked_release() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_PATH),
            str(load_fixture("block")),
            "--format",
            "text",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == validator.EXIT_VALIDATION_FAILED
    assert "status=block" in proc.stdout
