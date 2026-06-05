from __future__ import annotations

from pathlib import Path

from tools.scripts import execute_source_adapter, plan_structured_data_source_adapter


def test_plan_structured_data_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"name":"Alice","name":"Bob"}\n', encoding="utf-8")

    records, errors = plan_structured_data_source_adapter.parse_json_records(path, record_path=None)

    assert records == []
    assert errors == [{"context": "line:1", "reason": "duplicate JSON object key: name"}]


def test_plan_structured_data_rejects_duplicate_jsonl_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.jsonl"
    path.write_text('{"name":"Alice","name":"Bob"}\n', encoding="utf-8")

    records, errors = plan_structured_data_source_adapter.parse_jsonl_records(path)

    assert records == []
    assert errors == [{"context": "line:1", "reason": "duplicate JSON object key: name"}]


def test_plan_structured_data_rejects_duplicate_csv_headers(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.csv"
    path.write_text("id,id\n1,2\n", encoding="utf-8")

    records, errors = plan_structured_data_source_adapter.parse_csv_records(path)

    assert records == []
    assert errors == [{"context": "line:1", "reason": "duplicate CSV header"}]


def test_executor_rejects_duplicate_json_and_jsonl_keys(tmp_path: Path) -> None:
    json_path = tmp_path / "duplicate.json"
    json_path.write_text('{"name":"Alice","name":"Bob"}\n', encoding="utf-8")
    jsonl_path = tmp_path / "duplicate.jsonl"
    jsonl_path.write_text('{"name":"Alice","name":"Bob"}\n', encoding="utf-8")

    json_records, json_errors = execute_source_adapter.load_json_record_map(json_path, record_path=None)
    jsonl_records, jsonl_errors = execute_source_adapter.load_jsonl_record_map(jsonl_path)

    assert json_records == {}
    assert json_errors == [{"context": "line:1", "reason": "duplicate JSON object key: name"}]
    assert jsonl_records == {}
    assert jsonl_errors == [{"context": "line:1", "reason": "duplicate JSON object key: name"}]


def test_executor_rejects_duplicate_csv_headers(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.csv"
    path.write_text("id,id\n1,2\n", encoding="utf-8")

    records, errors = execute_source_adapter.load_csv_row_map(path)

    assert records == {}
    assert errors == [{"context": "line:1", "reason": "duplicate CSV header"}]
