from __future__ import annotations

import json
import subprocess
import sys
import shutil
from pathlib import Path

from tools.validators import validate_source_acquisition_execution as validator


REPO_ROOT = Path(__file__).resolve().parents[1]
EXECUTION_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "execution_run"
VALIDATOR = REPO_ROOT / "tools" / "validators" / "validate_source_acquisition_execution.py"


def copy_execution_fixture(tmp_path: Path) -> Path:
    run_dir = tmp_path / "execution_run"
    shutil.copytree(EXECUTION_FIXTURE_DIR, run_dir)
    return run_dir


def write_json_lines(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records), encoding="utf-8")


def run_validator(run_dir: Path, *, tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            str(run_dir / "execution-record.json"),
            "--report-json",
            str(tmp_path / "actual" / "report.json"),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    report = json.loads((tmp_path / "actual" / "report.json").read_text(encoding="utf-8"))
    return proc, report


def test_execution_validation_rejects_capture_handoff_hash_mismatch(tmp_path: Path) -> None:
    run_dir = copy_execution_fixture(tmp_path)

    execution_record = json.loads((run_dir / "execution-record.json").read_text(encoding="utf-8"))
    execution_record["capture_event_count"] = 1
    capture_events = [
        json.loads(line)
        for line in (run_dir / "capture-events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    capture_events[0]["handoff_hash"] = "0" * 64

    (run_dir / "execution-record.json").write_text(
        json.dumps(execution_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_json_lines(run_dir / "capture-events.jsonl", capture_events)

    proc, report = run_validator(run_dir, tmp_path=tmp_path)

    assert proc.returncode == validator.EXIT_VALIDATION_FAILED
    assert any(
        error["code"] == "CAPTURE_HANDOFF_HASH_MISMATCH" for error in report.get("errors", [])
    )

