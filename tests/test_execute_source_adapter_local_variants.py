from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from tools.scripts import execute_source_adapter as source_executor

REPO_ROOT = Path(__file__).resolve().parents[1]
EXECUTOR = REPO_ROOT / "tools" / "scripts" / "execute_source_adapter.py"
STRUCTURED_FIXTURE_ROOT = (
    REPO_ROOT / "tests" / "fixtures" / "source_adapter_runtime" / "hostile_replay" / "structured_data"
)
ADAPTER = STRUCTURED_FIXTURE_ROOT / "source_adapter.json"


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def canonical_jsonl_bytes(records: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records).encode(
        "utf-8"
    )


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def materialize_handoff(template_path: Path, output_path: Path) -> Path:
    template = template_path.read_text(encoding="utf-8")
    template = template.replace("<repo-root>", str(REPO_ROOT))
    template = template.replace("<emitted-at>", "2026-06-03T12:34:56Z")
    output_path.write_text(template, encoding="utf-8")
    return output_path


def run_executor(*, handoff: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(EXECUTOR),
            "--handoff",
            str(handoff),
            "--adapter",
            str(ADAPTER),
            "--output",
            str(output),
            "--mode",
            "local",
            "--run-id",
            output.name,
            "--created-at",
            "2026-06-03T12:34:56Z",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_execute_structured_data_branch_emits_artifacts_for_hostile_replay_fixture(
    tmp_path: Path,
) -> None:
    handoff = materialize_handoff(
        STRUCTURED_FIXTURE_ROOT / "expected_handoff.jsonl",
        tmp_path / "structured-data-handoff.jsonl",
    )
    output = tmp_path / "structured-data-execution"

    proc = run_executor(handoff=handoff, output=output)

    assert proc.returncode == source_executor.EXIT_STATE_UNSAFE, proc.stdout + proc.stderr

    execution = json.loads((output / "execution-record.json").read_text(encoding="utf-8"))
    captures = load_jsonl(output / "capture-events.jsonl")
    extractions = load_jsonl(output / "extraction-records.jsonl")
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

    assert execution["schema_version"] == "source-acquisition-execution.v1"
    assert execution["adapter_type"] == "structured_data"
    assert execution["status"] == "failed"
    assert execution["network_access_attempted"] is False
    assert execution["capture_event_count"] == 3
    assert execution["extraction_record_count"] == 4
    assert execution["local_input_paths_processed"] == [
        str(STRUCTURED_FIXTURE_ROOT / "corpus" / "injection.jsonl"),
        str(STRUCTURED_FIXTURE_ROOT / "corpus" / "markup.xml"),
        str(STRUCTURED_FIXTURE_ROOT / "corpus" / "oversize.json"),
    ]

    assert [capture["capture_method"] for capture in captures] == ["structured_data_load"] * 3
    assert [extraction["extraction_method"] for extraction in extractions] == ["structured_record_extract"] * 4
    assert extractions[-1]["status"] == "skipped"
    assert extractions[-1]["failure_reason"] == "oversized_payload"
    assert extractions[-1]["extracted_text_path"] is None
    assert extractions[0]["status"] == "completed"
    assert extractions[0]["structured_format"] == "jsonl"
    assert extractions[0]["record_locator"] == "line:1"
    assert (output / "execution-record.json").read_bytes() == canonical_json_bytes(execution)
    assert (output / "capture-events.jsonl").read_bytes() == canonical_jsonl_bytes(captures)
    assert (output / "extraction-records.jsonl").read_bytes() == canonical_jsonl_bytes(extractions)
    assert (output / "manifest.json").read_bytes() == canonical_json_bytes(manifest)


def test_execute_structured_data_reads_each_source_file_once_per_grouped_capture(
    tmp_path: Path, monkeypatch: Any
) -> None:
    handoff = materialize_handoff(
        STRUCTURED_FIXTURE_ROOT / "expected_handoff.jsonl",
        tmp_path / "structured-data-handoff.jsonl",
    )
    adapter_payload = source_executor.load_validated_adapter(ADAPTER)
    records, handoff_hash = source_executor.load_validated_handoff_records(
        handoff, adapter_path=ADAPTER
    )

    source_paths = {
        (STRUCTURED_FIXTURE_ROOT / "corpus" / "injection.jsonl").resolve(),
        (STRUCTURED_FIXTURE_ROOT / "corpus" / "markup.xml").resolve(),
        (STRUCTURED_FIXTURE_ROOT / "corpus" / "oversize.json").resolve(),
    }
    read_counts = {path: 0 for path in source_paths}
    original_open = Path.open

    def count_if_source(path: Path) -> None:
        resolved = path.resolve()
        if resolved in read_counts:
            read_counts[resolved] += 1

    def guarded_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        count_if_source(self)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open, raising=True)

    capture_events, extraction_records, text_artifacts, local_paths, failed = (
        source_executor.execute_structured_data(
            records=records,
            adapter_payload=adapter_payload,
            adapter_path=ADAPTER,
            run_id="structured-data-execution",
            created_at="2026-06-03T12:34:56Z",
            handoff_hash=handoff_hash,
        )
    )

    assert failed is True
    assert local_paths == [
        str(STRUCTURED_FIXTURE_ROOT / "corpus" / "injection.jsonl"),
        str(STRUCTURED_FIXTURE_ROOT / "corpus" / "markup.xml"),
        str(STRUCTURED_FIXTURE_ROOT / "corpus" / "oversize.json"),
    ]
    assert [capture["capture_method"] for capture in capture_events] == ["structured_data_load"] * 3
    assert [extraction["extraction_method"] for extraction in extraction_records] == [
        "structured_record_extract"
    ] * 4
    assert text_artifacts["extracted-text/extraction-0001.txt"].startswith("{\n")
    assert text_artifacts["extracted-text/extraction-0002.txt"].startswith("{\n")
    assert text_artifacts["extracted-text/extraction-0003.txt"].startswith("<")
    assert extraction_records[-1]["status"] == "skipped"
    assert extraction_records[-1]["failure_reason"] == "oversized_payload"
    assert all(count == 1 for count in read_counts.values())
