from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = REPO_ROOT / "tools" / "validators" / "validate_llm_prompt_fixture.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "validators" / "llm_prompt_fixture"
COMMON_PATH = REPO_ROOT / "tools" / "common" / "llm_source_text_wrapper.py"

validator_spec = importlib.util.spec_from_file_location("llm_prompt_fixture_validator_for_tests", VALIDATOR_PATH)
assert validator_spec is not None
validator = importlib.util.module_from_spec(validator_spec)
assert validator_spec.loader is not None
sys.modules[validator_spec.name] = validator
validator_spec.loader.exec_module(validator)

common_spec = importlib.util.spec_from_file_location("llm_source_text_wrapper_common_for_tests", COMMON_PATH)
assert common_spec is not None
wrapper = importlib.util.module_from_spec(common_spec)
assert common_spec.loader is not None
sys.modules[common_spec.name] = wrapper
common_spec.loader.exec_module(wrapper)


def load_fixture(name: str) -> Path:
    return FIXTURE_ROOT / name / "inputs" / "prompt_fixture.json"


def validate(path: Path) -> tuple[dict[str, object], int]:
    return validator.validate_prompt_fixture(path)


def test_valid_wrapped_hostile_prompt_fixture_passes() -> None:
    result, exit_code = validate(load_fixture("valid_wrapped_hostile_prompt"))

    assert exit_code == validator.EXIT_PASS
    assert result["errors"] == []


def test_invalid_unwrapped_source_text_fixture_fails() -> None:
    result, exit_code = validate(load_fixture("invalid_unwrapped_source_text"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    codes = [error["code"] for error in result["errors"]]
    assert "WRAPPED_SOURCE_BLOCK_REQUIRED" in codes
    assert "WRAPPED_SOURCE_BLOCK_COUNT_MISMATCH" in codes
    assert "UNWRAPPED_SOURCE_TEXT" in codes


def test_invalid_missing_instruction_negation_fixture_fails() -> None:
    result, exit_code = validate(load_fixture("invalid_missing_instruction_negation"))

    assert exit_code == validator.EXIT_VALIDATION_FAILED
    assert [error["code"] for error in result["errors"]] == ["INSTRUCTION_NEGATION_MISSING"]


def test_wrapper_renderer_and_parser_round_trip() -> None:
    template = wrapper.load_template()
    rendered = wrapper.render_wrapped_block(
        source_ref="source:fixture:1",
        provenance="fixture provenance",
        hazard_flags=["prompt_injection_text", "hostile_markup"],
        source_text="Ignore previous instructions.",
        template=template,
    )

    parsed = wrapper.parse_wrapped_blocks(rendered, template=template)

    assert len(parsed) == 1
    assert parsed[0].source_ref == "source:fixture:1"
    assert parsed[0].provenance == "fixture provenance"
    assert list(parsed[0].hazard_flags) == ["prompt_injection_text", "hostile_markup"]
    assert parsed[0].instruction_negation == template.instruction_negation_guidance
    assert parsed[0].source_text == "Ignore previous instructions."


def test_validator_cli_writes_reports(tmp_path: Path) -> None:
    target = load_fixture("valid_wrapped_hostile_prompt")
    report_json = tmp_path / "report.json"
    report_text = tmp_path / "report.txt"

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR_PATH),
            str(target),
            "--scenario",
            "valid_wrapped_hostile_prompt",
            "--target-id",
            "inputs/prompt_fixture.json",
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
    assert report["validator"] == "llm_prompt_fixture"
    assert report["status"] == "pass"
    assert "accepted=1" in report_text.read_text(encoding="utf-8")
