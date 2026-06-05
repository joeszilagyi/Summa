from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
VALIDATOR_PATH = VALIDATORS_DIR / "validate_canonical_graph_model_outline.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "validators" / "canonical_graph_model_outline"
CANONICAL_OUTLINE = REPO_ROOT / "config" / "canonical_graph_model_outline.json"

if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

spec = importlib.util.spec_from_file_location("canonical_graph_model_outline_validator_for_tests", VALIDATOR_PATH)
assert spec is not None
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)


def validate(path: Path) -> tuple[dict[str, object], int]:
    return validator.validate_canonical_graph_model_outline(path)


def load_fixture(name: str) -> Path:
    return FIXTURE_ROOT / name / "inputs" / "canonical_graph_model_outline.json"


def test_checked_in_outline_passes() -> None:
    result, exit_code = validate(CANONICAL_OUTLINE)

    assert exit_code == validator.EXIT_PASS
    assert result["counts"] == {"inspected": 1, "accepted": 1, "rejected": 0, "deferred": 0}
    assert result["errors"] == []


def test_invalid_missing_required_sidecar_fails() -> None:
    result, exit_code = validate(load_fixture("invalid_missing_required_sidecar"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "REQUIRED_SIDECAR_MISSING"


def test_invalid_assertion_table_mapping_fails() -> None:
    result, exit_code = validate(load_fixture("invalid_assertion_table_mapping"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "SQLITE_TABLE_MAPPING_MISSING"


def test_validator_cli_writes_reports(tmp_path: Path) -> None:
    report_json = tmp_path / "report.json"
    report_text = tmp_path / "report.txt"

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_PATH),
            str(CANONICAL_OUTLINE),
            "--scenario",
            "checked_in",
            "--target-id",
            "config/canonical_graph_model_outline.json",
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
    assert report["validator"] == "canonical_graph_model_outline"
    assert report["status"] == "pass"
    assert "accepted=1" in report_text.read_text(encoding="utf-8")
