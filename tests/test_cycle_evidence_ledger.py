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


def test_cycle_evidence_rejects_non_finite_json_values(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with pytest.raises(ValueError, match="Out of range float values are not JSON compliant"):
            cycle_evidence_ledger.record_cycle_event_start(
                conn,
                run_id="run-non-finite-json",
                metadata={"bad": float("nan")},
            )
    finally:
        conn.close()


def test_cycle_evidence_finish_rejects_reverse_chronology(tmp_path: Path) -> None:
    db_path = init_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        cycle_id = cycle_evidence_ledger.record_cycle_event_start(
            conn,
            run_id="run-time-reversal",
            started_at="2026-06-03T12:34:56Z",
        )
        stage_id = cycle_evidence_ledger.record_cycle_stage_start(
            conn,
            cycle_event_id=cycle_id,
            run_id="run-time-reversal",
            stage_name="gather",
            stage_order=1,
            started_at="2026-06-03T12:34:56Z",
        )
        with pytest.raises(
            cycle_evidence_ledger.CycleEvidenceLedgerError,
            match="earlier than started_at",
        ):
            cycle_evidence_ledger.record_cycle_event_finish(
                conn,
                cycle_event_id=cycle_id,
                status="failed",
                ended_at="2026-06-03T12:34:55Z",
            )
        with pytest.raises(
            cycle_evidence_ledger.CycleEvidenceLedgerError,
            match="earlier than started_at",
        ):
            cycle_evidence_ledger.record_cycle_stage_finish(
                conn,
                stage_event_id=stage_id,
                status="failed",
                ended_at="2026-06-03T12:34:55Z",
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
    plan_payload = {
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
    }
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
            cycle_evidence_ledger._record_feedback_candidates_payload(  # type: ignore[attr-defined]
                conn,
                cycle_event_id=cycle_event_id,
                stage_event_id=None,
                payload=plan_payload,
                source_artifact_path="candidate-feedback-plan.json",
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


def test_candidate_batch_payload_records_considered_candidates_without_file_reads(
    tmp_path: Path,
) -> None:
    db_path = init_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            cycle_event_id = cycle_evidence_ledger.record_cycle_event_start(
                conn,
                run_id="candidate-batch-payload",
                workspace_id="fixture_workspace",
                workspace_ref=str(tmp_path / "workspace"),
                subject_key="fixture_subject",
                domain_pack_id="general.v1",
                cycle_depth=1,
                mode="local",
                started_at=FIXED_TIMESTAMP,
                status="running",
            )
            cycle_evidence_ledger._record_candidate_batch_payload(  # type: ignore[attr-defined]
                conn,
                cycle_event_id=cycle_event_id,
                stage_event_id=None,
                batch={
                    "facet": {"name": "sources"},
                    "candidates": [
                        {
                            "candidate_id": "candidate-1",
                            "candidate_type": "source_lead",
                            "review_status": "proposed",
                            "persistence_status": "workspace_only",
                            "origin": {"source": "fixture"},
                        },
                        {
                            "candidate_id": "candidate-2",
                            "candidate_type": "source_lead",
                            "review_status": "rejected",
                            "persistence_status": "discarded",
                            "origin": {"source": "fixture"},
                        },
                    ],
                },
                source_artifact_path="gather-candidate-batch.json",
            )
        rows = conn.execute(
            """
            SELECT candidate_ref_id, candidate_kind, candidate_label, selected
            FROM cycle_candidate_considered
            ORDER BY candidate_ref_id
            """
        ).fetchall()
    finally:
        conn.close()

    assert [row["candidate_ref_id"] for row in rows] == ["candidate-1", "candidate-2"]
    assert [row["candidate_kind"] for row in rows] == ["source_lead", "source_lead"]
    assert [row["candidate_label"] for row in rows] == [
        "source_lead / proposed / workspace_only",
        "source_lead / rejected / discarded",
    ]
    assert [bool(row["selected"]) for row in rows] == [False, False]


def test_summarize_cycle_evidence_uses_grouped_counts_and_combined_detail_query(
    tmp_path: Path, monkeypatch
) -> None:
    class FakeCursor:
        def __init__(self, row: dict[str, int] | None = None, rows: list[dict[str, object]] | None = None) -> None:
            self._row = row
            self._rows = rows or []

        def fetchone(self) -> dict[str, int] | None:
            return self._row

        def fetchall(self) -> list[dict[str, object]]:
            return self._rows

    class FakeConnection:
        def __init__(self) -> None:
            self.sql: list[str] = []

        def execute(self, sql: str, params: tuple[object, ...] = ()) -> FakeCursor:
            self.sql.append(sql)
            if "cycle_stage_event" in sql and "cycle_operator_override" in sql:
                assert params == ("cycle:test",) * 6
                return FakeCursor(
                    {
                        "stages": 3,
                        "artifacts": 2,
                        "candidates_considered": 4,
                        "candidates_excluded": 5,
                        "tool_failures": 1,
                        "operator_overrides": 2,
                    }
                )
            if "UNION ALL" in sql and "cycle_artifact_ref" in sql:
                assert params == ("cycle:test", "cycle:test")
                return FakeCursor(
                    rows=[
                        {
                            "row_kind": "stage",
                            "stage_event_id": "stage:test",
                            "cycle_event_id": "cycle:test",
                            "run_id": "run:test",
                            "stage_name": "run_gather",
                            "stage_order": 1,
                            "started_at": FIXED_TIMESTAMP,
                            "ended_at": FIXED_TIMESTAMP,
                            "status": "passed",
                            "required_stage": 1,
                            "skipped_reason": None,
                            "command_name": "run_topic_cycle.py",
                            "helper_name": None,
                            "input_artifact_ref_id": None,
                            "output_artifact_ref_id": None,
                            "validation_status": "pass",
                            "error_summary": None,
                            "metadata_json": "{}",
                            "created_at": FIXED_TIMESTAMP,
                            "record_last_updated": FIXED_TIMESTAMP,
                            "artifact_ref_id": None,
                            "artifact_type": None,
                            "artifact_path": None,
                            "artifact_hash": None,
                            "byte_count": None,
                            "privacy_classification": None,
                            "public_safe": None,
                            "schema_id": None,
                        },
                        {
                            "row_kind": "artifact",
                            "stage_event_id": None,
                            "cycle_event_id": "cycle:test",
                            "run_id": None,
                            "stage_name": None,
                            "stage_order": None,
                            "started_at": None,
                            "ended_at": None,
                            "status": None,
                            "required_stage": None,
                            "skipped_reason": None,
                            "command_name": None,
                            "helper_name": None,
                            "input_artifact_ref_id": None,
                            "output_artifact_ref_id": None,
                            "validation_status": "pass",
                            "error_summary": None,
                            "metadata_json": "{}",
                            "created_at": FIXED_TIMESTAMP,
                            "record_last_updated": FIXED_TIMESTAMP,
                            "artifact_ref_id": "artifact:test",
                            "artifact_type": "candidate_batch",
                            "artifact_path": "candidate-batch.json",
                            "artifact_hash": "sha256:" + "a" * 64,
                            "byte_count": 12,
                            "privacy_classification": "local_operator",
                            "public_safe": 0,
                            "schema_id": "gather-candidate-batch.v1",
                        },
                    ]
                )
            assert params == ("cycle:test",)
            raise AssertionError(f"unexpected SQL: {sql}")

    conn = FakeConnection()
    monkeypatch.setattr(
        cycle_evidence_ledger,
        "load_cycle_event",
        lambda _conn, _event_id: {"cycle_event_id": "cycle:test", "status": "completed"},
    )

    summary = cycle_evidence_ledger.summarize_cycle_evidence(conn, "cycle:test")

    assert summary["counts"] == {
        "stages": 3,
        "artifacts": 2,
        "candidates_considered": 4,
        "candidates_excluded": 5,
        "tool_failures": 1,
        "operator_overrides": 2,
    }
    count_queries = [sql for sql in conn.sql if "SELECT COUNT(*) FROM" in sql]
    assert len(count_queries) == 1
    detail_queries = [sql for sql in conn.sql if "UNION ALL" in sql and "cycle_artifact_ref" in sql]
    assert len(detail_queries) == 1
    assert [stage["stage_name"] for stage in summary["stages"]] == ["run_gather"]
    assert [artifact["artifact_type"] for artifact in summary["artifacts"]] == ["candidate_batch"]


def test_record_stage_artifacts_streams_hash_without_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_path = tmp_path / "candidate-batch.json"
    artifact_path.write_text('{"schema_version":"gather-candidate-batch.v1"}\n', encoding="utf-8")
    seen: dict[str, object] = {}

    def fail_read_bytes(self: Path) -> bytes:
        raise AssertionError("artifact hashing should stream bytes instead of read_bytes")

    def fake_record_cycle_artifact_ref(conn, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return "artifact:fixture"

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes, raising=False)
    monkeypatch.setattr(cycle_evidence_ledger, "record_cycle_artifact_ref", fake_record_cycle_artifact_ref)

    cycle_evidence_ledger._record_stage_artifacts(  # type: ignore[attr-defined]
        object(),
        cycle_event_id="cycle:test",
        stage_event_id="stage:test",
        stage={"artifacts": {"candidate_batch": str(artifact_path)}},
    )

    assert str(seen["artifact_hash"]).startswith("sha256:")
    assert seen["schema_id"] is None


def test_record_topic_cycle_manifest_uses_stage_evidence_for_artifacts_and_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = init_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        manifest_path = tmp_path / "topic-cycle-run.json"
        manifest_path.write_text('{"schema_version":"topic-cycle-run.v1"}\n', encoding="utf-8")
        candidate_batch_path = tmp_path / "gather-candidate-batch.json"
        candidate_batch_path.write_text("candidate-batch\n", encoding="utf-8")
        feedback_plan_path = tmp_path / "candidate-feedback-plan.json"
        feedback_plan_path.write_text("feedback-plan\n", encoding="utf-8")
        execution_record_path = tmp_path / "execution-record.json"
        execution_record_path.write_text("execution-record\n", encoding="utf-8")
        capture_events_path = tmp_path / "capture-events.jsonl"
        capture_events_path.write_text("capture-events\n", encoding="utf-8")
        extraction_records_path = tmp_path / "extraction-records.jsonl"
        extraction_records_path.write_text("extraction-records\n", encoding="utf-8")

        candidate_batch_payload = {
            "schema_version": "gather-candidate-batch.v1",
            "facet": {"name": "sources"},
            "candidates": [
                {
                    "candidate_id": "candidate-1",
                    "candidate_type": "source_lead",
                    "review_status": "proposed",
                    "persistence_status": "workspace_only",
                    "origin": {"source": "fixture"},
                }
            ],
        }
        feedback_plan_payload = {
            "schema_version": "candidate-feedback-plan.v1",
            "selection_explanation": {
                "explanation_id": "explanation:fixture",
                "policy": {"policy_id": "policy:fixture"},
                "considered_candidates": [
                    {
                        "candidate_id": "candidate-1",
                        "candidate_type": "feedback_candidate",
                        "label": "keep",
                        "score": 1.0,
                        "rationale": "kept",
                        "reason_codes": ["selected"],
                        "eligibility_status": "eligible",
                        "selected": True,
                    },
                    {
                        "candidate_id": "candidate-2",
                        "candidate_type": "feedback_candidate",
                        "label": "skip",
                        "score": 0.0,
                        "rationale": "defer",
                        "reason_codes": ["deferred"],
                        "eligibility_status": "eligible",
                        "selected": False,
                    },
                ],
                "excluded_candidates": [
                    {
                        "candidate_id": "candidate-3",
                        "candidate_type": "feedback_candidate",
                        "label": "excluded",
                        "reason": "deferred_by_feedback_plan",
                        "retryable": False,
                    }
                ],
            },
        }
        manifest = {
            "schema_version": "topic-cycle-run.v1",
            "run_id": "cycle-evidence-fixture",
            "workspace": {"path": str(tmp_path / "workspace"), "workspace_id": "fixture-workspace"},
            "subject": {"subject_id": "fixture-subject"},
            "domain_pack": {"domain_pack_id": "general.v1"},
            "status": "completed",
            "mode": "local",
            "started_at": FIXED_TIMESTAMP,
            "ended_at": FIXED_TIMESTAMP,
            "cycle_depth": 1,
            "previous_run_ids": [],
            "warnings": [],
            "operator_overrides": [],
            "stages": [
                {
                    "name": "run_gather",
                    "status": "passed",
                    "artifacts": {"candidate_batch": str(candidate_batch_path)},
                },
                {
                    "name": "ingest_candidate_batch",
                    "status": "passed",
                    "artifacts": {},
                    "evidence": {
                        "artifact_schema_ids": {
                            "candidate_batch": "gather-candidate-batch.v1",
                        },
                        "candidate_batch": {
                            "schema_version": "gather-candidate-batch.v1",
                            "facet": {"name": "sources"},
                            "candidates": candidate_batch_payload["candidates"],
                            "artifact_path": str(candidate_batch_path),
                        },
                    },
                },
                {
                    "name": "load_feedback_plan",
                    "status": "passed",
                    "artifacts": {"feedback_plan": str(feedback_plan_path)},
                    "evidence": {
                        "artifact_schema_ids": {
                            "feedback_plan": "candidate-feedback-plan.v1",
                        },
                        "feedback_plan": {
                            "schema_version": "candidate-feedback-plan.v1",
                            "selection_explanation": feedback_plan_payload["selection_explanation"],
                            "next_action": None,
                            "deferred": [],
                            "artifact_path": str(feedback_plan_path),
                        },
                    },
                },
                {
                    "name": "execute_source_adapter",
                    "status": "passed",
                    "artifacts": {
                        "execution_record": str(execution_record_path),
                        "capture_events": str(capture_events_path),
                        "extraction_records": str(extraction_records_path),
                    },
                    "evidence": {
                        "artifact_schema_ids": {
                            "execution_record": "source-acquisition-execution.v1",
                            "capture_events": "source-capture-event.v1",
                            "extraction_records": "source-extraction-record.v1",
                        }
                    },
                },
            ],
            "cycle_evidence_ledger": {"status": "pending"},
        }

        def fail_read_text(self: Path, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("record_topic_cycle_manifest should reuse stage evidence")

        monkeypatch.setattr(Path, "read_text", fail_read_text, raising=False)

        event_id = cycle_evidence_ledger.record_topic_cycle_manifest(
            conn,
            manifest=manifest,
            manifest_path=manifest_path,
            manifest_hash="manifest-hash",
            canonical_db_ref=str(db_path),
        )

        artifact_rows = conn.execute(
            """
            SELECT artifact_type, artifact_path, schema_id
            FROM cycle_artifact_ref
            WHERE cycle_event_id=?
            ORDER BY artifact_type, artifact_path
            """,
            (event_id,),
        ).fetchall()
        artifact_schema_ids = {
            (row["artifact_type"], row["artifact_path"]): row["schema_id"] for row in artifact_rows
        }
        considered_rows = conn.execute(
            """
            SELECT candidate_ref_id, candidate_kind, candidate_label, reason_json
            FROM cycle_candidate_considered
            WHERE cycle_event_id=?
            ORDER BY candidate_ref_id
            """,
            (event_id,),
        ).fetchall()
        excluded_rows = conn.execute(
            """
            SELECT candidate_ref_id, candidate_kind, candidate_label, exclusion_reason
            FROM cycle_candidate_excluded
            WHERE cycle_event_id=?
            ORDER BY candidate_ref_id
            """,
            (event_id,),
        ).fetchall()
    finally:
        conn.close()

    assert artifact_schema_ids[("candidate_batch", str(candidate_batch_path))] == "gather-candidate-batch.v1"
    assert artifact_schema_ids[("feedback_plan", str(feedback_plan_path))] == "candidate-feedback-plan.v1"
    assert artifact_schema_ids[("execution_record", str(execution_record_path))] == "source-acquisition-execution.v1"
    assert artifact_schema_ids[("capture_events", str(capture_events_path))] == "source-capture-event.v1"
    assert artifact_schema_ids[("extraction_records", str(extraction_records_path))] == "source-extraction-record.v1"
    assert {
        (row["candidate_ref_id"], row["candidate_kind"])
        for row in considered_rows
    } == {
        ("candidate-1", "source_lead"),
        ("candidate-1", "feedback_candidate"),
        ("candidate-2", "feedback_candidate"),
    }
    assert [row["candidate_ref_id"] for row in excluded_rows] == ["candidate-3"]
    assert excluded_rows[0]["candidate_kind"] == "feedback_candidate"


def test_record_topic_cycle_manifest_persists_cycle_metrics_for_loop_health(
    tmp_path: Path,
) -> None:
    db_path = init_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        manifest_path = tmp_path / "topic-cycle-run.json"
        candidate_batch_path = tmp_path / "gather-candidate-batch.json"
        candidate_batch_path.write_text("candidate-batch\n", encoding="utf-8")
        manifest_path.write_text('{"schema_version":"topic-cycle-run.v1"}\n', encoding="utf-8")

        gather_event = canonical_store.record_provenance_event(
            conn,
            object_namespace="gather_candidate_batch",
            object_id="cycle-evidence-metrics.gather",
            event_type="gather_candidate_batch_ingest",
            tool_name="pytest",
            run_id="cycle-evidence-metrics.gather",
            event_timestamp=FIXED_TIMESTAMP,
            note_text=json.dumps(
                {
                    "subject_id": "fixture-subject",
                    "workspace_id": "fixture-workspace",
                    "facet": "sources",
                },
                sort_keys=True,
            ),
            provenance_event_key_v1="prov:cycle-evidence:metrics:gather",
        )
        canonical_store.upsert_work(
            conn,
            work_key_v1="work:cycle-evidence-metrics",
            provenance_event_ref=gather_event.event_key,
            work_type="article",
            title="Cycle Evidence Metrics",
            review_state="accepted",
            workspace_id="fixture-workspace",
            first_seen_at=FIXED_TIMESTAMP,
            last_seen_at=FIXED_TIMESTAMP,
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        canonical_store.record_source_claim(
            conn,
            provenance_event_ref=gather_event.event_key,
            source_claim_key_v1="claim:cycle-evidence-metrics",
            about_object_ref="work:cycle-evidence-metrics",
            claim_text="Cycle evidence metric claim.",
            claim_type="fixture_claim",
            review_state="needs_review",
            workspace_id="fixture-workspace",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )
        canonical_store.record_source_relationship(
            conn,
            provenance_event_ref=gather_event.event_key,
            from_object_ref="work:cycle-evidence-metrics",
            to_object_ref="claim:cycle-evidence-metrics",
            predicate="contradicts",
            review_state="needs_review",
            workspace_id="fixture-workspace",
            created_at=FIXED_TIMESTAMP,
            record_last_updated=FIXED_TIMESTAMP,
        )

        manifest = {
            "schema_version": "topic-cycle-run.v1",
            "run_id": "cycle-evidence-metrics",
            "workspace": {"path": str(tmp_path / "workspace"), "workspace_id": "fixture-workspace"},
            "subject": {"subject_id": "fixture-subject"},
            "domain_pack": {"domain_pack_id": "general.v1"},
            "status": "completed",
            "mode": "local",
            "started_at": FIXED_TIMESTAMP,
            "ended_at": FIXED_TIMESTAMP,
            "cycle_depth": 1,
            "previous_run_ids": [],
            "warnings": [],
            "operator_overrides": [],
            "stages": [
                {
                    "name": "run_gather",
                    "status": "passed",
                    "artifacts": {"candidate_batch": str(candidate_batch_path)},
                },
                {
                    "name": "ingest_candidate_batch",
                    "status": "passed",
                    "artifacts": {"candidate_batch": str(candidate_batch_path)},
                    "evidence": {
                        "candidate_batch": {
                            "schema_version": "gather-candidate-batch.v1",
                            "facet": {"name": "sources"},
                            "candidates": [
                                {
                                    "candidate_id": "candidate-1",
                                    "candidate_type": "source_lead",
                                    "origin": {"source": "fixture"},
                                }
                            ],
                            "artifact_path": str(candidate_batch_path),
                        }
                    },
                },
            ],
            "cycle_evidence_ledger": {"status": "pending"},
        }

        event_id = cycle_evidence_ledger.record_topic_cycle_manifest(
            conn,
            manifest=manifest,
            manifest_path=manifest_path,
            manifest_hash="manifest-hash",
            canonical_db_ref=str(db_path),
        )
        row = conn.execute(
            "SELECT row_count_delta_json FROM cycle_event WHERE cycle_event_id=?",
            (event_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    metrics = json.loads(row["row_count_delta_json"])
    assert metrics["facet"] == "sources"
    assert metrics["new_work_count"] == 1
    assert metrics["new_source_claim_count"] == 1
    assert metrics["new_source_relationship_count"] == 1
    assert metrics["new_contradiction_count"] == 1
    assert metrics["new_reviewable_count"] == 2
    assert metrics["new_accepted_count"] == 1
