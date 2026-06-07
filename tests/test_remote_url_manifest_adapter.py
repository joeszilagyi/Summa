from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "plan_remote_url_manifest_adapter.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "source_adapter_runtime" / "remote_url_manifest"


def run_planner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_remote_url_manifest_plans_entries_without_network_access(tmp_path: Path) -> None:
    adapter_path = FIXTURE_ROOT / "source_adapter.json"
    manifest_jsonl = FIXTURE_ROOT / "manifest.jsonl"
    handoff_jsonl = tmp_path / "handoff.jsonl"
    input_paths = [adapter_path, manifest_jsonl]
    mtimes_before = {path: path.stat().st_mtime_ns for path in input_paths}

    proc = run_planner(
        [
            "--adapter",
            str(adapter_path),
            "--manifest-jsonl",
            str(manifest_jsonl),
            "--handoff-jsonl",
            str(handoff_jsonl),
            "--format",
            "json",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert {path: path.stat().st_mtime_ns for path in input_paths} == mtimes_before
    payload = json.loads(proc.stdout)
    assert payload["schema_version"] == "remote-url-manifest-plan.v1"
    assert payload["accepted_entry_count"] == 2
    assert payload["rejected_entry_count"] == 0
    assert payload["blocker_count"] == 0
    assert payload["network_access_attempted"] is False
    assert payload["remote_state"] == "configured_remote"
    assert all(entry["remote_state"] == "configured_remote" for entry in payload["accepted_entries"])
    assert all(entry["network_access_attempted"] is False for entry in payload["accepted_entries"])

    records = [json.loads(line) for line in handoff_jsonl.read_text(encoding="utf-8").splitlines()]
    assert records == payload["handoff_records"]
    first_record = records[0]
    assert first_record["schema_version"] == "source-adapter-handoff.v1"
    assert first_record["remote_state"] == "configured_remote"
    assert first_record["network_access_attempted"] is False
    assert first_record["source_specific"] == {
        "manifest_url": "https://archives.example.gov/subject/alpha/manifest.jsonl"
    }
    assert first_record["source_identity"] == {
        "manifest_url": "https://archives.example.gov/subject/alpha/manifest.jsonl",
        "manifest_snapshot": {
            "path": str(manifest_jsonl),
            "sha256": hashlib.sha256(manifest_jsonl.read_bytes()).hexdigest(),
        },
        "manifest_line": 1,
        "entry_url": "https://archives.example.gov/subject/alpha/entry-001",
    }


def test_remote_url_manifest_rejects_invalid_row_urls_and_reports_blockers(tmp_path: Path) -> None:
    adapter_path = FIXTURE_ROOT / "source_adapter.json"
    manifest_jsonl = tmp_path / "invalid.jsonl"
    manifest_jsonl.write_text(
        '{"url":"ftp://bad.example.org/file.pdf"}\n'
        '{"title":"missing url"}\n',
        encoding="utf-8",
    )

    proc = run_planner(
        [
            "--adapter",
            str(adapter_path),
            "--manifest-jsonl",
            str(manifest_jsonl),
            "--format",
            "json",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["accepted_entry_count"] == 0
    assert payload["rejected_entry_count"] == 2
    assert payload["blockers"] == ["no valid URL manifest entries were accepted"]
    assert payload["rejected_entries"] == [
        {"line_number": 1, "reason": "url must be an absolute http or https URL"},
        {"line_number": 2, "reason": "url must be an absolute http or https URL"},
    ]


def test_remote_url_manifest_normalizes_entry_urls(tmp_path: Path) -> None:
    adapter_path = FIXTURE_ROOT / "source_adapter.json"
    manifest_jsonl = tmp_path / "manifest.jsonl"
    manifest_jsonl.write_text('{"url":"HTTPS://Archives.Example.Gov:443/sample/path?x=1"}\n', encoding="utf-8")

    proc = run_planner(
        [
            "--adapter",
            str(adapter_path),
            "--manifest-jsonl",
            str(manifest_jsonl),
            "--format",
            "json",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["accepted_entry_count"] == 1
    assert payload["accepted_entries"][0]["url"] == "https://archives.example.gov/sample/path?x=1"


def test_remote_url_manifest_rejects_entry_urls_with_spaces(tmp_path: Path) -> None:
    adapter_path = FIXTURE_ROOT / "source_adapter.json"
    manifest_jsonl = tmp_path / "invalid.jsonl"
    manifest_jsonl.write_text('{"url":"https://exa mple.com/unsafe"}\n', encoding="utf-8")

    proc = run_planner(
        [
            "--adapter",
            str(adapter_path),
            "--manifest-jsonl",
            str(manifest_jsonl),
            "--format",
            "json",
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["accepted_entry_count"] == 0
    assert payload["rejected_entries"] == [
        {"line_number": 1, "reason": "url must be an absolute http or https URL"}
    ]
