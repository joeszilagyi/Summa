import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "validators" / "jsonl_syntax"
VALIDATOR = REPO_ROOT / "tools" / "validators" / "validate_jsonl.py"

EXIT_PASS = 0
EXIT_VALIDATION_FAILED = 1
EXIT_INPUT_UNAVAILABLE = 4


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_fixture(tmp_path: Path, scenario: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    source_dir = FIXTURE_ROOT / scenario
    scenario_dir = tmp_path / scenario
    shutil.copytree(source_dir, scenario_dir)
    actual_dir = scenario_dir / "actual"
    actual_dir.mkdir()

    target = Path("inputs/demo.jsonl")
    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            str(target),
            "--scenario",
            scenario,
            "--target-id",
            str(target),
            "--report-json",
            "actual/report.json",
            "--report-text",
            "actual/report.txt",
        ],
        cwd=scenario_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    return proc, scenario_dir


def render_text_from_json(report: dict[str, object]) -> str:
    counts = report["counts"]
    lines = [
        f"validator={report['validator']}",
        f"scenario={report['scenario'] or '-'}",
        f"target={report['target']}",
        f"status={report['status']}",
        (
            "inspected={inspected} accepted={accepted} rejected={rejected} deferred={deferred}".format(
                **counts
            )
        ),
        f"errors={len(report['errors'])} warnings={len(report['warnings'])}",
    ]
    for index, error in enumerate(report["errors"]):
        line_suffix = f" line={error['line']}" if error["line"] is not None else ""
        lines.append(
            f"error[{index}]={error['code']}{line_suffix} message={error['message']}"
        )
    for index, warning in enumerate(report["warnings"]):
        line_suffix = f" line={warning['line']}" if warning["line"] is not None else ""
        lines.append(
            f"warning[{index}]={warning['code']}{line_suffix} message={warning['message']}"
        )
    return "\n".join(lines) + "\n"


def assert_matches_golden(scenario_dir: Path) -> None:
    expected_dir = scenario_dir / "expected"
    actual_dir = scenario_dir / "actual"

    expected_json = json.loads((expected_dir / "report.json").read_text(encoding="utf-8"))
    actual_json = json.loads((actual_dir / "report.json").read_text(encoding="utf-8"))
    assert actual_json == expected_json

    expected_text = (expected_dir / "report.txt").read_text(encoding="utf-8")
    actual_text = (actual_dir / "report.txt").read_text(encoding="utf-8")
    assert actual_text == expected_text
    assert actual_text == render_text_from_json(actual_json)


def test_valid_fixture_passes_and_matches_golden(tmp_path: Path) -> None:
    input_hash_before = sha256(FIXTURE_ROOT / "valid_minimal" / "inputs" / "demo.jsonl")
    proc, scenario_dir = run_fixture(tmp_path, "valid_minimal")
    input_hash_after = sha256(scenario_dir / "inputs" / "demo.jsonl")

    assert proc.returncode == EXIT_PASS, proc.stdout + proc.stderr
    assert_matches_golden(scenario_dir)
    assert proc.stdout == (scenario_dir / "actual" / "report.txt").read_text(encoding="utf-8")
    assert input_hash_after == input_hash_before


def test_invalid_fixture_fails_and_matches_golden(tmp_path: Path) -> None:
    input_hash_before = sha256(
        FIXTURE_ROOT / "invalid_malformed" / "inputs" / "demo.jsonl"
    )
    proc, scenario_dir = run_fixture(tmp_path, "invalid_malformed")
    input_hash_after = sha256(scenario_dir / "inputs" / "demo.jsonl")

    assert proc.returncode == EXIT_VALIDATION_FAILED, proc.stdout + proc.stderr
    assert_matches_golden(scenario_dir)
    assert proc.stdout == (scenario_dir / "actual" / "report.txt").read_text(encoding="utf-8")
    assert input_hash_after == input_hash_before


def test_invalid_utf8_reports_input_unavailable(tmp_path: Path) -> None:
    target = tmp_path / "bad.jsonl"
    target.write_bytes(b'{"kind": "demo", "name": "alpha"}\n\xff\n')

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            str(target),
            "--scenario",
            "invalid_utf8",
            "--target-id",
            "inputs/bad.jsonl",
            "--report-json",
            "report.json",
            "--report-text",
            "report.txt",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == EXIT_INPUT_UNAVAILABLE, proc.stdout + proc.stderr
    assert proc.stderr == ""
    assert proc.stdout == (tmp_path / "report.txt").read_text(encoding="utf-8")

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "fail"
    assert report["errors"] == [
        {
            "code": "INPUT_DECODE_ERROR",
            "line": None,
            "message": "input file is not valid UTF-8",
        }
    ]


def test_crlf_jsonl_lines_pass(tmp_path: Path) -> None:
    target = tmp_path / "demo.jsonl"
    target.write_bytes(
        b'{"kind":"demo","name":"alpha"}\r\n{"kind":"demo","name":"beta"}\r\n'
    )

    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(target)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == EXIT_PASS, proc.stdout + proc.stderr
    assert "status=pass" in proc.stdout


def test_nonstandard_json_constants_fail(tmp_path: Path) -> None:
    target = tmp_path / "demo.jsonl"
    target.write_text('{"kind":"demo","score":NaN}\n', encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(target)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == EXIT_VALIDATION_FAILED, proc.stdout + proc.stderr
    assert "JSONL_PARSE_ERROR" in proc.stdout
    assert "invalid JSON constant NaN" in proc.stdout
    assert "Traceback" not in proc.stderr


def test_duplicate_json_keys_fail(tmp_path: Path) -> None:
    target = tmp_path / "demo.jsonl"
    target.write_text('{"kind":"demo","name":"alpha","name":"beta"}\n', encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(target)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == EXIT_VALIDATION_FAILED, proc.stdout + proc.stderr
    assert "DUPLICATE_JSON_KEY" in proc.stdout
    assert "duplicate JSON object key: name" in proc.stdout
    assert "Traceback" not in proc.stderr
