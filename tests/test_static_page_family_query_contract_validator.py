from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
VALIDATOR_PATH = VALIDATORS_DIR / "validate_static_page_family_query_contract.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "validators" / "static_page_family_query_contract"
CONTRACT_PATH = REPO_ROOT / "config" / "static_page_family_query_contract.json"

if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

spec = importlib.util.spec_from_file_location("static_page_family_query_contract_validator_for_tests", VALIDATOR_PATH)
assert spec is not None
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)


def validate(path: Path) -> tuple[dict[str, object], int]:
    return validator.validate_static_page_family_query_contract(path)


def load_fixture(name: str) -> Path:
    return FIXTURE_ROOT / name / "inputs" / "static_page_family_query_contract.json"


def test_checked_in_contract_passes() -> None:
    result, exit_code = validate(CONTRACT_PATH)

    assert exit_code == validator.EXIT_PASS
    assert result["errors"] == []


def test_invalid_missing_required_page_family_fails() -> None:
    result, exit_code = validate(load_fixture("invalid_missing_required_page_family"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "REQUIRED_PAGE_FAMILY_MISSING"


def test_invalid_visibility_overlap_fails() -> None:
    result, exit_code = validate(load_fixture("invalid_visibility_overlap"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "VISIBILITY_FIELD_OVERLAP"


def test_validator_cli_writes_reports(tmp_path: Path) -> None:
    report_json = tmp_path / "report.json"
    report_text = tmp_path / "report.txt"

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_PATH),
            str(CONTRACT_PATH),
            "--scenario",
            "checked_in",
            "--target-id",
            "config/static_page_family_query_contract.json",
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
    assert report["validator"] == "static_page_family_query_contract"
    assert report["status"] == "pass"
    assert "accepted=1" in report_text.read_text(encoding="utf-8")
