from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "plan_local_source_adapter.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "source_adapter_runtime"


def run_planner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def snapshot_paths(paths: list[Path]) -> dict[Path, tuple[int, str | None, str | None]]:
    snapshot: dict[Path, tuple[int, str | None, str | None]] = {}
    for path in paths:
        if path.is_symlink():
            snapshot[path] = (path.stat().st_size, None, path.readlink().as_posix())
        elif path.is_file():
            snapshot[path] = (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest(), None)
        else:
            snapshot[path] = (path.stat().st_size, None, None)
    return snapshot


def test_local_directory_adapter_plans_candidates_and_handoff_records(tmp_path: Path) -> None:
    adapter_path = FIXTURE_ROOT / "local_directory" / "source_adapter.json"
    handoff_jsonl = tmp_path / "handoff.jsonl"
    input_paths = sorted((FIXTURE_ROOT / "local_directory" / "corpus").rglob("*"))
    input_paths.append(adapter_path)
    tree_before = sorted(path.relative_to(FIXTURE_ROOT).as_posix() for path in input_paths)
    snapshot_before = snapshot_paths(input_paths)

    proc = run_planner(["--adapter", str(adapter_path), "--handoff-jsonl", str(handoff_jsonl), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sorted(path.relative_to(FIXTURE_ROOT).as_posix() for path in input_paths) == tree_before
    assert snapshot_paths(input_paths) == snapshot_before

    payload = json.loads(proc.stdout)
    assert payload["schema_version"] == "local-source-adapter-plan.v1"
    assert payload["candidate_count"] == 3
    assert payload["blocker_count"] == 0
    assert payload["handoff_record_count"] == 3
    assert payload["handoff_validation"]["ok"] is True
    assert [entry["relative_path"] for entry in payload["candidates"]] == [
        "nested/data.json",
        "nested/report.pdf",
        "top/notes.pdf",
    ]
    assert any(entry["reason"] == "excluded" for entry in payload["skipped_entries"])

    records = [json.loads(line) for line in handoff_jsonl.read_text(encoding="utf-8").splitlines()]
    assert records == payload["handoff_records"]
    assert [record["sequence"] for record in records] == [1, 2, 3]
    assert [record["relative_path"] for record in records] == [
        "nested/data.json",
        "nested/report.pdf",
        "top/notes.pdf",
    ]
    first_record = records[0]
    assert first_record["schema_version"] == "source-adapter-handoff.v1"
    assert first_record["record_family"] == "capture"
    assert first_record["source_specific"]["source_filename"]
    assert "relative_path" in first_record["source_specific"]
    assert "rights_posture" in first_record["preserved"]


def test_local_file_adapter_handles_single_file_root(tmp_path: Path) -> None:
    adapter_path = FIXTURE_ROOT / "local_file" / "source_adapter.json"

    proc = run_planner(["--adapter", str(adapter_path), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["candidate_count"] == 1
    assert payload["blocker_count"] == 0
    assert payload["handoff_record_count"] == 1
    record = payload["handoff_records"][0]
    assert record["source_specific"] == {"source_filename": "single.pdf"}


def test_local_directory_adapter_rejects_symlink_root(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "file.txt").write_text("fixture\n", encoding="utf-8")
    symlink_root = tmp_path / "symlink-root"
    symlink_root.symlink_to(source_root, target_is_directory=True)

    adapter_path = tmp_path / "source_adapter.json"
    adapter_path.write_text(
        json.dumps(
            {
                "schema_version": "source-adapter.v1",
                "adapter_id": "runtime_symlink_root",
                "display_name": "Runtime symlink root",
                "workspace_id": "alpha_subject",
                "description": "Fixture adapter with a symlinked directory root.",
                "input_family": "local_directory",
                "locator": {
                    "local_path": str(symlink_root),
                    "include_globs": ["**/*.txt"],
                },
                "content_profile": {
                    "content_kinds": ["text"],
                    "hazard_flags": [],
                },
                "provenance": {
                    "discovery_provenance": "test fixture",
                    "acquisition_method": "manual_drop",
                    "source_description": "Symlinked directory root fixture.",
                },
                "rights_and_storage": {
                    "payload_storage_policy_class": "private_only",
                    "metadata_storage_policy_class": "tracked_derived",
                    "rights_posture": "private_local_only",
                },
                "automation_posture": "operator_review_required",
                "normalized_handoff": {
                    "record_family": "capture",
                    "batch_unit": "per_file",
                    "preserve_fields": [
                        "original_locator",
                        "discovery_provenance",
                        "rights_posture",
                    ],
                    "source_specific_fields": ["relative_path", "source_filename"],
                },
                "transform_lineage": [
                    {
                        "step_id": "handoff",
                        "step_kind": "emit_handoff",
                        "description": "Emit local capture handoff.",
                        "deterministic": True,
                        "review_required": True,
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    proc = run_planner(["--adapter", str(adapter_path), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["blocker_count"] == 1
    assert payload["blockers"][0].startswith("local directory root is a symlink")
    assert payload["candidate_count"] == 0


def test_local_directory_adapter_rejects_symlink_escape_and_keeps_binary_bytes_as_candidates(
    tmp_path: Path,
) -> None:
    corpus_root = tmp_path / "corpus"
    corpus_root.mkdir()
    payload_path = corpus_root / "payload.txt"
    payload_path.write_bytes(b"\x00binary\xffpayload\n")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_secret = outside_dir / "secret.txt"
    outside_secret.write_text("secret\n", encoding="utf-8")
    symlink_path = corpus_root / "escape.txt"
    symlink_path.symlink_to(outside_secret)
    adapter_path = tmp_path / "source_adapter.json"
    adapter_path.write_text(
        json.dumps(
            {
                "schema_version": "source-adapter.v1",
                "adapter_id": "runtime_binary_containment",
                "display_name": "Runtime binary containment",
                "workspace_id": "alpha_subject",
                "description": "Fixture adapter with binary and symlink containment cases.",
                "input_family": "local_directory",
                "locator": {
                    "local_path": "corpus",
                    "include_globs": ["**/*.txt"],
                },
                "content_profile": {
                    "content_kinds": ["text", "binary"],
                    "hazard_flags": [],
                },
                "provenance": {
                    "discovery_provenance": "test fixture",
                    "acquisition_method": "manual_drop",
                    "source_description": "Binary file and symlink containment fixture.",
                },
                "rights_and_storage": {
                    "payload_storage_policy_class": "private_only",
                    "metadata_storage_policy_class": "tracked_derived",
                    "rights_posture": "private_local_only",
                },
                "automation_posture": "operator_review_required",
                "normalized_handoff": {
                    "record_family": "capture",
                    "batch_unit": "per_file",
                    "preserve_fields": [
                        "original_locator",
                        "discovery_provenance",
                        "rights_posture",
                        "transform_lineage",
                    ],
                    "source_specific_fields": ["relative_path", "source_filename"],
                },
                "transform_lineage": [
                    {
                        "step_id": "handoff",
                        "step_kind": "emit_handoff",
                        "description": "Emit local capture handoff.",
                        "deterministic": True,
                        "review_required": True,
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    proc = run_planner(["--adapter", str(adapter_path), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["candidate_count"] == 1
    assert payload["blocker_count"] == 0
    assert payload["skipped_count"] == 1
    assert payload["candidates"] == [
        {
            "resolved_source_path": str(payload_path),
            "relative_path": "payload.txt",
            "size_bytes": payload_path.stat().st_size,
        }
    ]
    assert payload["skipped_entries"][0]["reason"] == "symlink_not_allowed"
    assert payload["handoff_records"][0]["source_specific"] == {
        "relative_path": "payload.txt",
        "source_filename": "payload.txt",
    }


def test_local_directory_adapter_reports_blockers_for_missing_or_unmatched_roots(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus"
    corpus_root.mkdir()
    (corpus_root / "readme.txt").write_text("fixture\n", encoding="utf-8")
    adapter_path = tmp_path / "source_adapter.json"
    adapter_path.write_text(
        json.dumps(
            {
                "schema_version": "source-adapter.v1",
                "adapter_id": "runtime_blocker",
                "display_name": "Runtime blocker",
                "workspace_id": "alpha_subject",
                "description": "Fixture adapter with no matching candidates.",
                "input_family": "local_directory",
                "locator": {
                    "local_path": "corpus",
                    "include_globs": ["**/*.pdf"],
                },
                "content_profile": {
                    "content_kinds": ["pdf"],
                    "hazard_flags": [],
                },
                "provenance": {
                    "discovery_provenance": "test fixture",
                    "acquisition_method": "manual_drop",
                    "source_description": "No matching files.",
                },
                "rights_and_storage": {
                    "payload_storage_policy_class": "private_only",
                    "metadata_storage_policy_class": "tracked_derived",
                    "rights_posture": "private_local_only",
                },
                "automation_posture": "operator_review_required",
                "normalized_handoff": {
                    "record_family": "capture",
                    "batch_unit": "per_file",
                    "preserve_fields": [
                        "original_locator",
                        "discovery_provenance",
                        "rights_posture",
                        "transform_lineage",
                    ],
                    "source_specific_fields": ["relative_path"],
                },
                "transform_lineage": [
                    {
                        "step_id": "handoff",
                        "step_kind": "emit_handoff",
                        "description": "Emit local capture handoff.",
                        "deterministic": True,
                        "review_required": True,
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    proc = run_planner(["--adapter", str(adapter_path), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["candidate_count"] == 0
    assert payload["blocker_count"] == 1
    assert payload["blockers"] == ["no candidate files matched include/exclude globs"]
