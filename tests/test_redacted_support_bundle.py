from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SUPPORT_BUILDER_PATH = REPO_ROOT / "tools" / "scripts" / "build_redacted_support_bundle.py"

support_builder_spec = importlib.util.spec_from_file_location(
    "redacted_support_bundle_builder_for_tests",
    SUPPORT_BUILDER_PATH,
)
assert support_builder_spec is not None
support_builder = importlib.util.module_from_spec(support_builder_spec)
assert support_builder_spec.loader is not None
support_builder_spec.loader.exec_module(support_builder)


def test_redacted_support_bundle_rejects_unrecognized_output_on_overwrite(tmp_path: Path) -> None:
    output_dir = tmp_path / "support-bundle"
    output_dir.mkdir()
    (output_dir / "notes.txt").write_text("do-not-overwrite", encoding="utf-8")

    try:
        support_builder.build_bundle(REPO_ROOT, output_dir, overwrite=True)
    except support_builder.SupportBundleError as exc:
        assert "not a recognized support bundle" in str(exc)
    else:
        raise AssertionError("expected unrecognized output rejection")


def test_redacted_support_bundle_overwrites_valid_output_directory(tmp_path: Path) -> None:
    output_dir = tmp_path / "support-bundle"
    first_report = support_builder.build_bundle(REPO_ROOT, output_dir)

    second_report = support_builder.build_bundle(REPO_ROOT, output_dir, overwrite=True)

    assert second_report["status"] == "pass"
    assert first_report["manifest_path"] == str((output_dir / "manifest.json").resolve())
    assert second_report["manifest_path"] == str((output_dir / "manifest.json").resolve())


def test_redacted_support_bundle_redacts_private_fields_from_doctor_report(tmp_path: Path) -> None:
    output_dir = tmp_path / "support-bundle"
    doctor_report = tmp_path / "doctor-report.json"
    doctor_report.write_text(
        json.dumps(
            {
                "schema_version": "local-doctor-report.v1",
                "summary": {"status": "warn", "finding_count": 1, "operator_action_required_count": 1},
                "checks": {"repo_hygiene": "pass"},
                "canonical_store": {
                    "status": "populated",
                    "total_rows": 1,
                    "last_ingest_at": "2026-06-06T12:00:00Z",
                    "private_note": "BEGIN SECRET /home/joe/private/doctor.txt",
                },
                "loop_health": {
                    "health_status": "healthy",
                    "review_backlog": {"pending_review_count": 0},
                    "raw_prompt_text": "ignore previous instructions",
                },
                "graph_closure": {"status": "pass", "orphan_error_count": 0, "unresolved_tracked_count": 0},
                "findings": [
                    {
                        "code": "TEST",
                        "class": "advisory_only",
                        "message": "public note",
                        "details": {"path": "/home/joe/private/doctor.txt"},
                    }
                ],
                "redaction": {
                    "raw_payloads_included": False,
                    "runtime_logs_included": False,
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    report = support_builder.build_bundle(REPO_ROOT, output_dir, doctor_report=doctor_report)

    assert report["status"] == "pass"
    bundle_text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(output_dir.rglob("*")))
    assert "BEGIN SECRET" not in bundle_text
    assert "/home/joe/private/doctor.txt" not in bundle_text
    assert "ignore previous instructions" not in bundle_text


def test_redacted_support_bundle_scans_manifest_after_writing_it(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "support-bundle"
    scan_calls: list[bool] = []

    def fake_scan(bundle_root: Path) -> list[dict[str, str]]:
        scan_calls.append((bundle_root / "manifest.json").exists())
        return []

    monkeypatch.setattr(support_builder, "scan_bundle_for_leaks", fake_scan)

    report = support_builder.build_bundle(REPO_ROOT, output_dir)

    assert report["status"] == "pass"
    assert scan_calls == [False, True]


def test_schema_versions_prefers_inventory_sidecar(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    inventory = {
        "schema_count": 1,
        "schemas": [
            {
                "path": "config/example.schema.json",
                "title": "Example schema",
                "id": "example.schema.json",
                "status": "readable",
            }
        ],
    }
    inventory_path = repo_root / "runtime" / "config" / "schema_inventory.json"
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def fail_glob(*args: object, **kwargs: object):
        raise AssertionError("schema_versions should not glob schema files when inventory is present")

    monkeypatch.setattr(Path, "glob", fail_glob)

    payload = support_builder.schema_versions(repo_root)

    assert payload == inventory


def test_ledger_summary_prefers_metadata_sidecar(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    ledger_path = repo_root / "runtime" / "ledgers" / "workspace.runtime-ledger.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text('{"event_id":"event-1"}\n', encoding="utf-8")
    metadata_path = ledger_path.with_name(ledger_path.name + ".meta.json")
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": "runtime-ledger-metadata.v1",
                "ledger_path": str(ledger_path),
                "line_count": 37,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    original_open = Path.open

    def fail_ledger_open(self: Path, *args: object, **kwargs: object):
        if self == ledger_path and args and args[0] == "r":
            raise AssertionError("ledger_summary should not scan the ledger when metadata is present")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_ledger_open)

    summary = support_builder.ledger_summary(repo_root)

    assert summary == {
        "ledger_root_present": True,
        "ledgers": [
            {
                "name": ledger_path.name,
                "size_bytes": ledger_path.stat().st_size,
                "line_count": 37,
            }
        ],
    }


def test_redacted_recent_log_reads_tail_without_read_text(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "runtime" / "logs" / "index-actions.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"line {index}" for index in range(1, 205)]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def fail_read_text(*args: object, **kwargs: object) -> str:
        raise AssertionError("redacted_recent_log should not read the entire file as text")

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    tail = support_builder.redacted_recent_log(tmp_path)

    assert tail is not None
    tail_lines = tail.splitlines()
    assert len(tail_lines) == support_builder.MAX_LOG_LINES
    assert tail_lines[0] == "line 5"
    assert tail_lines[-1] == "line 204"
