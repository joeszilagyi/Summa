from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tools.source_db_tools import canonical_store, cycle_evidence_ledger

FIXED_TIMESTAMP = "2026-06-04T10:00:00Z"


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "canonical.sqlite"
    result = canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest",
    )
    assert result.schema_version == canonical_store.CURRENT_SCHEMA_VERSION
    return db_path


def count_rows(conn: sqlite3.Connection, table_name: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def test_bootstrap_includes_cycle_evidence_ledger_tables(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        tables = canonical_store.actual_tables(conn)
    finally:
        conn.close()

    assert {
        "cycle_event",
        "cycle_stage_event",
        "cycle_artifact_ref",
        "cycle_candidate_considered",
        "cycle_candidate_excluded",
        "cycle_tool_failure",
        "cycle_operator_override",
    } <= tables


def test_cycle_evidence_write_and_read_helpers_are_deterministic(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            cycle_id = cycle_evidence_ledger.record_cycle_event_start(
                conn,
                run_id="cycle-ledger-test",
                workspace_id="fixture_workspace",
                workspace_ref=str(tmp_path / "workspace"),
                subject_key="fixture_subject",
                domain_pack_id="general.v1",
                cycle_depth=1,
                previous_run_ids=["prior-run"],
                mode="local",
                started_at=FIXED_TIMESTAMP,
                status="running",
            )
            repeat_cycle_id = cycle_evidence_ledger.record_cycle_event_start(
                conn,
                run_id="cycle-ledger-test",
                workspace_id="fixture_workspace",
                workspace_ref=str(tmp_path / "workspace"),
                subject_key="fixture_subject",
                domain_pack_id="general.v1",
                cycle_depth=1,
                previous_run_ids=["prior-run"],
                mode="local",
                started_at=FIXED_TIMESTAMP,
                status="running",
            )
            assert repeat_cycle_id == cycle_id
            stage_id = cycle_evidence_ledger.record_cycle_stage_start(
                conn,
                cycle_event_id=cycle_id,
                run_id="cycle-ledger-test",
                stage_name="run_gather",
                stage_order=1,
                started_at=FIXED_TIMESTAMP,
                command_name="run_topic_gather.py",
            )
            cycle_evidence_ledger.record_cycle_stage_finish(
                conn,
                stage_event_id=stage_id,
                status="passed",
                ended_at="2026-06-04T10:01:00Z",
                validation_status="pass",
            )
            artifact_id = cycle_evidence_ledger.record_cycle_artifact_ref(
                conn,
                cycle_event_id=cycle_id,
                stage_event_id=stage_id,
                artifact_type="candidate_batch",
                artifact_path=str(tmp_path / "gather-candidate-batch.json"),
                artifact_hash="sha256:" + "a" * 64,
                byte_count=12,
                schema_id="gather-candidate-batch.v1",
                validation_status="pass",
            )
            cycle_evidence_ledger.record_cycle_candidate_considered(
                conn,
                cycle_event_id=cycle_id,
                stage_event_id=stage_id,
                candidate_kind="source_lead",
                candidate_ref_type="gather_candidate",
                candidate_ref_id="candidate-1",
                candidate_label="source_lead / proposed",
                score=0.75,
                score_policy_id="policy:test",
                rationale="fixture selection evidence",
                reason={"reason_codes": ["fixture"]},
                selected=True,
            )
            cycle_evidence_ledger.record_cycle_candidate_excluded(
                conn,
                cycle_event_id=cycle_id,
                stage_event_id=stage_id,
                candidate_kind="cycle_stage",
                candidate_ref_type="stage",
                candidate_ref_id="execute_source_adapter",
                candidate_label="execute_source_adapter",
                exclusion_reason="no source handoff supplied",
                retryable=True,
            )
            cycle_evidence_ledger.record_cycle_tool_failure(
                conn,
                cycle_event_id=cycle_id,
                stage_event_id=stage_id,
                tool_name="run_topic_gather.py",
                command_name="run_topic_gather.py",
                failure_kind="stage_failure",
                error_summary="fixture failure",
                artifact_ref_id=artifact_id,
                retryable=True,
            )
            cycle_evidence_ledger.record_cycle_operator_override(
                conn,
                cycle_event_id=cycle_id,
                override_kind="manual_candidate_batch_fixture",
                override_value="fixture",
                reason="operator supplied an explicit local artifact input",
                actor="pytest",
            )
            cycle_evidence_ledger.record_cycle_event_finish(
                conn,
                cycle_event_id=cycle_id,
                status="failed",
                ended_at="2026-06-04T10:02:00Z",
                error_count=1,
            )

        summary = cycle_evidence_ledger.summarize_cycle_evidence(conn, cycle_id)
        assert summary["schema_version"] == cycle_evidence_ledger.SCHEMA_VERSION
        assert summary["cycle_event"]["status"] == "failed"
        assert summary["counts"] == {
            "stages": 1,
            "artifacts": 1,
            "candidates_considered": 1,
            "candidates_excluded": 1,
            "tool_failures": 1,
            "operator_overrides": 1,
        }
        assert [stage["stage_name"] for stage in summary["stages"]] == ["run_gather"]
        assert [artifact["artifact_type"] for artifact in summary["artifacts"]] == [
            "candidate_batch"
        ]
        assert count_rows(conn, "source_claim") == 0
    finally:
        conn.close()


def test_cycle_evidence_foreign_keys_and_transaction_rollback(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError), conn:
            cycle_evidence_ledger.record_cycle_stage_start(
                conn,
                cycle_event_id="cycle:missing",
                run_id="missing-run",
                stage_name="run_gather",
                stage_order=1,
            )

        conn.execute("BEGIN")
        cycle_evidence_ledger.record_cycle_event_start(
            conn,
            run_id="rolled-back-cycle",
            workspace_id="fixture_workspace",
            started_at=FIXED_TIMESTAMP,
        )
        conn.rollback()

        assert count_rows(conn, "cycle_event") == 0
    finally:
        conn.close()


def test_cycle_evidence_helpers_fail_clearly_on_invalid_inputs(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with pytest.raises(cycle_evidence_ledger.CycleEvidenceLedgerError, match="run_id"):
            cycle_evidence_ledger.record_cycle_event_start(conn, run_id="")
        with pytest.raises(cycle_evidence_ledger.CycleEvidenceLedgerError, match="stage_name"):
            cycle_evidence_ledger.record_cycle_stage_start(
                conn,
                cycle_event_id="cycle:test",
                run_id="run",
                stage_name="",
                stage_order=1,
            )
        with pytest.raises(
            cycle_evidence_ledger.CycleEvidenceLedgerError,
            match="cycle_event finish target not found",
        ):
            cycle_evidence_ledger.record_cycle_event_finish(
                conn,
                cycle_event_id="cycle:missing",
                status="failed",
            )
        with pytest.raises(
            cycle_evidence_ledger.CycleEvidenceLedgerError,
            match="cycle_stage_event finish target not found",
        ):
            cycle_evidence_ledger.record_cycle_stage_finish(
                conn,
                stage_event_id="stage:missing",
                status="failed",
            )
    finally:
        conn.close()


def test_cycle_events_for_subject_returns_latest_when_limited(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        cycle_evidence_ledger.record_cycle_event_start(
            conn,
            run_id="run-1",
            workspace_id="fixture_workspace",
            workspace_ref=str(tmp_path / "workspace"),
            subject_key="fixture_subject",
            domain_pack_id="general.v1",
            cycle_depth=1,
            previous_run_ids=["run-0"],
            mode="local",
            started_at="2026-06-01T00:00:00Z",
            status="running",
        )
        cycle_evidence_ledger.record_cycle_event_start(
            conn,
            run_id="run-2",
            workspace_id="fixture_workspace",
            workspace_ref=str(tmp_path / "workspace"),
            subject_key="fixture_subject",
            domain_pack_id="general.v1",
            cycle_depth=1,
            previous_run_ids=["run-1"],
            mode="local",
            started_at="2026-06-02T00:00:00Z",
            status="running",
        )
        cycle_evidence_ledger.record_cycle_event_start(
            conn,
            run_id="run-3",
            workspace_id="fixture_workspace",
            workspace_ref=str(tmp_path / "workspace"),
            subject_key="fixture_subject",
            domain_pack_id="general.v1",
            cycle_depth=1,
            previous_run_ids=["run-2"],
            mode="local",
            started_at="2026-06-03T00:00:00Z",
            status="running",
        )
        assert [event["run_id"] for event in cycle_evidence_ledger.list_cycle_events_for_subject(conn, "fixture_subject", limit=1)] == ["run-3"]
        assert [event["run_id"] for event in cycle_evidence_ledger.list_cycle_events_for_subject(conn, "fixture_subject")] == ["run-1", "run-2", "run-3"]
    finally:
        conn.close()


def test_cycle_event_start_replays_are_idempotent_by_run_id(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        first_id = cycle_evidence_ledger.record_cycle_event_start(
            conn,
            run_id="run-duplicate",
            workspace_id="fixture_workspace",
            workspace_ref=str(tmp_path / "workspace"),
            subject_key="fixture_subject",
            domain_pack_id="general.v1",
            cycle_depth=1,
            mode="local",
            started_at="2026-06-01T00:00:00Z",
            status="running",
        )
        second_id = cycle_evidence_ledger.record_cycle_event_start(
            conn,
            run_id="run-duplicate",
            workspace_id="fixture_workspace",
            workspace_ref=str(tmp_path / "workspace"),
            subject_key="fixture_subject",
            domain_pack_id="general.v1",
            cycle_depth=1,
            mode="local",
            started_at="2026-06-01T00:00:00Z",
            status="running",
        )
        assert first_id == second_id
        event = cycle_evidence_ledger.load_cycle_event(conn, first_id)
        assert event is not None
        assert event["started_at"] == "2026-06-01T00:00:00Z"
        expected_id = cycle_evidence_ledger.build_cycle_event_id(
            run_id="run-duplicate",
            started_at="2026-06-03T00:00:00Z",
            workspace_ref=str(tmp_path / "workspace"),
        )
        assert first_id == expected_id
        with pytest.raises(
            cycle_evidence_ledger.CycleEvidenceLedgerError,
            match="ledger replay mismatch",
        ):
            cycle_evidence_ledger.record_cycle_event_start(
                conn,
                run_id="run-duplicate",
                workspace_id="fixture_workspace",
                workspace_ref=str(tmp_path / "workspace"),
                subject_key="fixture_subject",
                domain_pack_id="general.v1",
                cycle_depth=1,
                mode="local",
                started_at="2026-06-03T00:00:00Z",
                status="running",
            )
    finally:
        conn.close()


def test_cycle_event_start_replay_ignores_status_transition_for_force_reruns(
    tmp_path: Path,
) -> None:
    db_path = init_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        first_id = cycle_evidence_ledger.record_cycle_event_start(
            conn,
            run_id="run-force-rerun",
            workspace_id="fixture_workspace",
            workspace_ref=str(tmp_path / "workspace"),
            subject_key="fixture_subject",
            domain_pack_id="general.v1",
            cycle_depth=1,
            mode="local",
            started_at="2026-06-01T00:00:00Z",
            status="completed",
        )
        second_id = cycle_evidence_ledger.record_cycle_event_start(
            conn,
            run_id="run-force-rerun",
            workspace_id="fixture_workspace",
            workspace_ref=str(tmp_path / "workspace"),
            subject_key="fixture_subject",
            domain_pack_id="general.v1",
            cycle_depth=1,
            mode="local",
            started_at="2026-06-01T00:00:00Z",
            status="failed",
        )
    finally:
        conn.close()

    assert first_id == second_id


def test_feedback_candidate_fallback_uses_current_deferred_contract(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    plan_path = tmp_path / "candidate-feedback-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": "candidate-feedback-plan.v1",
                "next_action": {
                    "action_id": "next-action:fixture:sources:facet:1",
                    "action_kind": "facet_only",
                    "subject_id": "fixture_subject",
                    "selected_facet": "sources",
                    "selected_prompt_bundle_id": "bundle:sources",
                    "selected_object_ref": None,
                    "selected_lead_kind": None,
                    "selected_source_locus_id": None,
                    "selected_source_lead_id": None,
                    "selected_label": "sources",
                    "selected_review_state": None,
                    "selection_score": 1.0,
                    "scoring_policy_id": "candidate-feedback.default.v1",
                    "rationale": "fixture",
                    "reason_codes": ["fixture"],
                    "cycle_depth": 1,
                    "use_prior_state": False,
                    "previous_run_ids_considered": [],
                    "input_record_refs": [],
                    "suggested_cli_args": ["--facet", "sources"],
                },
                "deferred": [
                    {
                        "candidate_id": "lead:123",
                        "candidate_kind": "lead",
                        "score": -1.5,
                        "reason": "repeated_low_yield",
                    },
                    {
                        "candidate_id": "facet:open_questions",
                        "candidate_kind": "facet",
                        "score": 0.25,
                        "reason": "lower_score_than_selected",
                    },
                ],
                "warnings": [],
                "errors": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        with conn:
            cycle_event_id = cycle_evidence_ledger.record_cycle_event_start(
                conn,
                run_id="feedback-plan-fallback",
                workspace_id="fixture_workspace",
                workspace_ref=str(tmp_path / "workspace"),
                subject_key="fixture_subject",
                domain_pack_id="general.v1",
                cycle_depth=1,
                mode="local",
                started_at=FIXED_TIMESTAMP,
                status="running",
            )
            cycle_evidence_ledger._record_feedback_candidates(  # type: ignore[attr-defined]
                conn,
                cycle_event_id=cycle_event_id,
                stage_event_id=None,
                feedback_plan_path=plan_path,
            )
        rows = conn.execute(
            """
            SELECT candidate_kind, candidate_ref_type, candidate_ref_id, exclusion_reason, retryable
            FROM cycle_candidate_excluded
            ORDER BY candidate_ref_id
            """
        ).fetchall()
    finally:
        conn.close()

    assert [row["candidate_ref_id"] for row in rows] == ["facet:open_questions", "lead:123"]
    assert [row["candidate_ref_type"] for row in rows] == [
        "candidate_feedback_plan",
        "candidate_feedback_plan",
    ]
    assert [row["candidate_kind"] for row in rows] == ["facet", "lead"]
    assert [row["exclusion_reason"] for row in rows] == [
        "lower_score_than_selected",
        "repeated_low_yield",
    ]
    assert [bool(row["retryable"]) for row in rows] == [True, False]
