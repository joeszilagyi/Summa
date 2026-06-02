from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATORS_DIR = REPO_ROOT / "tools" / "validators"
VALIDATOR_PATH = VALIDATORS_DIR / "validate_knowledge_tree_export.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "validators" / "knowledge_tree_export"

if str(VALIDATORS_DIR) not in sys.path:
    sys.path.insert(0, str(VALIDATORS_DIR))

spec = importlib.util.spec_from_file_location("knowledge_tree_export_validator_for_tests", VALIDATOR_PATH)
validator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(validator)


def validate(path: Path) -> tuple[dict[str, object], int]:
    return validator.validate_knowledge_tree_export(path)


def load_fixture(name: str) -> Path:
    return FIXTURE_ROOT / name / "inputs" / "knowledge_tree_export.json"


def test_valid_minimal_fixture_passes() -> None:
    result, exit_code = validate(load_fixture("valid_minimal"))

    assert exit_code == validator.EXIT_PASS
    assert result["counts"] == {"inspected": 1, "accepted": 1, "rejected": 0, "deferred": 0}
    assert result["errors"] == []
    assert result["warnings"] == []


def test_invalid_missing_required_key_fixture_fails() -> None:
    result, exit_code = validate(load_fixture("invalid_missing_required_key"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "MISSING_REQUIRED_KEY"


def test_invalid_unknown_source_ref_fixture_fails() -> None:
    result, exit_code = validate(load_fixture("invalid_unknown_source_ref"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert result["errors"][0]["code"] == "UNKNOWN_SOURCE_REFERENCE"


def test_invalid_broken_page_link_fixture_fails() -> None:
    result, exit_code = validate(load_fixture("invalid_broken_page_link"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert [error["code"] for error in result["errors"]] == [
        "BROKEN_PAGE_LINK",
        "BROKEN_SECTION_LINK",
    ]


def test_metadata_only_exception_fixture_passes() -> None:
    result, exit_code = validate(load_fixture("valid_metadata_only_exception"))

    assert exit_code == validator.EXIT_PASS
    assert result["errors"] == []


def test_unreviewed_authoritative_claim_fixture_fails_with_queue_refs() -> None:
    result, exit_code = validate(load_fixture("invalid_unreviewed_authoritative_claim"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    codes = [error["code"] for error in result["errors"]]
    assert codes == ["AUTHORITY_REVIEW_REQUIRED", "FIELD_AUTHORITY_BLOCKED"]
    assert "claim:17" in result["errors"][0]["message"]
    assert "claim:17" in result["errors"][1]["message"]


def test_validator_cli_writes_reports(tmp_path: Path) -> None:
    target = load_fixture("valid_minimal")
    report_json = tmp_path / "report.json"
    report_text = tmp_path / "report.txt"

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_PATH),
            str(target),
            "--scenario",
            "valid_minimal",
            "--target-id",
            "inputs/knowledge_tree_export.json",
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
    assert report["validator"] == "knowledge_tree_export"
    assert report["status"] == "pass"
    assert "accepted=1" in report_text.read_text(encoding="utf-8")


def test_build_manifest_validator_module_imports_export_validator() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import tools.validators.validate_knowledge_tree_build_manifest as mod; print(mod.VALIDATOR_NAME)",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "knowledge_tree_build_manifest"
