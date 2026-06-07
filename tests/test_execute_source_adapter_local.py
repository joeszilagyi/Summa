from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tools.scripts import execute_source_adapter as source_executor


REPO_ROOT = Path(__file__).resolve().parents[1]
PLANNER = REPO_ROOT / "tools" / "scripts" / "plan_local_source_adapter.py"
EXECUTOR = REPO_ROOT / "tools" / "scripts" / "execute_source_adapter.py"
VALIDATOR = REPO_ROOT / "tools" / "validators" / "validate_source_acquisition_execution.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "source_adapter_runtime" / "hostile_replay" / "local_source"
ADAPTER = FIXTURE_ROOT / "source_adapter.json"


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def canonical_jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records).encode(
        "utf-8"
    )


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_planner(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    handoff = tmp_path / "local-source-handoff.jsonl"
    proc = subprocess.run(
        [
            sys.executable,
            str(PLANNER),
            "--adapter",
            str(ADAPTER),
            "--handoff-jsonl",
            str(handoff),
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["schema_version"] == "local-source-adapter-plan.v1"
    assert payload["candidate_count"] == 2
    assert payload["handoff_record_count"] == 2
    assert payload["handoff_validation"]["ok"] is True
    assert [entry["relative_path"] for entry in payload["candidates"]] == [
        "oversize/big.pdf",
        "prompt_notes.pdf",
    ]
    return handoff, payload


def run_executor(tmp_path: Path, *, handoff: Path) -> subprocess.CompletedProcess[str]:
    output = tmp_path / "local-source-execution"
    return subprocess.run(
        [
            sys.executable,
            str(EXECUTOR),
            "--handoff",
            str(handoff),
            "--output",
            str(output),
            "--mode",
            "local",
            "--run-id",
            "local-source-execution",
            "--created-at",
            "2026-06-03T12:34:56Z",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_execute_local_source_emits_valid_artifacts_for_text_and_oversize_inputs(
    tmp_path: Path,
) -> None:
    handoff, plan = run_planner(tmp_path)
    output = tmp_path / "local-source-execution"

    proc = subprocess.run(
        [
            sys.executable,
            str(EXECUTOR),
            "--handoff",
            str(handoff),
            "--output",
            str(output),
            "--mode",
            "local",
            "--run-id",
            "local-source-execution",
            "--created-at",
            "2026-06-03T12:34:56Z",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == source_executor.EXIT_STATE_UNSAFE, proc.stdout + proc.stderr

    execution = json.loads((output / "execution-record.json").read_text(encoding="utf-8"))
    captures = load_jsonl(output / "capture-events.jsonl")
    extractions = load_jsonl(output / "extraction-records.jsonl")

    assert execution["schema_version"] == "source-acquisition-execution.v1"
    assert execution["adapter_type"] == "local_source"
    assert execution["status"] == "failed"
    assert execution["dry_run"] is False
    assert execution["network_access_attempted"] is False
    assert execution["local_input_paths_processed"] == [
        str(FIXTURE_ROOT / "corpus" / "oversize" / "big.pdf"),
        str(FIXTURE_ROOT / "corpus" / "prompt_notes.pdf"),
    ]
    assert execution["capture_event_count"] == 2
    assert execution["extraction_record_count"] == 2
    assert [action["action_kind"] for action in execution["planned_actions"]] == [
        "read_local_source",
        "read_local_source",
    ]
    assert [action["relative_path"] for action in execution["planned_actions"]] == [
        record["relative_path"] for record in plan["handoff_records"]
    ]
    assert [action["sequence"] for action in execution["planned_actions"]] == [1, 2]
    assert (output / "execution-record.json").read_bytes() == canonical_json_bytes(execution)
    assert (output / "capture-events.jsonl").read_bytes() == canonical_jsonl_bytes(captures)
    assert (output / "extraction-records.jsonl").read_bytes() == canonical_jsonl_bytes(extractions)
    assert (output / "manifest.json").exists()

    assert [capture["source_reference"]["relative_path"] for capture in captures] == [
        "oversize/big.pdf",
        "prompt_notes.pdf",
    ]
    assert [capture["status"] for capture in captures] == ["completed", "completed"]
    assert captures[0]["capture_method"] == "local_directory_walk"
    assert captures[0]["byte_count"] == (FIXTURE_ROOT / "corpus" / "oversize" / "big.pdf").stat().st_size
    assert captures[0]["content_hash"] == hashlib.sha256(
        (FIXTURE_ROOT / "corpus" / "oversize" / "big.pdf").read_bytes()
    ).hexdigest()
    assert captures[1]["content_hash"] == hashlib.sha256(
        (FIXTURE_ROOT / "corpus" / "prompt_notes.pdf").read_bytes()
    ).hexdigest()

    assert extractions[0]["status"] == "skipped"
    assert extractions[0]["failure_reason"] == "oversized_payload"
    assert extractions[0]["extracted_text_path"] is None
    assert extractions[1]["status"] == "completed"
    assert extractions[1]["failure_reason"] is None
    assert extractions[1]["extracted_text_path"] == "extracted-text/extraction-0002.txt"
    assert (output / extractions[1]["extracted_text_path"]).read_text(encoding="utf-8") == (
        FIXTURE_ROOT / "corpus" / "prompt_notes.pdf"
    ).read_text(encoding="utf-8")
    assert extractions[1]["byte_count_out"] == len(
        (FIXTURE_ROOT / "corpus" / "prompt_notes.pdf").read_text(encoding="utf-8").encode("utf-8")
    )

    validator_proc = subprocess.run(
        [sys.executable, str(VALIDATOR), str(output)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert validator_proc.returncode == 0, validator_proc.stdout + validator_proc.stderr


def test_execute_local_source_replaces_stale_artifacts_on_reuse(tmp_path: Path) -> None:
    handoff, _ = run_planner(tmp_path)
    output = tmp_path / "local-source-execution"

    first_proc = run_executor(tmp_path, handoff=handoff)
    assert first_proc.returncode == source_executor.EXIT_STATE_UNSAFE, first_proc.stdout + first_proc.stderr
    stale_artifact = output / "extracted-text" / "extraction-0002.txt"
    assert stale_artifact.exists()

    single_handoff = tmp_path / "local-source-handoff-one.jsonl"
    single_handoff.write_text(handoff.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")

    second_proc = run_executor(tmp_path, handoff=single_handoff)
    assert second_proc.returncode == source_executor.EXIT_STATE_UNSAFE, second_proc.stdout + second_proc.stderr
    assert not stale_artifact.exists()
    assert (output / "manifest.json").exists()


def test_publish_output_dir_swaps_staged_tree_into_place(tmp_path: Path) -> None:
    output = tmp_path / "local-source-execution"
    output.mkdir()
    (output / "stale.txt").write_text("stale\n", encoding="utf-8")

    staging = tmp_path / ".local-source-execution.staging"
    staging.mkdir()
    (staging / "manifest.json").write_text("fresh\n", encoding="utf-8")
    (staging / "extracted-text").mkdir()
    (staging / "extracted-text" / "extraction-0001.txt").write_text("fresh\n", encoding="utf-8")

    source_executor.publish_output_dir(staging, output)

    assert (output / "manifest.json").read_text(encoding="utf-8") == "fresh\n"
    assert (output / "extracted-text" / "extraction-0001.txt").read_text(encoding="utf-8") == "fresh\n"
    assert not (output / "stale.txt").exists()
    assert not staging.exists()


def test_compute_git_snapshot_hash_is_order_insensitive() -> None:
    left = source_executor.compute_git_snapshot_hash(
        [
            {"relative_path": "b.txt", "content_hash": "sha256:b", "byte_count": 2},
            {"relative_path": "a.txt", "content_hash": "sha256:a", "byte_count": 1},
        ],
        git_ref="refs/heads/main",
        git_commit="abc123",
    )
    right = source_executor.compute_git_snapshot_hash(
        [
            {"relative_path": "a.txt", "content_hash": "sha256:a", "byte_count": 1},
            {"relative_path": "b.txt", "content_hash": "sha256:b", "byte_count": 2},
        ],
        git_ref="refs/heads/main",
        git_commit="abc123",
    )

    assert left == right


def test_compute_git_snapshot_hash_is_stable_for_candidate_path_order() -> None:
    left = source_executor.compute_git_snapshot_hash(
        [
            {"relative_path": "nested/data.json", "content_hash": "sha256:b", "byte_count": 2},
            {"relative_path": "tracked.md", "content_hash": "sha256:a", "byte_count": 1},
        ],
        git_ref="refs/heads/main",
        git_commit="abc123",
    )
    right = source_executor.compute_git_snapshot_hash(
        [
            {"relative_path": "tracked.md", "content_hash": "sha256:a", "byte_count": 1},
            {"relative_path": "nested/data.json", "content_hash": "sha256:b", "byte_count": 2},
        ],
        git_ref="refs/heads/main",
        git_commit="abc123",
    )

    assert left == right


def test_normalize_created_at_rejects_invalid_timestamp() -> None:
    with pytest.raises(source_executor.SourceAcquisitionError, match="created_at must be an RFC3339 date-time"):
        source_executor.normalize_created_at("not-a-timestamp")
