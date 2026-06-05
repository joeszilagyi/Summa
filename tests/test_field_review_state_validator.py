from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
VALIDATOR_PATH = VALIDATORS_DIR / "validate_field_review_state.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "validators" / "field_review_state"

if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

spec = importlib.util.spec_from_file_location("field_review_state_validator_for_tests", VALIDATOR_PATH)
assert spec is not None
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)


def validate(path: Path) -> tuple[dict[str, object], int]:
    return validator.validate_field_review_state(path)


def load_fixture(name: str) -> Path:
    return FIXTURE_ROOT / name / "inputs" / "field_review_state.json"


def test_valid_field_review_state_fixture_passes() -> None:
    result, exit_code = validate(load_fixture("valid_all_states"))

    assert exit_code == validator.EXIT_PASS
    assert result["counts"] == {"inspected": 1, "accepted": 1, "rejected": 0, "deferred": 0}
    assert result["errors"] == []
    assert result["warnings"] == []


def test_invalid_superseded_fixture_fails() -> None:
    result, exit_code = validate(load_fixture("invalid_superseded_without_reference"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "SUPERSEDES_REFERENCE_REQUIRED"


def test_invalid_cross_field_demotion_fixture_fails() -> None:
    result, exit_code = validate(load_fixture("invalid_cross_field_demotion"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "FIELD_REFERENCE_MISMATCH"


def test_validator_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    target = tmp_path / "field_review_state.json"
    raw = load_fixture("valid_all_states").read_text(encoding="utf-8")
    raw = raw.replace(
        '"field_path": "title"',
        '"field_path": "title_duplicate", "field_path": "title"',
        1,
    )
    target.write_text(raw, encoding="utf-8")

    result, exit_code = validate(target)

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "DUPLICATE_JSON_KEY"


def test_validator_rejects_out_of_order_field_history(tmp_path: Path) -> None:
    payload = json.loads(load_fixture("valid_all_states").read_text(encoding="utf-8"))
    payload["field_reviews"][1]["reviewed_at"] = "2026-06-01T09:55:00Z"
    target = tmp_path / "field_review_state.json"
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    result, exit_code = validate(target)

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "FIELD_REVIEW_HISTORY_OUT_OF_ORDER"


def test_validator_cli_writes_reports(tmp_path: Path) -> None:
    target = load_fixture("valid_all_states")
    report_json = tmp_path / "report.json"
    report_text = tmp_path / "report.txt"

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_PATH),
            str(target),
            "--scenario",
            "valid_all_states",
            "--target-id",
            "fixtures/valid_all_states",
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
    assert report["validator"] == "field_review_state"
    assert report["status"] == "pass"
    assert "accepted=1" in report_text.read_text(encoding="utf-8")
