from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

from tools.source_db_tools import canonical_graph_closure, canonical_ingest, canonical_store

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "scripts" / "audit_canonical_graph_closure.py"
FIXTURE_BATCH = (
    REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "gather-candidate-batch.json"
)
FIXED_TIMESTAMP = "2026-06-03T12:34:56Z"


def init_db(path: Path) -> None:
    canonical_store.init_canonical_store(
        path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.graph_closure",
    )


def populate_batch(path: Path) -> None:
    batch, batch_hash = canonical_ingest.load_validated_candidate_batch(FIXTURE_BATCH)
    conn = canonical_store.connect_canonical_store(path)
    try:
        with conn:
            canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=FIXTURE_BATCH,
                batch_hash=batch_hash,
                db_path=path,
            )
    finally:
        conn.close()


def table_count(path: Path, table_name: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
    finally:
        conn.close()


def insert_orphan_claim(path: Path, *, review_state: str = "accepted") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """
            INSERT INTO source_claim (
              source_claim_key_v1,
              about_object_ref,
              claim_text,
              public_summary,
              claim_type,
              review_state,
              provenance_event_ref,
              capture_event_id,
              extraction_id,
              created_at,
              record_last_updated
            ) VALUES (?, NULL, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
            """,
            (
                f"claim:orphan:{review_state}",
                "orphan claim",
                "orphan claim",
                "factual",
                review_state,
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def insert_unresolved_tracked_claim(path: Path) -> None:
    conn = canonical_store.connect_canonical_store(path)
    try:
        with conn:
            provenance = canonical_store.record_provenance_event(
                conn,
                object_namespace="source_claim",
                object_id="claim:unresolved:tracked",
                event_type="gather_candidate_batch_ingest",
                tool_name="pytest.graph_closure",
                run_id="graph-closure",
                event_timestamp=FIXED_TIMESTAMP,
                provenance_event_key_v1="prov:graph-closure:unresolved",
            )
            conn.execute(
                """
                INSERT INTO source_claim (
                  source_claim_key_v1,
                  about_object_ref,
                  claim_text,
                  public_summary,
                  claim_type,
                  review_state,
                  provenance_event_ref,
                  created_at,
                  record_last_updated
                ) VALUES (?, NULL, ?, ?, ?, 'proposed', ?, ?, ?)
                """,
                (
                    "claim:unresolved:tracked",
                    "unresolved tracked claim",
                    "unresolved tracked claim",
                    "factual",
                    provenance.event_key,
                    FIXED_TIMESTAMP,
                    FIXED_TIMESTAMP,
                ),
            )
    finally:
        conn.close()


def insert_missing_work_references(path: Path) -> None:
    conn = canonical_store.connect_canonical_store(path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        with conn:
            provenance = canonical_store.record_provenance_event(
                conn,
                object_namespace="fixture",
                object_id="graph-closure-missing-work",
                event_type="fixture_ingest",
                tool_name="pytest.graph_closure",
                run_id="graph-closure",
                event_timestamp=FIXED_TIMESTAMP,
                provenance_event_key_v1="prov:graph-closure:missing-work",
            )
            canonical_store.record_source_access(
                conn,
                provenance_event_ref=provenance.event_key,
                work_id=999,
                original_locator="https://example.invalid/missing-work",
                review_state="needs_review",
                publication_state="private_working",
                workspace_id="fixture-workspace",
                record_last_updated=FIXED_TIMESTAMP,
            )
            canonical_store.record_capture_event(
                conn,
                provenance_event_ref=provenance.event_key,
                work_id=999,
                original_locator="https://example.invalid/capture-missing-work",
                captured_at=FIXED_TIMESTAMP,
                capture_method="fixture_capture",
                review_state="needs_review",
                workspace_id="fixture-workspace",
                record_last_updated=FIXED_TIMESTAMP,
            )
    finally:
        conn.close()


def test_cli_help_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "graph closure" in proc.stdout.lower()
    assert "--db" in proc.stdout


def test_empty_store_reports_no_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)

    report = canonical_graph_closure.audit_canonical_graph_closure(
        db_path,
        generated_at=FIXED_TIMESTAMP,
    )

    assert report["status"] == "no_rows"
    assert report["summary"]["true_orphan_error_count"] == 0
    assert report["read_only"] is True
    assert report["repair_performed"] is False


def test_populated_store_has_no_true_orphan_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    populate_batch(db_path)

    report_path = tmp_path / "graph-closure-report.json"
    report = canonical_graph_closure.audit_canonical_graph_closure(
        db_path,
        generated_at=FIXED_TIMESTAMP,
        report_path=report_path,
    )

    assert report_path.is_file()
    assert report["status"] in {"pass", "pass_with_unresolved"}
    assert report["summary"]["true_orphan_error_count"] == 0
    assert report["report_sha256"].startswith("sha256:")


def test_true_orphan_claim_fails_strict_audit(tmp_path: Path) -> None:
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    insert_orphan_claim(db_path)

    report = canonical_graph_closure.audit_canonical_graph_closure(
        db_path,
        generated_at=FIXED_TIMESTAMP,
        strict=True,
    )

    assert report["status"] == "fail"
    assert report["summary"]["true_orphan_error_count"] == 1
    assert report["issues"][0]["table"] == "source_claim"


def test_unresolved_tracked_claim_is_visible_but_not_orphan(tmp_path: Path) -> None:
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    insert_unresolved_tracked_claim(db_path)

    report = canonical_graph_closure.audit_canonical_graph_closure(
        db_path,
        generated_at=FIXED_TIMESTAMP,
    )

    assert report["status"] == "pass_with_unresolved"
    assert report["summary"]["true_orphan_error_count"] == 0
    assert report["summary"]["unresolved_tracked_count"] >= 1
    assert any(issue["status"] == "unresolved_tracked" for issue in report["issues"])


def test_missing_work_links_are_reported_as_orphans(tmp_path: Path) -> None:
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    insert_missing_work_references(db_path)

    report = canonical_graph_closure.audit_canonical_graph_closure(
        db_path,
        generated_at=FIXED_TIMESTAMP,
    )

    codes = {issue["code"] for issue in report["issues"]}
    assert "SOURCE_ACCESS_TRUE_ORPHAN" in codes
    assert "CAPTURE_EVENT_TRUE_ORPHAN" in codes
    assert report["status"] == "fail"


def test_graph_closure_report_is_deterministic_and_read_only(tmp_path: Path) -> None:
    db_path = tmp_path / "canonical.sqlite"
    init_db(db_path)
    populate_batch(db_path)
    before_counts = {
        table: table_count(db_path, table)
        for table in ("source_claim", "provenance_event", "review_state_history")
    }

    first = canonical_graph_closure.audit_canonical_graph_closure(
        db_path,
        generated_at=FIXED_TIMESTAMP,
    )
    second = canonical_graph_closure.audit_canonical_graph_closure(
        db_path,
        generated_at=FIXED_TIMESTAMP,
    )
    after_counts = {
        table: table_count(db_path, table)
        for table in ("source_claim", "provenance_event", "review_state_history")
    }

    assert first == second
    assert after_counts == before_counts
