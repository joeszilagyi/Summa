from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.source_db_tools import canonical_store, cycle_evidence_ledger, loop_health

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_SCRIPT = REPO_ROOT / "tools" / "scripts" / "build_operator_dashboard.py"
FIXED_NOW = "2026-06-04T12:00:00Z"


def bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at="2026-06-01T00:00:00Z",
        applied_by="pytest.loop_health",
    )
    return db_path


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def connect(db_path: Path):
    return canonical_store.connect_canonical_store(db_path)


def gather_event(
    conn,
    *,
    subject_id: str,
    run_id: str,
    cycle_depth: int,
    timestamp: str,
) -> canonical_store.ProvenanceEventRef:
    note = {
        "subject_id": subject_id,
        "facet": "sources",
        "cycle_depth": cycle_depth,
        "prompt_bundle_id": "general.sources.v1",
    }
    return canonical_store.record_provenance_event(
        conn,
        object_namespace="gather_candidate_batch",
        object_id=run_id,
        event_type="gather_candidate_batch_ingest",
        tool_name="tests/test_loop_health_view.py",
        run_id=run_id,
        event_timestamp=timestamp,
        note_text=json.dumps(note, sort_keys=True),
        provenance_event_key_v1=f"prov:loop-health:gather:{subject_id}:{run_id}",
    )


def fixture_event(conn, *, suffix: str, timestamp: str) -> canonical_store.ProvenanceEventRef:
    return canonical_store.record_provenance_event(
        conn,
        object_namespace="loop_health_fixture",
        object_id=suffix,
        event_type="fixture_event",
        tool_name="tests/test_loop_health_view.py",
        run_id=f"fixture-{suffix}",
        event_timestamp=timestamp,
        provenance_event_key_v1=f"prov:loop-health:fixture:{suffix}",
    )


def add_cycle(
    conn,
    *,
    subject_id: str = "fixture_subject",
    run_id: str,
    cycle_depth: int,
    timestamp: str,
    reviewable_claims: int = 0,
    accepted_claims: int = 0,
    detected_entities: int = 0,
    relationships: int = 0,
    contradictions: int = 0,
) -> None:
    provenance = gather_event(
        conn,
        subject_id=subject_id,
        run_id=run_id,
        cycle_depth=cycle_depth,
        timestamp=timestamp,
    )
    for index in range(reviewable_claims):
        canonical_store.record_source_claim(
            conn,
            provenance_event_ref=provenance.event_key,
            source_claim_key_v1=f"claim:{subject_id}:{run_id}:reviewable:{index}",
            about_object_ref=f"subject:{subject_id}",
            claim_text=f"Reviewable claim {index} from {run_id}.",
            claim_type="fixture_claim",
            review_state="needs_review",
            workspace_id=subject_id,
            created_at=timestamp,
            record_last_updated=timestamp,
        )
    for index in range(accepted_claims):
        canonical_store.record_source_claim(
            conn,
            provenance_event_ref=provenance.event_key,
            source_claim_key_v1=f"claim:{subject_id}:{run_id}:accepted:{index}",
            about_object_ref=f"subject:{subject_id}",
            claim_text=f"Accepted claim {index} from {run_id}.",
            claim_type="fixture_claim",
            review_state="accepted",
            workspace_id=subject_id,
            created_at=timestamp,
            record_last_updated=timestamp,
        )
    for index in range(detected_entities):
        canonical_store.record_extraction_detected_entity(
            conn,
            provenance_event_ref=provenance.event_key,
            entity_label=f"Entity {index} {run_id}",
            entity_type="person",
            review_state="proposed",
            confidence_score=0.7,
            record_last_updated=timestamp,
        )
    for index in range(relationships):
        canonical_store.record_source_relationship(
            conn,
            provenance_event_ref=provenance.event_key,
            from_object_ref=f"subject:{subject_id}",
            to_object_ref=f"object:{index}",
            predicate="related_to",
            review_state="proposed",
            workspace_id=subject_id,
            created_at=timestamp,
            record_last_updated=timestamp,
        )
    for index in range(contradictions):
        canonical_store.record_source_relationship(
            conn,
            provenance_event_ref=provenance.event_key,
            from_object_ref=f"source_claim:{run_id}:{index}:left",
            to_object_ref=f"source_claim:{run_id}:{index}:right",
            predicate="contradicts",
            target_label="fixture_contradiction",
            evidence_note="fixture contradiction",
            review_state="needs_review",
            workspace_id=subject_id,
            created_at=timestamp,
            record_last_updated=timestamp,
        )


