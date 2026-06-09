from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.common import topic_saturation
from tools.source_db_tools import authority_reconciliation, canonical_store

REPO_ROOT = Path(__file__).resolve().parents[1]
EVALUATOR = REPO_ROOT / "tools" / "scripts" / "evaluate_topic_saturation.py"
SELECTOR = REPO_ROOT / "tools" / "scripts" / "select_scheduled_workspaces.py"
FIXED_TIMESTAMP = "2026-06-04T12:00:00Z"


def load_evaluator_module():
    spec = importlib.util.spec_from_file_location("evaluate_topic_saturation", EVALUATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.topic_saturation",
    )
    return db_path


def policy_payload(**overrides: object) -> dict[str, object]:
    payload = json.loads(
        (REPO_ROOT / "config" / "topic_saturation_policy.v1.json").read_text(encoding="utf-8")
    )
    payload.update(overrides)
    return payload


def write_policy(tmp_path: Path, **overrides: object) -> Path:
    index = len(list(tmp_path.glob("topic_saturation_policy*.json")))
    path = tmp_path / f"topic_saturation_policy{index}.json"
    path.write_text(
        json.dumps(policy_payload(**overrides), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def write_manifest(workspace_root: Path, *, subject_id: str) -> Path:
    manifest_path = workspace_root / ".indexer" / "subject_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "subject-manifest.v1",
                "subject_id": subject_id,
                "display_name": subject_id.replace("_", " ").title(),
                "domain_pack": "general.v1",
                "scope_statement": "Topic saturation fixture.",
                "languages": ["en"],
                "aliases": [subject_id],
                "disambiguation_terms": ["fixture"],
                "excluded_senses": ["non-fixture"],
                "enabled_facets": ["sources"],
                "query_families": ["web_search"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def workspace_record(
    *,
    workspace_id: str,
    workspace_root: Path,
    manifest_path: Path,
    scheduler_policy: dict[str, object] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "workspace_id": workspace_id,
        "topic_label": workspace_id.replace("_", " ").title(),
        "workspace_root": str(workspace_root),
        "domain_pack": "general.v1",
        "lifecycle_state": "active",
        "schedule_posture": "scheduled",
        "workspace_policy_class": "private_local",
        "default_subject_manifest": str(manifest_path),
    }
    if scheduler_policy is not None:
        record["scheduler_policy"] = scheduler_policy
    return record


def write_registry(tmp_path: Path, workspaces: list[dict[str, object]]) -> Path:
    registry_path = tmp_path / "topic_workspaces.local.json"
    registry_path.write_text(
        json.dumps(
            {"schema_version": "topic-workspace-registry.v1", "workspaces": workspaces},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return registry_path


def add_cycle(
    conn,
    *,
    subject_id: str,
    run_id: str,
    cycle_depth: int,
    event_index: int,
    event_timestamp: str | None = None,
    review_state: str | None = None,
    artifact_hash: str | None = None,
) -> str:
    timestamp = event_timestamp or f"2026-06-04T12:0{event_index}:00Z"
    note = {
        "subject_id": subject_id,
        "facet": "sources",
        "cycle_depth": cycle_depth,
        "prompt_bundle_id": "general.sources.v1",
    }
    if artifact_hash is not None:
        note["artifact_hash"] = artifact_hash
    provenance = canonical_store.record_provenance_event(
        conn,
        object_namespace="gather_candidate_batch",
        object_id=run_id,
        event_type="gather_candidate_batch_ingest",
        tool_name="tests/test_topic_saturation_policy.py",
        run_id=run_id,
        event_timestamp=timestamp,
        source_object_namespace=topic_saturation.GATHER_EVENT_SOURCE_NAMESPACE,
        source_object_id=subject_id,
        note_text=json.dumps(note, sort_keys=True),
        provenance_event_key_v1=f"prov:saturation:{subject_id}:{run_id}",
    )
    if review_state is not None:
        canonical_store.record_source_claim(
            conn,
            provenance_event_ref=provenance.event_key,
            source_claim_key_v1=f"claim:saturation:{subject_id}:{run_id}",
            about_object_ref=f"subject:{subject_id}",
            claim_text=f"Reviewable claim from {run_id}.",
            claim_type="fixture_claim",
            review_state=review_state,
            workspace_id=subject_id,
            created_at=timestamp,
            record_last_updated=timestamp,
        )
    return provenance.event_key


def evaluate(
    db_path: Path, *, subject_id: str, policy_path: Path, workspace_id: str | None = None
) -> dict[str, object]:
    conn = canonical_store.connect_existing_read_only(db_path)
    try:
        return topic_saturation.evaluate_saturation(
            conn,
            workspace_id=workspace_id or subject_id,
            subject_id=subject_id,
            policy=topic_saturation.load_policy(policy_path),
            evaluated_at=FIXED_TIMESTAMP,
        )
    finally:
        conn.close()


def test_policy_validation_rejects_negative_threshold(tmp_path: Path) -> None:
    bad = policy_payload(min_new_reviewable_records=-1)
    with pytest.raises(topic_saturation.TopicSaturationError, match="min_new_reviewable_records"):
        topic_saturation.validate_policy(bad)  # type: ignore[arg-type]


def test_disabled_policy_returns_active_run_state(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, enabled=False)
    result = evaluate(db_path, subject_id="disabled_subject", policy_path=policy)
    assert result["state"] == "active"
    assert result["scheduler_action"] == "run"
    assert result["reason_codes"] == ["policy_disabled"]


def test_bootstrap_topic_with_insufficient_history_is_not_saturated(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, lookback_cycles=3)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            add_cycle(
                conn, subject_id="bootstrap_subject", run_id="run-1", cycle_depth=1, event_index=1
            )
    finally:
        conn.close()
    result = evaluate(db_path, subject_id="bootstrap_subject", policy_path=policy)
    assert result["state"] == "active_bootstrap"
    assert result["scheduler_action"] == "run"
    assert "insufficient_history" in result["reason_codes"]


def test_active_topic_with_reviewable_yield_stays_runnable(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, lookback_cycles=2)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            add_cycle(
                conn,
                subject_id="active_subject",
                run_id="run-1",
                cycle_depth=1,
                event_index=1,
                review_state="needs_review",
            )
            add_cycle(
                conn,
                subject_id="active_subject",
                run_id="run-2",
                cycle_depth=2,
                event_index=2,
                review_state="needs_review",
            )
    finally:
        conn.close()
    result = evaluate(db_path, subject_id="active_subject", policy_path=policy)
    assert result["state"] == "active"
    assert result["scheduler_action"] == "run"
    assert result["recent_yield_summary"]["new_reviewable_records"] == 2  # type: ignore[index]


def test_evaluate_saturations_batches_multiple_subjects_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, lookback_cycles=1)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            add_cycle(
                conn,
                subject_id="subject.one",
                run_id="run-1",
                cycle_depth=1,
                event_index=1,
                review_state="needs_review",
            )
            add_cycle(
                conn,
                subject_id="subject.two",
                run_id="run-2",
                cycle_depth=1,
                event_index=2,
                review_state="needs_review",
            )
    finally:
        conn.close()

    cycle_call_count = {"count": 0}
    original_cycle_yields = topic_saturation.cycle_yields

    def counting_cycle_yields(*args: object, **kwargs: object) -> list[dict[str, object]]:
        cycle_call_count["count"] += 1
        return original_cycle_yields(*args, **kwargs)

    monkeypatch.setattr(topic_saturation, "cycle_yields", counting_cycle_yields)

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        results = topic_saturation.evaluate_saturations(
            conn,
            workspace_subject_pairs=[
                ("workspace.one", "subject.one"),
                ("workspace.two", "subject.two"),
            ],
            policy=topic_saturation.load_policy(policy),
            evaluated_at=FIXED_TIMESTAMP,
        )
    finally:
        conn.close()

    assert cycle_call_count["count"] == 1
    assert set(results) == {"workspace.one", "workspace.two"}
    assert results["workspace.one"]["subject_id"] == "subject.one"
    assert results["workspace.two"]["subject_id"] == "subject.two"


def test_load_recent_gather_events_orders_by_normalized_timestamp(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            late_utc_event = add_cycle(
                conn,
                subject_id="timestamp_subject",
                run_id="run-late-utc",
                cycle_depth=1,
                event_index=1,
                event_timestamp="2026-06-04T09:00:00-03:00",
            )
            add_cycle(
                conn,
                subject_id="timestamp_subject",
                run_id="run-early-utc",
                cycle_depth=1,
                event_index=2,
                event_timestamp="2026-06-04T11:30:00Z",
            )
        events = topic_saturation.load_recent_gather_events(
            conn,
            subject_id="timestamp_subject",
            limit=1,
        )
    finally:
        conn.close()

    assert len(events) == 1
    assert events[0]["event_key"] == late_utc_event
    assert events[0]["event_timestamp"] == "2026-06-04T09:00:00-03:00"


def test_rejected_source_access_does_not_count_as_useful_yield(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, lookback_cycles=1)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            event_key = add_cycle(
                conn,
                subject_id="rejected_source_access_subject",
                run_id="run-1",
                cycle_depth=1,
                event_index=1,
                artifact_hash="rejected-source-access",
            )
            canonical_store.record_source_access(
                conn,
                provenance_event_ref=event_key,
                source_lead_id="source-lead:rejected-source-access:001",
                original_locator="https://example.test/rejected-source-access",
                review_state="rejected",
                workspace_id="rejected_source_access_subject",
                first_seen_at=FIXED_TIMESTAMP,
                last_seen_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
        event = topic_saturation.load_recent_gather_events(
            conn,
            subject_id="rejected_source_access_subject",
            limit=1,
        )[0]
        cycle = topic_saturation.cycle_yield(
            conn,
            event=event,
            policy=topic_saturation.load_policy(policy),
        )
    finally:
        conn.close()

    assert cycle["new_accepted_records"] == 0
    assert cycle["new_reviewable_records"] == 0
    assert cycle["family_counts"]["source_access"] == 0
    assert cycle["useful_yield"] == 0.0
    assert cycle["low_yield"] is True


def test_source_access_count_for_event_includes_direct_provenance_event_ref(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            event_key = add_cycle(
                conn,
                subject_id="source_access_subject",
                run_id="run-1",
                cycle_depth=1,
                event_index=1,
            )
            canonical_store.record_source_access(
                conn,
                original_locator="https://example.test/source-access",
                provenance_event_ref=event_key,
                review_state="accepted",
                workspace_id="source_access_subject",
                first_seen_at=FIXED_TIMESTAMP,
                last_seen_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
        count = topic_saturation.source_access_count_for_event(conn, event_key)
    finally:
        conn.close()

    assert count == 1


def test_authority_reconciliation_counts_toward_useful_yield(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, lookback_cycles=1)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            event_key = add_cycle(
                conn,
                subject_id="authority_reconciliation_subject",
                run_id="run-1",
                cycle_depth=1,
                event_index=1,
            )
            authority_id = authority_reconciliation.create_local_authority(
                conn,
                authority_type="person",
                preferred_label="Jane Smith",
                source_namespace="pytest",
                source_id="authority:Jane-Smith",
                review_state="accepted",
                confidence_score=1.0,
                created_at=FIXED_TIMESTAMP,
            )
            entity = canonical_store.record_extraction_detected_entity(
                conn,
                provenance_event_ref=event_key,
                entity_label="Jane Smith",
                normalized_label="jane smith",
                entity_type="person",
                review_state="proposed",
                confidence_score=0.8,
                workspace_id="authority_reconciliation_subject",
                record_last_updated=FIXED_TIMESTAMP,
            )
            authority_reconciliation.propose_candidate(
                conn,
                detected_entity_id=entity.row_id,
                raw_label="Jane Smith",
                entity_type="person",
                candidate_authority_id=authority_id,
                match_method="exact_identifier",
                match_score=0.95,
                evidence_context="fixture authority reconciliation",
                review_state="proposed",
                created_at=FIXED_TIMESTAMP,
            )
        event = topic_saturation.load_recent_gather_events(
            conn,
            subject_id="authority_reconciliation_subject",
            limit=1,
        )[0]
        cycle = topic_saturation.cycle_yield(
            conn,
            event=event,
            policy=topic_saturation.load_policy(policy),
        )
    finally:
        conn.close()

    assert cycle["family_counts"]["authority_reconciliation"] == 1
    assert cycle["useful_yield"] > 0.0


def test_saturated_topic_with_consecutive_low_yield_is_deprioritized(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, lookback_cycles=2, scheduler_action_on_saturated="deprioritize")
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            add_cycle(
                conn, subject_id="saturated_subject", run_id="run-1", cycle_depth=1, event_index=1
            )
            add_cycle(
                conn, subject_id="saturated_subject", run_id="run-2", cycle_depth=2, event_index=2
            )
    finally:
        conn.close()
    result = evaluate(db_path, subject_id="saturated_subject", policy_path=policy)
    assert result["state"] == "saturated"
    assert result["scheduler_action"] == "deprioritize"
    assert "consecutive_low_yield" in result["reason_codes"]


def test_configurable_threshold_changes_state(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            add_cycle(
                conn,
                subject_id="threshold_subject",
                run_id="run-1",
                cycle_depth=1,
                event_index=1,
                review_state="needs_review",
            )
            add_cycle(
                conn,
                subject_id="threshold_subject",
                run_id="run-2",
                cycle_depth=2,
                event_index=2,
                review_state="needs_review",
            )
    finally:
        conn.close()
    low_threshold = write_policy(tmp_path, lookback_cycles=2, min_new_reviewable_records=1)
    high_threshold = write_policy(
        tmp_path, lookback_cycles=2, min_new_reviewable_records=2, min_useful_yield=10.0
    )
    assert (
        evaluate(db_path, subject_id="threshold_subject", policy_path=low_threshold)["state"]
        == "active"
    )
    assert (
        evaluate(db_path, subject_id="threshold_subject", policy_path=high_threshold)["state"]
        == "saturated"
    )


def test_review_backlog_pressure_reason_is_recorded(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, lookback_cycles=2, review_backlog_pressure_threshold=1)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            add_cycle(
                conn, subject_id="backlog_subject", run_id="run-1", cycle_depth=1, event_index=1
            )
            add_cycle(
                conn,
                subject_id="backlog_subject",
                run_id="run-2",
                cycle_depth=2,
                event_index=2,
                review_state="needs_review",
            )
    finally:
        conn.close()
    result = evaluate(db_path, subject_id="backlog_subject", policy_path=policy)
    assert "review_backlog_pressure" in result["reason_codes"]
    assert result["recent_yield_summary"]["review_backlog_count"] == 1  # type: ignore[index]


def test_accepted_only_mode_saturates_without_accepted_records(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, lookback_cycles=2, mode="accepted_only")
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            add_cycle(
                conn,
                subject_id="accepted_only_subject",
                run_id="run-1",
                cycle_depth=1,
                event_index=1,
                review_state="needs_review",
            )
            add_cycle(
                conn,
                subject_id="accepted_only_subject",
                run_id="run-2",
                cycle_depth=2,
                event_index=2,
                review_state="needs_review",
            )
    finally:
        conn.close()
    assert (
        evaluate(db_path, subject_id="accepted_only_subject", policy_path=policy)["state"]
        == "saturated"
    )


def test_reviewable_yield_mode_keeps_unaccepted_reviewable_topic_active(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, lookback_cycles=2, mode="reviewable_yield")
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            add_cycle(
                conn,
                subject_id="reviewable_subject",
                run_id="run-1",
                cycle_depth=1,
                event_index=1,
                review_state="needs_review",
            )
            add_cycle(
                conn,
                subject_id="reviewable_subject",
                run_id="run-2",
                cycle_depth=2,
                event_index=2,
                review_state="needs_review",
            )
    finally:
        conn.close()
    assert (
        evaluate(db_path, subject_id="reviewable_subject", policy_path=policy)["state"] == "active"
    )


def test_selector_deprioritizes_saturated_workspace_and_records_reason(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, lookback_cycles=2, scheduler_action_on_saturated="deprioritize")
    active_root = tmp_path / "workspaces" / "active_subject"
    saturated_root = tmp_path / "workspaces" / "saturated_subject"
    active_manifest = write_manifest(active_root, subject_id="active_subject")
    saturated_manifest = write_manifest(saturated_root, subject_id="saturated_subject")
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            add_cycle(
                conn,
                subject_id="active_subject",
                run_id="run-1",
                cycle_depth=1,
                event_index=1,
                review_state="needs_review",
            )
            add_cycle(
                conn,
                subject_id="active_subject",
                run_id="run-2",
                cycle_depth=2,
                event_index=2,
                review_state="needs_review",
            )
            add_cycle(
                conn, subject_id="saturated_subject", run_id="run-1", cycle_depth=1, event_index=1
            )
            add_cycle(
                conn, subject_id="saturated_subject", run_id="run-2", cycle_depth=2, event_index=2
            )
    finally:
        conn.close()
    registry = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="saturated_subject",
                workspace_root=saturated_root,
                manifest_path=saturated_manifest,
            ),
            workspace_record(
                workspace_id="active_subject",
                workspace_root=active_root,
                manifest_path=active_manifest,
            ),
        ],
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(SELECTOR),
            "--registry",
            str(registry),
            "--db",
            str(db_path),
            "--saturation-policy",
            str(policy),
            "--limit",
            "1",
            "--planner-run-id",
            "planner-saturation",
            "--planned-at",
            FIXED_TIMESTAMP,
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["selected_workspaces"][0]["workspace_id"] == "active_subject"
    skipped = {item["workspace_id"]: item for item in payload["skipped_workspaces"]}
    assert skipped["saturated_subject"]["saturation"]["scheduler_action"] == "deprioritize"
    assert skipped["saturated_subject"]["reasons"] == [
        "selection limit reached after saturation deprioritization"
    ]


def test_selector_override_includes_halted_saturated_workspace(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, lookback_cycles=2, scheduler_action_on_saturated="halt")
    root = tmp_path / "workspaces" / "halted_subject"
    manifest = write_manifest(root, subject_id="halted_subject")
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            add_cycle(
                conn, subject_id="halted_subject", run_id="run-1", cycle_depth=1, event_index=1
            )
            add_cycle(
                conn, subject_id="halted_subject", run_id="run-2", cycle_depth=2, event_index=2
            )
    finally:
        conn.close()
    registry = write_registry(
        tmp_path,
        [
            workspace_record(
                workspace_id="halted_subject", workspace_root=root, manifest_path=manifest
            )
        ],
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(SELECTOR),
            "--registry",
            str(registry),
            "--db",
            str(db_path),
            "--saturation-policy",
            str(policy),
            "--include-saturated",
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["selected_count"] == 1
    assert payload["planned_run_records"][0]["saturation_override"] is True


def test_evaluator_cli_outputs_saturation_json(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, lookback_cycles=2)
    root = tmp_path / "workspaces" / "cli_subject"
    write_manifest(root, subject_id="cli_subject")
    proc = subprocess.run(
        [
            sys.executable,
            str(EVALUATOR),
            "--workspace",
            str(root),
            "--db",
            str(db_path),
            "--policy",
            str(policy),
            "--evaluated-at",
            FIXED_TIMESTAMP,
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(proc.stdout)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert payload["schema_version"] == "topic-saturation.v1"
    assert payload["state"] == "active_bootstrap"


def test_evaluator_main_uses_atomic_output_and_strict_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_evaluator_module()
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, lookback_cycles=1)
    root = tmp_path / "workspaces" / "cli_subject"
    write_manifest(root, subject_id="cli_subject")
    output_json = tmp_path / "output" / "saturation.json"
    atomic_calls: list[tuple[Path, dict[str, object]]] = []

    def fake_atomic_write_json(path: Path, payload: dict[str, object]) -> None:
        atomic_calls.append((path, payload))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(module, "atomic_write_json", fake_atomic_write_json)
    monkeypatch.setattr(module.sys, "stdout", io.StringIO())

    exit_code = module.main(
        [
            "--workspace",
            str(root),
            "--db",
            str(db_path),
            "--policy",
            str(policy),
            "--evaluated-at",
            FIXED_TIMESTAMP,
            "--output-json",
            str(output_json),
            "--format",
            "json",
        ]
    )

    assert exit_code == 0
    assert len(atomic_calls) == 1
    assert atomic_calls[0][0] == output_json
    stdout_payload = json.loads(module.sys.stdout.getvalue())
    assert stdout_payload == atomic_calls[0][1]
    assert output_json.is_file()


def test_evaluate_saturation_uses_workspace_id_for_backlog_count(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, lookback_cycles=1)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            provenance = add_cycle(
                conn,
                subject_id="subject.alpha",
                run_id="run-1",
                cycle_depth=1,
                event_index=1,
            )
            canonical_store.record_source_claim(
                conn,
                provenance_event_ref=provenance,
                source_claim_key_v1="claim:workspace-mismatch",
                about_object_ref="subject:workspace-alpha",
                claim_text="Workspace-scoped backlog claim.",
                claim_type="fixture_claim",
                review_state="needs_review",
                workspace_id="workspace.alpha",
                created_at=FIXED_TIMESTAMP,
                record_last_updated=FIXED_TIMESTAMP,
            )
        result = topic_saturation.evaluate_saturation(
            conn,
            workspace_id="workspace.alpha",
            subject_id="subject.alpha",
            policy=topic_saturation.load_policy(policy),
            evaluated_at=FIXED_TIMESTAMP,
        )
    finally:
        conn.close()

    assert result["workspace_id"] == "workspace.alpha"
    assert result["recent_yield_summary"]["review_backlog_count"] == 1  # type: ignore[index]


def test_evaluator_is_read_only(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path)
    before = db_path.read_bytes()
    evaluate(db_path, subject_id="readonly_subject", policy_path=policy)
    assert db_path.read_bytes() == before


def test_evaluate_saturation_batches_cycle_count_queries(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    policy = write_policy(tmp_path, lookback_cycles=3)
    conn = canonical_store.connect_canonical_store(db_path)
    executed_sql: list[str] = []
    try:
        with conn:
            add_cycle(
                conn,
                subject_id="batched_subject",
                run_id="run-1",
                cycle_depth=1,
                event_index=1,
                artifact_hash="artifact-one",
            )
            add_cycle(
                conn,
                subject_id="batched_subject",
                run_id="run-2",
                cycle_depth=2,
                event_index=2,
                artifact_hash="artifact-two",
            )
            add_cycle(
                conn,
                subject_id="batched_subject",
                run_id="run-3",
                cycle_depth=3,
                event_index=3,
                artifact_hash="artifact-three",
            )
        conn.set_trace_callback(executed_sql.append)
        result = topic_saturation.evaluate_saturation(
            conn,
            workspace_id="batched_subject",
            subject_id="batched_subject",
            policy=topic_saturation.load_policy(policy),
            evaluated_at=FIXED_TIMESTAMP,
        )
    finally:
        conn.set_trace_callback(None)
        conn.close()

    assert result["recent_yield_summary"]["cycle_count"] == 3  # type: ignore[index]
    assert (
        sum(
            "SELECT COUNT(*) AS count FROM work WHERE provenance_event_ref=?" in sql
            for sql in executed_sql
        )
        == 0
    )
    assert (
        sum(
            "SELECT COUNT(*) AS count FROM source_claim WHERE provenance_event_ref=?" in sql
            for sql in executed_sql
        )
        == 0
    )
    assert (
        sum(
            "SELECT COUNT(*) AS count FROM extraction_detected_entity WHERE provenance_event_ref=?"
            in sql
            for sql in executed_sql
        )
        == 0
    )
    assert (
        sum(
            "SELECT COUNT(*) AS count FROM source_relationship WHERE provenance_event_ref=?" in sql
            for sql in executed_sql
        )
        == 0
    )
    assert (
        sum(
            "SELECT COUNT(*) AS count FROM capture_event WHERE provenance_event_ref=?" in sql
            for sql in executed_sql
        )
        == 0
    )
    assert (
        sum(
            "SELECT COUNT(*) AS count FROM extraction_record WHERE provenance_event_ref=?" in sql
            for sql in executed_sql
        )
        == 0
    )
    assert (
        sum("WITH requested_events(event_key, artifact_hash) AS" in sql for sql in executed_sql)
        == 1
    )


def test_load_recent_gather_events_uses_structured_source_object_filters(tmp_path: Path) -> None:
    db_path = bootstrap_db(tmp_path)
    subject_id = "subject_%_literal"
    other_subject_id = "subjectXliteral"
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            add_cycle(conn, subject_id=subject_id, run_id="run-1", cycle_depth=1, event_index=1)
            add_cycle(
                conn, subject_id=other_subject_id, run_id="run-2", cycle_depth=1, event_index=2
            )
        traces: list[str] = []
        conn.set_trace_callback(traces.append)
        events = topic_saturation.load_recent_gather_events(conn, subject_id=subject_id, limit=10)
    finally:
        conn.set_trace_callback(None)
        conn.close()

    assert [event["event_key"] for event in events] == [f"prov:saturation:{subject_id}:run-1"]
    assert any("source_object_namespace" in statement for statement in traces)
    assert any("source_object_id" in statement for statement in traces)
    assert not any("LIKE" in statement for statement in traces)


def test_load_recent_gather_events_uses_sql_json_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = bootstrap_db(tmp_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            add_cycle(
                conn,
                subject_id="json_extract_subject",
                run_id="run-1",
                cycle_depth=7,
                event_index=1,
                artifact_hash="artifact-hash",
            )
        monkeypatch.setattr(
            topic_saturation,
            "parse_note_text",
            lambda _: (_ for _ in ()).throw(AssertionError("parse_note_text should not be called")),
        )
        events = topic_saturation.load_recent_gather_events(
            conn, subject_id="json_extract_subject", limit=1
        )
    finally:
        conn.close()

    assert len(events) == 1
    assert events[0]["facet"] == "sources"
    assert events[0]["cycle_depth"] == 7
    assert events[0]["_artifact_hash"] == "artifact-hash"
