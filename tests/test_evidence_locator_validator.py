from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
VALIDATOR_PATH = VALIDATORS_DIR / "validate_evidence_locator.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "validators" / "evidence_locator"
FIELD_REVIEW_VALIDATOR_PATH = VALIDATORS_DIR / "validate_field_review_state.py"
FIELD_REVIEW_FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "validators"
    / "field_review_state"
    / "valid_all_states"
    / "inputs"
    / "field_review_state.json"
)

if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

spec = importlib.util.spec_from_file_location("evidence_locator_validator_for_tests", VALIDATOR_PATH)
assert spec is not None
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)

field_review_spec = importlib.util.spec_from_file_location("field_review_state_validator_for_evidence_tests", FIELD_REVIEW_VALIDATOR_PATH)
assert field_review_spec is not None
field_review_validator = importlib.util.module_from_spec(field_review_spec)
assert field_review_spec.loader is not None
field_review_spec.loader.exec_module(field_review_validator)


def validate(path: Path) -> tuple[dict[str, object], int]:
    return validator.validate_evidence_locator(path)


def load_fixture(name: str) -> Path:
    return FIXTURE_ROOT / name / "inputs" / "evidence_locator.json"


def test_valid_page_span_fixture_passes() -> None:
    result, exit_code = validate(load_fixture("valid_page_span"))

    assert exit_code == validator.EXIT_PASS
    assert result["errors"] == []


def test_valid_line_span_fixture_passes() -> None:
    result, exit_code = validate(load_fixture("valid_line_span"))

    assert exit_code == validator.EXIT_PASS
    assert result["errors"] == []


def test_valid_metadata_only_fixture_passes() -> None:
    result, exit_code = validate(load_fixture("valid_metadata_only"))

    assert exit_code == validator.EXIT_PASS
    assert result["errors"] == []


def test_invalid_private_text_leak_fixture_fails() -> None:
    result, exit_code = validate(load_fixture("invalid_private_text_leak"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert [error["code"] for error in result["errors"]] == [
        "PUBLIC_EXCERPT_REDACTION_CONFLICT",
        "PUBLIC_EXCERPT_QUOTE_CONFLICT",
        "PRIVATE_TEXT_LEAK",
    ]


def test_field_review_state_accepts_optional_evidence_locator_ref(tmp_path: Path) -> None:
    payload = json.loads(FIELD_REVIEW_FIXTURE.read_text(encoding="utf-8"))
    payload["field_reviews"][0]["evidence_ref"]["evidence_locator_ref"] = "evl:scan.page.12"
    target = tmp_path / "field_review_state.json"
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    result, exit_code = field_review_validator.validate_field_review_state(target)

    assert exit_code == field_review_validator.EXIT_PASS
    assert result["errors"] == []


def test_validator_cli_writes_reports(tmp_path: Path) -> None:
    target = load_fixture("valid_page_span")
    report_json = tmp_path / "report.json"
    report_text = tmp_path / "report.txt"

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_PATH),
            str(target),
            "--scenario",
            "valid_page_span",
            "--target-id",
            "inputs/evidence_locator.json",
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
    assert report["validator"] == "evidence_locator"
    assert report["status"] == "pass"
    assert "accepted=1" in report_text.read_text(encoding="utf-8")