def add_review_decision(conn, *, suffix: str, timestamp: str, workspace_id: str = "fixture_subject") -> None:
    target_provenance = fixture_event(conn, suffix=f"review-target-{suffix}", timestamp=timestamp)
    claim = canonical_store.record_source_claim(
        conn,
        provenance_event_ref=target_provenance.event_key,
        source_claim_key_v1=f"claim:{workspace_id}:{suffix}:decision",
        about_object_ref=f"subject:{workspace_id}",
        claim_text=f"Decision target {suffix}.",
        claim_type="fixture_claim",
        review_state="rejected",
        workspace_id=workspace_id,
        created_at=timestamp,
        record_last_updated=timestamp,
    )
    canonical_store.record_provenance_event(
        conn,
        object_namespace="source_claim",
        object_id=str(claim.row_id),
        event_type="review_decision_reject_claim",
        actor_type="human",
        actor_id="pytest",
        tool_name="tools/scripts/apply_review_decision.py",
        event_timestamp=timestamp,
        note_text="fixture review decision",
        provenance_event_key_v1=f"prov:loop-health:review-decision:{suffix}",
    )
    canonical_store.record_review_state_history(
        conn,
        target_namespace="source_claim",
        target_id=claim.row_id,
        previous_state="needs_review",
        new_state="rejected",
        changed_by="pytest",
        changed_at=timestamp,
        reason="reject_claim",
        source_tool="tools/scripts/apply_review_decision.py",
        review_state_history_key_v1=f"review:loop-health:{suffix}",
    )


def summarize(db_path: Path, *, subject_id: str = "fixture_subject") -> dict[str, object]:
    conn = canonical_store.connect_existing_read_only(db_path)
    try:
        return loop_health.build_loop_health_summary(
            conn,
            subject_id=subject_id,
            workspace_id=subject_id,
            now=FIXED_NOW,
        )
    finally:
        conn.close()


def test_empty_store_reports_insufficient_data_without_false_lag(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)

    summary = summarize(db_path)

    assert summary["schema_version"] == "loop-health-summary.v1"
    assert summary["health_status"] == "insufficient_data"
    assert summary["data_availability"]["cycle_history_available"] is False  # type: ignore[index]
    assert summary["ingestion_resolution"]["resolution_coverage"] is None  # type: ignore[index]
    assert "review is not keeping pace" not in " ".join(summary["warnings"])  # type: ignore[index]


