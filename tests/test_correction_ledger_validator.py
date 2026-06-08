from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
VALIDATOR_PATH = VALIDATORS_DIR / "validate_correction_ledger.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "validators" / "correction_ledger"

if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

spec = importlib.util.spec_from_file_location("correction_ledger_validator_for_tests", VALIDATOR_PATH)
assert spec is not None
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)


def validate(path: Path) -> tuple[dict[str, object], int]:
    return validator.validate_correction_ledger(path)


def load_fixture(name: str) -> Path:
    return FIXTURE_ROOT / name / "inputs" / "correction_ledger.json"


def test_valid_lineage_fixture_passes_and_reports_resolution() -> None:
    result, exit_code = validate(load_fixture("valid_lineage"))

    assert exit_code == validator.EXIT_PASS
    assert result["errors"] == []
    assert result["resolution"] == {
        "current_object_refs": ["authority:1", "claim:8", "work:10", "work:11"],
        "superseded_object_refs": ["authority:2", "authority:3", "claim:7", "work:9"],
        "superseded_by_event_id": {
            "authority:2": "cle:authority.dedupe.1",
            "authority:3": "cle:authority.dedupe.1",
            "claim:7": "cle:claim.supersede.1",
            "work:9": "cle:work.split.1",
        },
    }


def test_invalid_reuse_of_superseded_source_fails() -> None:
    result, exit_code = validate(load_fixture("invalid_reuse_superseded_source"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "SOURCE_OBJECT_NOT_CURRENT"


def test_invalid_split_wrong_result_count_fails() -> None:
    result, exit_code = validate(load_fixture("invalid_split_wrong_result_count"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "INVALID_RESULT_CARDINALITY"


def test_validator_cli_writes_reports(tmp_path: Path) -> None:
    target = load_fixture("valid_lineage")
    report_json = tmp_path / "report.json"
    report_text = tmp_path / "report.txt"

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_PATH),
            str(target),
            "--scenario",
            "valid_lineage",
            "--target-id",
            "inputs/correction_ledger.json",
            "--report-root",
            str(tmp_path),
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
    assert report["validator"] == "correction_ledger"
    assert report["status"] == "pass"
    assert report["resolution"]["current_object_refs"] == ["authority:1", "claim:8", "work:10", "work:11"]
    assert "accepted=1" in report_text.read_text(encoding="utf-8")
