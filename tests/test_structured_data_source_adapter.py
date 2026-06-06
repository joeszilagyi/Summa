from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "plan_structured_data_source_adapter.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "source_adapter_runtime" / "structured_data"


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


def write_adapter(
    path: Path,
    *,
    input_family: str,
    local_path: str,
    format_hint: str | None = None,
    record_path: str | None = None,
) -> Path:
    locator: dict[str, object] = {"local_path": local_path}
    if format_hint is not None:
        locator["format_hint"] = format_hint
    if record_path is not None:
        locator["record_path"] = record_path

    adapter_path = path / "source_adapter.json"
    adapter_path.write_text(
        json.dumps(
            {
                "schema_version": "source-adapter.v1",
                "adapter_id": "runtime_structured_data",
                "display_name": "Runtime structured data",
                "workspace_id": "alpha_subject",
                "description": "Runtime fixture for structured-data planning.",
                "input_family": input_family,
                "locator": locator,
                "content_profile": {
                    "content_kinds": ["structured_data", "json", "jsonl", "csv", "xml"],
                    "hazard_flags": ["prompt_injection_text"],
                },
                "provenance": {
                    "discovery_provenance": "runtime fixture corpus",
                    "acquisition_method": "manual_drop",
                    "source_description": "Local structured-data runtime fixtures.",
                },
                "rights_and_storage": {
                    "payload_storage_policy_class": "private_only",
                    "metadata_storage_policy_class": "tracked_derived",
                    "rights_posture": "private_local_only",
                },
                "automation_posture": "operator_review_required",
                "normalized_handoff": {
                    "record_family": "source_lead",
                    "batch_unit": "per_record",
                    "preserve_fields": [
                        "original_locator",
                        "discovery_provenance",
                        "rights_posture",
                        "byte_retention_status",
                        "discard_metadata",
                        "refetchability_status",
                        "transform_lineage",
                        "source_metadata",
                    ],
                    "source_specific_fields": [
                        "relative_path",
                        "source_filename",
                        "structured_format",
                        "record_locator",
                        "record_kind",
                    ],
                },
                "transform_lineage": [
                    {
                        "step_id": "parse",
                        "step_kind": "parse_structured_data",
                        "description": "Parse structured local source inputs without retaining raw payload.",
                        "deterministic": True,
                        "review_required": False,
                    },
                    {
                        "step_id": "handoff",
                        "step_kind": "emit_handoff",
                        "description": "Emit one source-lead handoff record per parsed structured record.",
                        "deterministic": True,
                        "review_required": True,
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return adapter_path


def test_structured_data_directory_plans_csv_json_jsonl_and_xml_without_payload_leakage(tmp_path: Path) -> None:
    adapter_path = write_adapter(tmp_path, input_family="local_directory", local_path=str(FIXTURE_ROOT))
    handoff_jsonl = tmp_path / "handoff.jsonl"
    input_paths = sorted(path for path in FIXTURE_ROOT.rglob("*"))
    input_paths.append(adapter_path)
    tree_before = sorted(path.relative_to(FIXTURE_ROOT).as_posix() for path in input_paths if path != adapter_path)
    snapshot_before = snapshot_paths(input_paths)

    proc = run_planner(["--adapter", str(adapter_path), "--handoff-jsonl", str(handoff_jsonl), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sorted(path.relative_to(FIXTURE_ROOT).as_posix() for path in input_paths if path != adapter_path) == tree_before
    assert snapshot_paths(input_paths) == snapshot_before

    payload = json.loads(proc.stdout)
    assert payload["schema_version"] == "structured-data-source-adapter-plan.v1"
    assert payload["source_count"] == 6
    assert payload["parsed_record_count"] == 10
    assert payload["parse_error_count"] == 1
    assert payload["handoff_record_count"] == 10
    assert payload["handoff_validation"]["ok"] is True
    assert any(entry["reason"] == "unsupported_format" for entry in payload["skipped_entries"])
    assert {entry["structured_format"] for entry in payload["parsed_sources"]} == {"csv", "json", "jsonl", "xml"}
    assert [entry["relative_path"] for entry in payload["parsed_sources"]] == [
        "invalid.jsonl",
        "nested_records.json",
        "records.csv",
        "records.json",
        "records.jsonl",
        "records.xml",
    ]
    assert [entry["structured_format"] for entry in payload["parsed_sources"]] == [
        "jsonl",
        "json",
        "csv",
        "json",
        "jsonl",
        "xml",
    ]

    records = [json.loads(line) for line in handoff_jsonl.read_text(encoding="utf-8").splitlines()]
    assert records == payload["handoff_records"]
    assert [record["sequence"] for record in records] == list(range(1, 11))
    assert {record["source_specific"]["structured_format"] for record in records} == {"csv", "json", "jsonl", "xml"}
    assert all("record_locator" in record["source_specific"] for record in records)
    assert all(record["preserved"]["refetchability_status"] == "local_replayable" for record in records)

    combined_output = proc.stdout + handoff_jsonl.read_text(encoding="utf-8")
    for secret in (
        "SECRET-CSV-ALPHA",
        "SECRET-JSON-ONE",
        "SECRET-JSONL-ONE",
        "SECRET-XML-ONE",
        "SECRET-NESTED-ONE",
    ):
        assert secret not in combined_output


def test_structured_data_directory_rejects_symlink_root(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    data_path = source_root / "records.json"
    data_path.write_text("[{\"id\": 1}]\n", encoding="utf-8")
    symlink_root = tmp_path / "symlink-root"
    symlink_root.symlink_to(source_root, target_is_directory=True)
    adapter_path = write_adapter(tmp_path, input_family="local_directory", local_path=str(symlink_root))

    proc = run_planner(["--adapter", str(adapter_path), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["blocker_count"] == 1
    assert payload["blockers"][0].startswith("local directory root is a symlink")
    assert payload["source_count"] == 0


def test_structured_data_csv_preserves_quoted_newlines_and_commas(tmp_path: Path) -> None:
    data_path = tmp_path / "quoted.csv"
    data_path.write_text(
        'name,notes\nAlice,"line 1\nline 2"\nBob,"comma, quote ""here"""\n',
        encoding="utf-8",
    )
    adapter_path = write_adapter(tmp_path, input_family="local_file", local_path=str(data_path), format_hint="csv")

    proc = run_planner(["--adapter", str(adapter_path), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["parsed_record_count"] == 2
    assert payload["parse_error_count"] == 0
    assert [record["source_specific"]["record_locator"] for record in payload["handoff_records"]] == ["row:1", "row:2"]
    assert [record["source_specific"]["source_filename"] for record in payload["handoff_records"]] == ["quoted.csv", "quoted.csv"]


def test_structured_data_json_duplicate_keys_scalar_roots_and_blank_jsonl_lines(tmp_path: Path) -> None:
    duplicate_json = tmp_path / "duplicate.json"
    duplicate_json.write_text('{"alpha": 1, "alpha": 2}\n', encoding="utf-8")
    scalar_json = tmp_path / "scalar.json"
    scalar_json.write_text('42\n', encoding="utf-8")
    jsonl_path = tmp_path / "blank-lines.jsonl"
    jsonl_path.write_text('{"first": 1}\n\n{"second": 2}\n{"third": 3, "third": 4}\n', encoding="utf-8")

    (tmp_path / "duplicate").mkdir()
    duplicate_adapter = write_adapter(
        tmp_path / "duplicate",
        input_family="local_file",
        local_path=str(duplicate_json),
        format_hint="json",
    )
    (tmp_path / "scalar").mkdir()
    scalar_adapter = write_adapter(
        tmp_path / "scalar",
        input_family="local_file",
        local_path=str(scalar_json),
        format_hint="json",
    )
    (tmp_path / "jsonl").mkdir()
    jsonl_adapter = write_adapter(
        tmp_path / "jsonl",
        input_family="local_file",
        local_path=str(jsonl_path),
        format_hint="jsonl",
    )

    duplicate_proc = run_planner(["--adapter", str(duplicate_adapter), "--format", "json"])
    scalar_proc = run_planner(["--adapter", str(scalar_adapter), "--format", "json"])
    jsonl_proc = run_planner(["--adapter", str(jsonl_adapter), "--format", "json"])

    assert duplicate_proc.returncode == 0, duplicate_proc.stdout + duplicate_proc.stderr
    duplicate_payload = json.loads(duplicate_proc.stdout)
    assert duplicate_payload["parse_error_count"] == 1
    assert "duplicate JSON object key" in duplicate_payload["parse_errors"][0]["reason"]
    assert duplicate_payload["handoff_record_count"] == 0

    assert scalar_proc.returncode == 0, scalar_proc.stdout + scalar_proc.stderr
    scalar_payload = json.loads(scalar_proc.stdout)
    assert scalar_payload["parsed_record_count"] == 1
    assert scalar_payload["handoff_records"][0]["source_specific"]["record_kind"] == "scalar"
    assert scalar_payload["handoff_records"][0]["source_specific"]["record_locator"] == "object:1"

    assert jsonl_proc.returncode == 0, jsonl_proc.stdout + jsonl_proc.stderr
    jsonl_payload = json.loads(jsonl_proc.stdout)
    assert jsonl_payload["parsed_record_count"] == 2
    assert jsonl_payload["parse_error_count"] == 1
    assert [entry["context"] for entry in jsonl_payload["parse_errors"]] == ["line:4"]
    assert [record["source_specific"]["record_locator"] for record in jsonl_payload["handoff_records"]] == [
        "line:1",
        "line:3",
    ]


def test_structured_data_xml_namespaces_and_repeated_children_are_selected(tmp_path: Path) -> None:
    xml_path = tmp_path / "namespaced.xml"
    xml_path.write_text(
        '<root xmlns="urn:test"><entry><value>one</value></entry><entry><value>two</value></entry></root>\n',
        encoding="utf-8",
    )
    adapter_path = write_adapter(
        tmp_path,
        input_family="local_file",
        local_path=str(xml_path),
        format_hint="xml",
        record_path=".//{urn:test}entry",
    )

    proc = run_planner(["--adapter", str(adapter_path), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["parsed_record_count"] == 2
    assert payload["parse_error_count"] == 0
    assert payload["handoff_validation"]["ok"] is True
    assert [record["source_specific"]["record_kind"] for record in payload["handoff_records"]] == ["element", "element"]
    assert all(record["source_specific"]["record_locator"].startswith("/{urn:test}root[1]") for record in payload["handoff_records"])


def test_structured_data_local_file_honors_record_path_hint(tmp_path: Path) -> None:
    adapter_path = write_adapter(
        tmp_path,
        input_family="local_file",
        local_path=str(FIXTURE_ROOT / "nested_records.json"),
        format_hint="json",
        record_path="records",
    )

    proc = run_planner(["--adapter", str(adapter_path), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["parsed_record_count"] == 2
    assert payload["parse_error_count"] == 0
    assert payload["record_path"] == "records"
    assert [record["source_specific"]["record_locator"] for record in payload["handoff_records"]] == ["index:1", "index:2"]
    assert all(record["source_specific"]["source_filename"] == "nested_records.json" for record in payload["handoff_records"])


def test_structured_data_reports_malformed_jsonl_with_line_context_and_no_payload_leakage(tmp_path: Path) -> None:
    adapter_path = write_adapter(
        tmp_path,
        input_family="local_file",
        local_path=str(FIXTURE_ROOT / "invalid.jsonl"),
        format_hint="jsonl",
    )
    handoff_jsonl = tmp_path / "handoff.jsonl"

    proc = run_planner(["--adapter", str(adapter_path), "--handoff-jsonl", str(handoff_jsonl), "--format", "json"])

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["parsed_record_count"] == 1
    assert payload["parse_error_count"] == 1
    assert payload["blocker_count"] == 0
    assert payload["parse_errors"][0]["relative_path"] == "invalid.jsonl"
    assert payload["parse_errors"][0]["context"].startswith("line:2,")
    assert payload["parse_errors"][0]["reason"]

    combined_output = proc.stdout + handoff_jsonl.read_text(encoding="utf-8")
    assert "SECRET-JSONL-GOOD" not in combined_output
    assert "SECRET-JSONL-BAD" not in combined_output