def test_populated_store_reports_per_cycle_yield_trend(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            add_cycle(conn, run_id="run-1", cycle_depth=1, timestamp="2026-06-04T10:00:00Z", reviewable_claims=1)
            add_cycle(
                conn,
                run_id="run-2",
                cycle_depth=2,
                timestamp="2026-06-04T11:00:00Z",
                reviewable_claims=2,
                detected_entities=1,
            )
    finally:
        conn.close()

    summary = summarize(db_path)

    assert summary["data_availability"]["cycle_history_available"] is True  # type: ignore[index]
    assert summary["aggregate_metrics"]["yield_trend"] == "rising"  # type: ignore[index]
    assert summary["aggregate_metrics"]["new_reviewable_records"] == 4  # type: ignore[index]
    assert [cycle["cycle_id"] for cycle in summary["per_cycle_metrics"]] == ["run-1", "run-2"]  # type: ignore[index]


def test_yield_trend_uses_the_full_lookback_window(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            add_cycle(conn, run_id="run-1", cycle_depth=1, timestamp="2026-06-04T10:00:00Z", reviewable_claims=1)
            add_cycle(conn, run_id="run-2", cycle_depth=2, timestamp="2026-06-04T11:00:00Z", reviewable_claims=5)
            add_cycle(conn, run_id="run-3", cycle_depth=3, timestamp="2026-06-04T12:00:00Z", reviewable_claims=2)
    finally:
        conn.close()

    summary = summarize(db_path)

    assert summary["aggregate_metrics"]["yield_trend"] == "rising"  # type: ignore[index]


def test_review_backlog_size_and_age_are_reported(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            provenance = fixture_event(conn, suffix="old-pending", timestamp="2026-04-01T00:00:00Z")
            canonical_store.record_source_claim(
                conn,
                provenance_event_ref=provenance.event_key,
                source_claim_key_v1="claim:loop-health:old-pending",
                about_object_ref="subject:fixture_subject",
                claim_text="Old pending claim.",
                claim_type="fixture_claim",
                review_state="needs_review",
                workspace_id="fixture_subject",
                created_at="2026-04-01T00:00:00Z",
                record_last_updated="2026-04-01T00:00:00Z",
            )
    finally:
        conn.close()

    summary = summarize(db_path)

    assert summary["review_backlog"]["pending_review_count"] == 1  # type: ignore[index]
    assert summary["review_backlog"]["pending_by_family"]["source_claim"] == 1  # type: ignore[index]
    assert summary["review_backlog"]["oldest_pending_age_days"] == 64.5  # type: ignore[index]


def test_ingestion_versus_resolution_coverage_is_reported(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            add_cycle(conn, run_id="run-1", cycle_depth=1, timestamp="2026-06-04T10:00:00Z", reviewable_claims=4)
            add_review_decision(conn, suffix="claim-1", timestamp="2026-06-04T11:00:00Z")
            add_review_decision(conn, suffix="claim-2", timestamp="2026-06-04T11:01:00Z")
    finally:
        conn.close()

    summary = summarize(db_path)

    assert summary["data_availability"]["review_resolution_available"] is True  # type: ignore[index]
    assert summary["ingestion_resolution"]["reviewable_ingested_count"] == 4  # type: ignore[index]
    assert summary["ingestion_resolution"]["review_decision_applied_count"] == 2  # type: ignore[index]
    assert summary["ingestion_resolution"]["resolution_coverage"] == 0.5  # type: ignore[index]


def test_contradiction_rate_and_spike_status_are_reported(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            add_cycle(
                conn,
                run_id="run-1",
                cycle_depth=1,
                timestamp="2026-06-04T10:00:00Z",
                reviewable_claims=2,
                contradictions=1,
            )
    finally:
        conn.close()

    summary = summarize(db_path)

    assert summary["contradictions"]["new_contradictions"] == 1  # type: ignore[index]
    assert summary["contradictions"]["contradictions_per_new_source_claim"] == 0.5  # type: ignore[index]
    assert summary["health_status"] == "contradiction_spike"


def test_review_lagging_status_when_resolution_falls_behind(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            add_cycle(conn, run_id="run-1", cycle_depth=1, timestamp="2026-06-04T09:00:00Z", reviewable_claims=2)
            add_cycle(conn, run_id="run-2", cycle_depth=2, timestamp="2026-06-04T10:00:00Z", reviewable_claims=4)
            add_review_decision(conn, suffix="claim-1", timestamp="2026-06-04T11:00:00Z")
    finally:
        conn.close()

    summary = summarize(db_path)

    assert summary["health_status"] == "review_lagging"
    assert summary["ingestion_resolution"]["resolution_coverage"] == 0.1667  # type: ignore[index]
    assert any("review decisions" in warning for warning in summary["warnings"])  # type: ignore[index]


def test_workspace_scoped_summary_ignores_other_workspace_activity(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            add_cycle(conn, run_id="run-1", cycle_depth=1, timestamp="2026-06-04T09:00:00Z", reviewable_claims=1)
            add_cycle(conn, run_id="run-2", cycle_depth=2, timestamp="2026-06-04T10:00:00Z", reviewable_claims=2)
            add_review_decision(conn, suffix="local", timestamp="2026-06-04T10:30:00Z")

            add_cycle(
                conn,
                subject_id="other_workspace",
                run_id="foreign-run",
                cycle_depth=1,
                timestamp="2026-06-04T11:00:00Z",
                reviewable_claims=9,
                contradictions=2,
            )
            add_review_decision(
                conn,
                suffix="foreign",
                timestamp="2026-06-04T11:30:00Z",
                workspace_id="other_workspace",
            )
    finally:
        conn.close()

    scoped = canonical_store.connect_existing_read_only(db_path)
    try:
        summary = loop_health.build_loop_health_summary(scoped, workspace_id="fixture_subject", now=FIXED_NOW)
    finally:
        scoped.close()

    assert summary["cycle_ids_considered"] == ["run-1", "run-2"]  # type: ignore[index]
    assert summary["aggregate_metrics"]["new_reviewable_records"] == 3  # type: ignore[index]
    assert summary["review_backlog"]["pending_review_count"] == 3  # type: ignore[index]
    assert summary["contradictions"]["new_contradictions"] == 0  # type: ignore[index]
    assert summary["ingestion_resolution"]["review_decision_applied_count"] == 1  # type: ignore[index]


def test_loop_health_uses_persisted_cycle_event_metrics_when_available(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            cycle_id = cycle_evidence_ledger.record_cycle_event_start(
                conn,
                run_id="cycle-ledger-metrics",
                workspace_id="fixture_subject",
                workspace_ref=str(tmp_path / "workspace"),
                subject_key="fixture_subject",
                domain_pack_id="general.v1",
                cycle_depth=1,
                mode="local",
                started_at="2026-06-04T10:00:00Z",
                status="running",
            )
            cycle_evidence_ledger.record_cycle_event_finish(
                conn,
                cycle_event_id=cycle_id,
                status="completed",
                ended_at="2026-06-04T10:05:00Z",
                row_count_delta={
                    "cycle_id": "cycle-ledger-metrics",
                    "cycle_depth": 1,
                    "event_timestamp": "2026-06-04T10:00:00Z",
                    "started_at": "2026-06-04T10:00:00Z",
                    "ended_at": "2026-06-04T10:05:00Z",
                    "final_status": "completed",
                    "facet": "sources",
                    "gather_candidate_count": None,
                    "candidate_ingest_count": 2,
                    "execution_capture_count": 0,
                    "execution_extraction_count": 0,
                    "new_work_count": 1,
                    "new_source_claim_count": 1,
                    "new_detected_entity_count": 0,
                    "new_source_relationship_count": 0,
                    "new_authority_reconciliation_count": None,
                    "new_contradiction_count": 0,
                    "new_reviewable_count": 2,
                    "new_accepted_count": 1,
                    "new_rejected_or_resolved_count": None,
                    "review_backlog_delta": None,
                    "feedback_selected_action": None,
                    "yield_score": 3,
                    "warning_count": 0,
                    "failure_stage": None,
                    "table_counts": {
                        "work": 1,
                        "source_claim": 1,
                        "extraction_detected_entity": 0,
                        "source_relationship": 0,
                        "capture_event": 0,
                        "extraction_record": 0,
                    },
                },
            )
    finally:
        conn.close()

    summary = summarize(db_path)

    assert summary["cycle_ids_considered"] == ["cycle-ledger-metrics"]  # type: ignore[index]
    assert summary["health_status"] == "healthy"
    assert summary["aggregate_metrics"]["new_reviewable_records"] == 2  # type: ignore[index]
    assert summary["aggregate_metrics"]["new_accepted_records"] == 1  # type: ignore[index]
    assert summary["contradictions"]["new_contradictions"] == 0  # type: ignore[index]


def test_resolution_metrics_are_unavailable_without_f29_apply_records(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            add_cycle(conn, run_id="run-1", cycle_depth=1, timestamp="2026-06-04T10:00:00Z", reviewable_claims=1)
    finally:
        conn.close()

    summary = summarize(db_path)

    assert summary["data_availability"]["review_resolution_available"] is False  # type: ignore[index]
    assert summary["ingestion_resolution"]["review_decision_applied_count"] is None  # type: ignore[index]
    assert summary["ingestion_resolution"]["resolution_coverage"] is None  # type: ignore[index]
    assert "review_decision_provenance_unavailable" in summary["limitations"]  # type: ignore[operator]


def test_loop_health_summary_is_read_only(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            add_cycle(conn, run_id="run-1", cycle_depth=1, timestamp="2026-06-04T10:00:00Z", reviewable_claims=1)
    finally:
        conn.close()

    before = file_hash(db_path)
    summary = loop_health.summarize_loop_health(db_path, subject_id="fixture_subject", now=FIXED_NOW)
    after = file_hash(db_path)

    assert summary["read_only"] is True
    assert before == after


def test_loop_health_summary_uses_fast_population_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = connect(db_path)
    try:
        with conn:
            add_cycle(conn, run_id="run-1", cycle_depth=1, timestamp="2026-06-04T10:00:00Z", reviewable_claims=1)
    finally:
        conn.close()

    include_counts_calls: list[bool] = []

    def fake_summary(db_path: Path, *, include_counts: bool = True) -> dict[str, object]:
        include_counts_calls.append(include_counts)
        return {
            "path": str(db_path),
            "exists": True,
            "initialized": True,
            "valid": True,
            "schema_version": 8,
            "current_migration_id": "m8",
            "status": "initialized_empty",
            "family_counts": {},
            "table_counts": {},
            "total_rows": None,
            "last_provenance_event_at": None,
            "last_provenance_event_type": None,
            "last_provenance_event_id": None,
            "last_ingest_at": None,
            "last_ingest_event_type": None,
            "last_ingest_provenance_event_id": None,
            "warnings": [],
            "errors": [],
            "recommended_interpretation": "Canonical store is initialized and valid, but contains no canonical records yet.",
        }

    monkeypatch.setattr(canonical_store, "summarize_canonical_store_population", fake_summary)

    summary = loop_health.summarize_loop_health(db_path, subject_id="fixture_subject", now=FIXED_NOW)

    assert include_counts_calls == [False]
    assert summary["read_only"] is True


def test_local_doctor_includes_loop_health_section(tmp_path: Path) -> None:
    sys.path.insert(0, str(REPO_ROOT / "tools" / "scripts"))
    import local_doctor

    db_path = bootstrap_db(tmp_path)

    report = local_doctor.build_report(REPO_ROOT, canonical_db=db_path)

    assert "loop_health" in report
    assert report["loop_health"]["schema_version"] == "loop-health-summary.v1"
    assert report["checks"]["loop_health"] in {"pass", "warn"}


def test_operator_dashboard_renders_loop_health_section(tmp_path: Path) -> None:
    doctor_report = tmp_path / "doctor.json"
    doctor_report.write_text(
        json.dumps(
            {
                "schema_version": "local-doctor-report.v1",
                "summary": {"status": "warn", "finding_count": 1, "operator_action_required_count": 0},
                "checks": {"loop_health": "warn"},
                "backup_posture": {"policy_status": "pass", "status": "pass"},
                "migration_posture": {"status": "pass"},
                "scheduler": {"selector_status": "pass", "status": "pass"},
                "public_gates": {"surfaces": {}},
                "canonical_store": {"status": "populated", "family_counts": {}, "table_counts": {}},
                "loop_health": {
                    "health_status": "review_lagging",
                    "lookback_cycles": 5,
                    "aggregate_metrics": {"yield_trend": "flat"},
                    "review_backlog": {
                        "pending_review_count": 12,
                        "oldest_pending_age_days": 31.0,
                        "median_pending_age_days": 8.0,
                    },
                    "contradictions": {
                        "total_contradictions": 3,
                        "new_contradictions": 1,
                        "contradictions_per_new_source_claim": 0.25,
                    },
                    "ingestion_resolution": {
                        "reviewable_ingested_count": 10,
                        "review_decision_applied_count": 2,
                        "resolution_coverage": 0.2,
                    },
                    "per_cycle_metrics": [
                        {
                            "cycle_id": "run-1",
                            "cycle_depth": 1,
                            "new_reviewable_count": 10,
                            "new_accepted_count": 0,
                            "new_contradiction_count": 1,
                            "yield_score": 10,
                        }
                    ],
                    "warnings": ["review decisions are not keeping pace with reviewable ingestion"],
                    "limitations": [],
                },
                "workspaces": [],
                "databases": [],
                "locks": [],
                "findings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "dashboard.html"
    proc = subprocess.run(
        [sys.executable, str(DASHBOARD_SCRIPT), "--doctor-report", str(doctor_report), "--output", str(output)],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    body = output.read_text(encoding="utf-8")
    assert "<h2>Loop Health</h2>" in body
    assert "review_lagging" in body
    assert "resolution_coverage" in body
