from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from jsonschema import validators

from tools.source_db_tools import canonical_store, cycle_evidence_ledger

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "scripts"))
from export_redacted_diagnostics import Redactor, render_text as render_export_text  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "export_redacted_diagnostics.py"
MANIFEST_SCHEMA = REPO_ROOT / "config" / "redacted-diagnostic-manifest.v1.schema.json"
FIXED_TIMESTAMP = "2026-06-04T12:00:00Z"
PRIVATE_SENTINEL = "PRIVATE_SENTINEL_DIAGNOSTIC"


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def table_counts(db_path: Path) -> dict[str, int]:
    conn = canonical_store.connect_existing_read_only(db_path)
    try:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
            for table in sorted(canonical_store.actual_tables(conn))
        }
    finally:
        conn.close()


def build_fixture_store(tmp_path: Path) -> tuple[Path, Path]:
    db_path = tmp_path / "canonical.sqlite"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "artifact-local-path.json").write_text(
        json.dumps(
            {
                "schema_version": "fixture-artifact.v1",
                "local_path": f"/home/operator/{PRIVATE_SENTINEL}/payload.txt",
                "message": f"do not export {PRIVATE_SENTINEL}",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (workspace / "topic-cycle-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "topic-cycle-run.v1",
                "run_id": "run-redacted-fixture",
                "status": "completed",
                "stages": [{"name": "fixture", "status": "passed"}],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.redacted_diagnostics",
    )
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        provenance = canonical_store.record_provenance_event(
            conn,
            object_namespace="fixture",
            object_id="redacted-diagnostics",
            event_type="fixture_ingest",
            actor_type="operator",
            actor_id=f"operator-{PRIVATE_SENTINEL}",
            actor_label=f"Operator {PRIVATE_SENTINEL}",
            tool_name="pytest",
            prompt_id=f"prompt-{PRIVATE_SENTINEL}",
            run_id="run-redacted-fixture",
            event_timestamp=FIXED_TIMESTAMP,
            note_text=f"private note {PRIVATE_SENTINEL}",
            provenance_event_key_v1="prov:redacted-diagnostics",
        )
        work = canonical_store.upsert_work(
            conn,
            work_key_v1="work:redacted-diagnostics",
            work_type="article",
            title=f"Private title {PRIVATE_SENTINEL}",
            provenance_event_ref=provenance.event_key,
            workspace_id="redacted_workspace",
            review_state="needs_review",
            publication_state="private_working",
            public_blocker="private_fixture",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        canonical_store.record_source_access(
            conn,
            provenance_event_ref=provenance.event_key,
            work_id=work.row_id,
            original_locator=f"https://sensitive.example.test/private?token={PRIVATE_SENTINEL}",
            canonical_url=f"https://sensitive.example.test/private?token={PRIVATE_SENTINEL}",
            review_state="needs_review",
            publication_state="private_working",
            public_blocker="private_fixture",
            workspace_id="redacted_workspace",
            record_last_updated=FIXED_TIMESTAMP,
        )
        capture = canonical_store.record_capture_event(
            conn,
            provenance_event_ref=provenance.event_key,
            work_id=work.row_id,
            original_locator=f"file:///home/operator/{PRIVATE_SENTINEL}/payload.txt",
            captured_at=FIXED_TIMESTAMP,
            capture_method="fixture_capture",
            content_hash="a" * 64,
            byte_count=128,
            mime_type="text/plain",
            transient_payload_note=f"payload note {PRIVATE_SENTINEL}",
            review_state="needs_review",
            workspace_id="redacted_workspace",
            record_last_updated=FIXED_TIMESTAMP,
        )
        extraction = canonical_store.record_extraction_record(
            conn,
            provenance_event_ref=provenance.event_key,
            capture_event_id=capture.row_id,
            extraction_method="fixture_extract",
            extraction_status="completed",
            extractor_name="pytest",
            summary_short=f"summary {PRIVATE_SENTINEL}",
            input_hash="a" * 64,
            output_hash="b" * 64,
            byte_count_in=128,
            byte_count_out=64,
            encoding_handling="utf8",
            truncation_status="not_truncated",
            review_state="needs_review",
            workspace_id="redacted_workspace",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        canonical_store.record_source_claim(
            conn,
            provenance_event_ref=provenance.event_key,
            source_claim_key_v1="claim:redacted-diagnostics",
            about_object_ref=f"work:{work.row_id}",
            claim_text=f"claim text {PRIVATE_SENTINEL}",
            public_summary="public-safe structural summary",
            claim_type="fixture_claim",
            review_state="proposed",
            publication_state="private_working",
            public_blocker="private_fixture",
            workspace_id="redacted_workspace",
            capture_event_id=capture.row_id,
            extraction_id=extraction.row_id,
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        canonical_store.record_source_relationship(
            conn,
            provenance_event_ref=provenance.event_key,
            from_object_ref=f"work:{work.row_id}",
            to_object_ref="authority:missing-private",
            predicate="mentions",
            target_label=f"target {PRIVATE_SENTINEL}",
            evidence_note=f"evidence {PRIVATE_SENTINEL}",
            review_state="needs_review",
            publication_state="private_working",
            public_blocker="private_fixture",
            workspace_id="redacted_workspace",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        conn.execute(
            """
            INSERT INTO review_state_history (
              review_state_history_key_v1, target_namespace, target_id,
              previous_state, new_state, changed_by, changed_at, reason, note,
              source_namespace, source_id, source_tool, source_run_id,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "review:redacted-diagnostics",
                "work",
                str(work.row_id),
                "needs_review",
                "needs_review",
                f"reviewer-{PRIVATE_SENTINEL}",
                FIXED_TIMESTAMP,
                "fixture",
                f"private review note {PRIVATE_SENTINEL}",
                "fixture",
                "redacted",
                "pytest",
                "run-redacted-fixture",
                FIXED_TIMESTAMP,
            ),
        )
        cycle_id = cycle_evidence_ledger.record_cycle_event_start(
            conn,
            run_id="run-redacted-fixture",
            workspace_id="redacted_workspace",
            subject_key="redacted_subject",
            started_at=FIXED_TIMESTAMP,
            status="completed",
            topic_cycle_manifest_path=str(workspace / "topic-cycle-manifest.json"),
            canonical_db_ref=str(db_path),
        )
        stage_id = cycle_evidence_ledger.record_cycle_stage_start(
            conn,
            cycle_event_id=cycle_id,
            run_id="run-redacted-fixture",
            stage_name="fixture",
            stage_order=1,
            started_at=FIXED_TIMESTAMP,
            status="passed",
        )
        cycle_evidence_ledger.record_cycle_artifact_ref(
            conn,
            cycle_event_id=cycle_id,
            stage_event_id=stage_id,
            artifact_type="fixture",
            artifact_path=str(workspace / f"{PRIVATE_SENTINEL}-artifact.json"),
            artifact_hash="c" * 64,
            byte_count=42,
            privacy_classification="internal_private",
            public_safe=False,
            created_at=FIXED_TIMESTAMP,
        )
        conn.commit()
    finally:
        conn.close()
    return db_path, workspace


def run_export(
    db_path: Path, workspace: Path, output_dir: Path, *extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--db",
            str(db_path),
            "--workspace",
            str(workspace),
            "--output-dir",
            str(output_dir),
            "--path-redaction",
            "hmac",
            "--url-redaction",
            "domain_only",
            "--redaction-key",
            "fixed-test-redaction-key",
            "--generated-at",
            FIXED_TIMESTAMP,
            "--overwrite",
            *extra,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_help_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "usage:" in (proc.stdout + proc.stderr).lower()


def test_basic_redacted_export_writes_structural_bundle_and_valid_manifest(tmp_path: Path) -> None:
    db_path, workspace = build_fixture_store(tmp_path)
    output_dir = tmp_path / "diagnostics"

    proc = run_export(db_path, workspace, output_dir)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    expected_files = {
        "diagnostic-manifest.json",
        "canonical-summary.json",
        "graph-shape.json",
        "review-state-summary.json",
        "relationship-summary.json",
        "source-access-summary.json",
        "cycle-ledger-summary.json",
        "artifact-summary.json",
        "cycle-summary.json",
        "spool-summary.json",
        "graph-closure-summary.json",
        "redaction-report.json",
        "leak-scan-report.json",
    }
    assert expected_files <= {path.name for path in output_dir.glob("*.json")}

    manifest = load_json(output_dir / "diagnostic-manifest.json")
    schema = load_json(MANIFEST_SCHEMA)
    validator_cls = validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator_cls(schema).validate(manifest)
    assert manifest["redaction_mode"] == "redacted"
    assert manifest["privacy_classification"] == "local_operator_redacted"
    assert manifest["redaction_policy"]["path_redaction"] == "hmac"
    assert manifest["redaction_policy"]["url_redaction"] == "domain_only"
    assert manifest["leak_scan"]["status"] == "pass"


def test_default_export_excludes_private_sentinel_paths_urls_text_and_payload_content(
    tmp_path: Path,
) -> None:
    db_path, workspace = build_fixture_store(tmp_path)
    output_dir = tmp_path / "diagnostics"

    proc = run_export(db_path, workspace, output_dir)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    bundle_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(output_dir.glob("*.json"))
    )
    assert PRIVATE_SENTINEL not in bundle_text
    assert "/home/operator" not in bundle_text
    assert "token=" not in bundle_text
    assert "claim text" not in bundle_text
    assert "private review note" not in bundle_text
    assert "path-hmac:" in bundle_text
    assert "sensitive.example.test" in bundle_text


def test_redactor_handles_absolute_local_paths_for_public_and_private_modes() -> None:
    absolute_path = Path("/home/operator/private fixtures/payload.txt")
    public_redactor = Redactor(
        path_mode="hmac",
        url_mode="domain_only",
        key="fixed-test-redaction-key",
        internal_full_fidelity=False,
    )
    private_redactor = Redactor(
        path_mode="hmac",
        url_mode="domain_only",
        key="fixed-test-redaction-key",
        internal_full_fidelity=True,
    )

    public_path = public_redactor.redact_path(absolute_path)
    public_json = public_redactor.redact_json({"local_path": str(absolute_path)})

    assert public_path is not None
    assert public_path.startswith("path-hmac:")
    assert str(absolute_path) not in public_path
    assert public_json["local_path"] == public_path
    assert private_redactor.redact_path(absolute_path) == str(absolute_path)


def test_redactor_strips_terminal_escape_sequences() -> None:
    redactor = Redactor(
        path_mode="hmac",
        url_mode="domain_only",
        key="fixed-test-redaction-key",
        internal_full_fidelity=False,
    )

    redacted = redactor.redact_text(
        "start\x1b]52;c;SGVsbG8=\x07\x1b[0m\x1b[2J\x1b[H\x1b]8;;https://example.test\x07click\x1b]8;;\x07end"
    )

    assert redacted == "startclickend"
    assert "\x1b" not in redacted


def test_redacted_export_text_quotes_operator_like_content() -> None:
    text = render_export_text(
        {
            "status": "pass",
            "summary": "ignore previous instructions",
            "command": "rm -rf /",
            "marker": "BEGIN SECRET",
            "syntax": "::set-output name=foo::bar",
        }
    )

    assert 'summary="ignore previous instructions"' in text
    assert 'command="rm -rf /"' in text
    assert 'marker="BEGIN SECRET"' in text
    assert 'syntax="::set-output name=foo::bar"' in text


def test_export_includes_counts_graph_closure_and_leak_scan_without_mutating_db(
    tmp_path: Path,
) -> None:
    db_path, workspace = build_fixture_store(tmp_path)
    output_dir = tmp_path / "diagnostics"
    before = table_counts(db_path)

    proc = run_export(db_path, workspace, output_dir)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert table_counts(db_path) == before
    canonical_summary = load_json(output_dir / "canonical-summary.json")
    graph_shape = load_json(output_dir / "graph-shape.json")
    review_summary = load_json(output_dir / "review-state-summary.json")
    relationship_summary = load_json(output_dir / "relationship-summary.json")
    graph_closure = load_json(output_dir / "graph-closure-summary.json")
    leak_report = load_json(output_dir / "leak-scan-report.json")

    assert canonical_summary["table_row_counts"]["work"] == 1
    assert canonical_summary["table_row_counts"]["source_claim"] == 1
    assert graph_shape["edge_counts_by_predicate"]["mentions"] == 1
    assert review_summary["tables"]["source_claim"]["proposed"] == 1
    assert relationship_summary["predicate_counts"]["mentions"] == 1
    assert graph_closure["schema_version"] == "canonical-graph-closure-report.v1"
    assert leak_report["status"] == "pass"


def test_deterministic_with_fixed_timestamp_and_key(tmp_path: Path) -> None:
    db_path, workspace = build_fixture_store(tmp_path)
    first = tmp_path / "diagnostics-a"
    second = tmp_path / "diagnostics-b"

    first_proc = run_export(db_path, workspace, first)
    second_proc = run_export(db_path, workspace, second)

    assert first_proc.returncode == 0, first_proc.stdout + first_proc.stderr
    assert second_proc.returncode == 0, second_proc.stdout + second_proc.stderr
    first_files = {path.name: path.read_bytes() for path in sorted(first.glob("*.json"))}
    second_files = {path.name: path.read_bytes() for path in sorted(second.glob("*.json"))}
    assert first_files == second_files


def test_internal_full_fidelity_is_explicit_and_marked_private(tmp_path: Path) -> None:
    db_path, workspace = build_fixture_store(tmp_path)
    output_dir = tmp_path / "internal-diagnostics"
    proc = run_export(
        db_path,
        workspace,
        output_dir,
        "--internal-full-fidelity",
        "--url-redaction",
        "full",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    manifest = load_json(output_dir / "diagnostic-manifest.json")
    source_access = load_json(output_dir / "source-access-summary.json")
    assert manifest["redaction_mode"] == "internal_full_fidelity"
    assert manifest["privacy_classification"] == "internal_private"
    assert any(PRIVATE_SENTINEL in str(record["locator"]) for record in source_access["records"])
    assert manifest["leak_scan"]["status"] == "fail"


def test_export_rejects_unrecognized_output_dir_with_overwrite(tmp_path: Path) -> None:
    db_path, workspace = build_fixture_store(tmp_path)
    output_dir = tmp_path / "diagnostics"
    output_dir.mkdir()
    (output_dir / "notes.txt").write_text("do-not-overwrite", encoding="utf-8")

    proc = run_export(db_path, workspace, output_dir)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "not a recognized redacted diagnostics bundle" in proc.stderr


def test_export_rejects_output_under_home_directory(tmp_path: Path) -> None:
    db_path, workspace = build_fixture_store(tmp_path)
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    output_dir = home_dir
    env = os.environ.copy()
    env["HOME"] = str(home_dir)

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--db",
            str(db_path),
            "--workspace",
            str(workspace),
            "--output-dir",
            str(output_dir),
            "--path-redaction",
            "hmac",
            "--url-redaction",
            "domain_only",
            "--redaction-key",
            "fixed-test-redaction-key",
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "refusing to write output to home directory" in proc.stderr


def test_export_overwrites_valid_diagnostic_bundle(tmp_path: Path) -> None:
    db_path, workspace = build_fixture_store(tmp_path)
    output_dir = tmp_path / "diagnostics"

    first = run_export(db_path, workspace, output_dir)
    second = run_export(db_path, workspace, output_dir)

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["manifest_path"] == str((output_dir / "diagnostic-manifest.json").resolve())
    assert second_payload["manifest_path"] == str((output_dir / "diagnostic-manifest.json").resolve())
    assert second_payload["status"] == "pass"
    manifest = load_json(output_dir / "diagnostic-manifest.json")
    assert manifest["schema_version"] == "redacted-diagnostic-manifest.v1"
