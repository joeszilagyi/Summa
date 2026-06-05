from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from tools.source_db_tools import canonical_ingest, canonical_store

REPO_ROOT = Path(__file__).resolve().parents[1]
PLANNER_PATH = REPO_ROOT / "tools" / "scripts" / "build_candidate_feedback_plan.py"
DRIVER_PATH = REPO_ROOT / "tools" / "scripts" / "run_topic_gather.py"
VALIDATOR_PATH = REPO_ROOT / "tools" / "validators" / "validate_candidate_feedback_plan.py"
FIXED_CREATED_AT = "2026-06-03T12:34:56Z"

validator_spec = importlib.util.spec_from_file_location(
    "candidate_feedback_validator_for_selection_tests",
    VALIDATOR_PATH,
)
validator = importlib.util.module_from_spec(validator_spec)
assert validator_spec.loader is not None
validator_spec.loader.exec_module(validator)


def run_planner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PLANNER_PATH), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def run_driver(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DRIVER_PATH), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_CREATED_AT,
        applied_by="pytest.candidate_feedback_selection",
    )
    return db_path


def write_manifest(
    workspace_root: Path,
    *,
    subject_id: str,
    domain_pack: str = "general.v1",
) -> Path:
    pack = json.loads(
        (REPO_ROOT / "config" / "domain_packs" / f"{domain_pack}.json").read_text(encoding="utf-8")
    )
    manifest_path = workspace_root / ".indexer" / "subject_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "subject-manifest.v1",
                "subject_id": subject_id,
                "display_name": f"{subject_id} fixture",
                "domain_pack": domain_pack,
                "scope_statement": "Synthetic candidate feedback selection fixture manifest.",
                "languages": ["en"],
                "aliases": [],
                "disambiguation_terms": [],
                "excluded_senses": [],
                "enabled_facets": list(pack["enabled_facets"]),
                "query_families": list(pack["query_families"]),
                "public_export_default": False,
                "legacy_substrate_paths": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def planner_output_path(tmp_path: Path, name: str = "candidate-feedback-plan.json") -> Path:
    return tmp_path / name


def batch_path_for(workspace_root: Path, run_id: str) -> Path:
    return workspace_root / "runs" / "gather" / run_id / "gather-candidate-batch.json"


def prompt_path_for(workspace_root: Path, run_id: str) -> Path:
    return workspace_root / "runs" / "gather" / run_id / "rendered-prompt.txt"


def gather_note(*, subject_id: str, facet: str, run_id: str, cycle_depth: int, prompt_bundle_id: str) -> str:
    return json.dumps(
        {
            "artifact_hash": f"artifact:{run_id}",
            "artifact_path": f"runs/gather/{run_id}/gather-candidate-batch.json",
            "candidate_count": 0,
            "cycle_depth": cycle_depth,
            "domain_pack": "general.v1",
            "facet": facet,
            "iteration_mode": "prior_state" if cycle_depth > 1 else "one_shot",
            "mode": "dry_run",
            "previous_run_ids": [],
            "prompt_bundle_id": prompt_bundle_id,
            "run_id": run_id,
            "schema_version": "gather-candidate-batch.v1",
            "subject_id": subject_id,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def seed_feedback_state(db_path: Path, *, subject_id: str) -> dict[str, str]:
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            high_gather = canonical_store.record_provenance_event(
                conn,
                object_namespace="gather_candidate_batch",
                object_id="cycle-one-sources-high",
                event_type="gather_candidate_batch_ingest",
                tool_name="pytest.candidate_feedback_selection",
                run_id="cycle-one-sources-high",
                event_timestamp="2026-06-02T10:00:00Z",
                note_text=gather_note(
                    subject_id=subject_id,
                    facet="sources",
                    run_id="cycle-one-sources-high",
                    cycle_depth=1,
                    prompt_bundle_id="general.gather.sources.v1",
                ),
                provenance_event_key_v1=f"prov:feedback:{subject_id}:sources-high",
            )
            low_gather = canonical_store.record_provenance_event(
                conn,
                object_namespace="gather_candidate_batch",
                object_id="cycle-one-sources-low",
                event_type="gather_candidate_batch_ingest",
                tool_name="pytest.candidate_feedback_selection",
                run_id="cycle-one-sources-low",
                event_timestamp="2026-06-02T11:00:00Z",
                note_text=gather_note(
                    subject_id=subject_id,
                    facet="sources",
                    run_id="cycle-one-sources-low",
                    cycle_depth=1,
                    prompt_bundle_id="general.gather.sources.v1",
                ),
                provenance_event_key_v1=f"prov:feedback:{subject_id}:sources-low",
            )
            open_question_gather = canonical_store.record_provenance_event(
                conn,
                object_namespace="gather_candidate_batch",
                object_id="cycle-one-open-questions",
                event_type="gather_candidate_batch_ingest",
                tool_name="pytest.candidate_feedback_selection",
                run_id="cycle-one-open-questions",
                event_timestamp="2026-06-02T12:00:00Z",
                note_text=gather_note(
                    subject_id=subject_id,
                    facet="open_questions",
                    run_id="cycle-one-open-questions",
                    cycle_depth=1,
                    prompt_bundle_id="general.gather.open_questions.v1",
                ),
                provenance_event_key_v1=f"prov:feedback:{subject_id}:open-questions",
            )
            high_work = canonical_store.upsert_work(
                conn,
                work_key_v1=f"work:{subject_id}:high",
                provenance_event_ref=high_gather.event_key,
                work_type="article",
                title="High Yield Work",
                review_state="accepted",
                confidence_score=0.94,
                workspace_id=subject_id,
                first_seen_at="2026-06-02T10:00:00Z",
                last_seen_at="2026-06-02T10:00:00Z",
                created_at="2026-06-02T10:00:00Z",
                record_last_updated="2026-06-02T10:00:00Z",
            )
            canonical_store.record_source_access(
                conn,
                provenance_event_ref=high_gather.event_key,
                work_id=high_work.row_id,
                source_locus_id="locus:high",
                source_lead_id="lead:high",
                original_locator="https://example.test/high",
                canonical_url="https://example.test/high",
                citation_hint="High yield source lead",
                workspace_id=subject_id,
                review_state="needs_review",
                first_seen_at="2026-06-02T10:00:00Z",
                last_seen_at="2026-06-02T10:00:00Z",
                record_last_updated="2026-06-02T10:00:00Z",
            )
            canonical_store.record_source_claim(
                conn,
                provenance_event_ref=high_gather.event_key,
                source_claim_key_v1=f"claim:{subject_id}:high",
                about_object_ref=f"work:{high_work.row_id}",
                claim_text="High lead produced a concrete follow-up claim.",
                claim_type="lead_claim",
                workspace_id=subject_id,
                review_state="needs_review",
                created_at="2026-06-02T10:00:00Z",
                record_last_updated="2026-06-02T10:00:00Z",
            )
            canonical_store.record_source_access(
                conn,
                provenance_event_ref=low_gather.event_key,
                source_locus_id="locus:low",
                source_lead_id="lead:low",
                original_locator="https://example.test/low",
                canonical_url="https://example.test/low",
                citation_hint="Low yield source lead",
                workspace_id=subject_id,
                review_state="needs_review",
                first_seen_at="2026-06-02T11:00:00Z",
                last_seen_at="2026-06-02T11:00:00Z",
                record_last_updated="2026-06-02T11:00:00Z",
            )
            canonical_store.record_source_claim(
                conn,
                provenance_event_ref=open_question_gather.event_key,
                source_claim_key_v1=f"claim:{subject_id}:open-question",
                claim_text="Which overlooked local archives might add missing detail?",
                claim_type="open_question",
                workspace_id=subject_id,
                review_state="proposed",
                created_at="2026-06-02T12:00:00Z",
                record_last_updated="2026-06-02T12:00:00Z",
            )

            high_exec = canonical_store.record_provenance_event(
                conn,
                object_namespace="execution_artifacts",
                object_id="exec-high",
                event_type="execution_artifact_ingest",
                tool_name="pytest.candidate_feedback_selection",
                run_id="exec-high",
                event_timestamp="2026-06-02T13:00:00Z",
                provenance_event_key_v1=f"prov:feedback:{subject_id}:exec-high",
            )
            high_capture = canonical_store.record_capture_event(
                conn,
                provenance_event_ref=high_exec.event_key,
                work_id=high_work.row_id,
                source_locus_ref="locus:high",
                original_locator="https://example.test/high",
                captured_at="2026-06-02T13:00:00Z",
                capture_method="fixture_capture",
                content_hash="a" * 64,
                byte_count=256,
                mime_type="text/plain",
                workspace_id=subject_id,
                record_last_updated="2026-06-02T13:00:00Z",
            )
            high_extraction = canonical_store.record_extraction_record(
                conn,
                provenance_event_ref=high_exec.event_key,
                capture_event_id=high_capture.row_id,
                extractor_name="pytest",
                extractor_version="1.0",
                extraction_method="fixture_extract",
                extraction_status="success",
                summary_short="Useful high-yield extraction.",
                input_hash="b" * 64,
                output_hash="c" * 64,
                byte_count_in=256,
                byte_count_out=128,
                encoding_handling="utf8",
                truncation_status="not_truncated",
                workspace_id=subject_id,
                created_at="2026-06-02T13:00:00Z",
                record_last_updated="2026-06-02T13:00:00Z",
            )
            canonical_store.record_extraction_detected_entity(
                conn,
                provenance_event_ref=high_exec.event_key,
                extraction_id=high_extraction.row_id,
                capture_event_id=high_capture.row_id,
                entity_label="High Yield Person",
                normalized_label="high yield person",
                entity_type="person",
                review_state="proposed",
                confidence_score=0.83,
                record_last_updated="2026-06-02T13:00:00Z",
            )

            for index in range(2):
                low_exec = canonical_store.record_provenance_event(
                    conn,
                    object_namespace="execution_artifacts",
                    object_id=f"exec-low-{index}",
                    event_type="execution_artifact_ingest",
                    tool_name="pytest.candidate_feedback_selection",
                    run_id=f"exec-low-{index}",
                    event_timestamp=f"2026-06-02T14:0{index}:00Z",
                    provenance_event_key_v1=f"prov:feedback:{subject_id}:exec-low-{index}",
                )
                low_capture = canonical_store.record_capture_event(
                    conn,
                    provenance_event_ref=low_exec.event_key,
                    source_locus_ref="locus:low",
                    original_locator="https://example.test/low",
                    captured_at=f"2026-06-02T14:0{index}:00Z",
                    capture_method="fixture_capture",
                    content_hash=("d" if index == 0 else "e") * 64,
                    byte_count=128,
                    mime_type="text/plain",
                    workspace_id=subject_id,
                    record_last_updated=f"2026-06-02T14:0{index}:00Z",
                )
                canonical_store.record_extraction_record(
                    conn,
                    provenance_event_ref=low_exec.event_key,
                    capture_event_id=low_capture.row_id,
                    extractor_name="pytest",
                    extractor_version="1.0",
                    extraction_method="fixture_extract",
                    extraction_status="failed",
                    summary_short="Low yield extraction failed.",
                    input_hash=("f" if index == 0 else "g") * 64,
                    output_hash=("h" if index == 0 else "i") * 64,
                    byte_count_in=128,
                    byte_count_out=0,
                    encoding_handling="utf8",
                    bad_utf8_handling="none",
                    truncation_status="not_truncated",
                    workspace_id=subject_id,
                    created_at=f"2026-06-02T14:0{index}:00Z",
                    record_last_updated=f"2026-06-02T14:0{index}:00Z",
                )
        return {
            "high_run_id": "cycle-one-sources-high",
            "low_run_id": "cycle-one-sources-low",
        }
    finally:
        conn.close()


def validate_plan(path: Path) -> dict[str, object]:
    report, exit_code = validator.validate_candidate_feedback_plan(path)
    assert exit_code == validator.EXIT_PASS, report
    return json.loads(path.read_text(encoding="utf-8"))


def build_plan(
    tmp_path: Path,
    db_path: Path,
    manifest_path: Path,
    *,
    extra_args: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, dict[str, object]]:
    output_path = planner_output_path(tmp_path)
    result = run_planner(
        [
            "--db",
            str(db_path),
            "--subject",
            str(manifest_path),
            "--workspace",
            str(manifest_path.parent.parent),
            "--output-json",
            str(output_path),
            "--generated-at",
            FIXED_CREATED_AT,
            *(extra_args or []),
        ]
    )
    payload = validate_plan(output_path)
    return result, output_path, payload


def test_candidate_feedback_planner_sparse_state_uses_bootstrap_selection(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    db_path = bootstrap_db(tmp_path)
    manifest_path = write_manifest(workspace_root, subject_id="sparse_subject")

    result, output_path, payload = build_plan(tmp_path, db_path, manifest_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert output_path.is_file()
    assert payload["schema_version"] == "candidate-feedback-plan.v1"
    assert payload["counts"]["gather_runs_considered"] == 0
    assert payload["next_action"]["action_kind"] == "facet_bootstrap"
    assert payload["next_action"]["selected_facet"] == "sources"
    assert payload["next_action"]["use_prior_state"] is False
    assert payload["next_action"]["previous_run_ids_considered"] == []
    assert "bootstrap_no_prior_productivity" in payload["next_action"]["reason_codes"]
    explanation = payload["selection_explanation"]
    assert explanation["schema_version"] == "selection-explanation.v1"
    assert explanation["selection_kind"] == "feedback_next_action"
    assert explanation["selected_candidate"]["candidate_id"] in {
        candidate["candidate_id"] for candidate in explanation["considered_candidates"]
    }
    assert explanation["selected_candidate"]["metadata"]["facet"] == "sources"
    assert explanation["policy"]["policy_id"] == payload["scoring_policy"]["policy_id"]


def test_candidate_feedback_planner_ranks_productive_locus_above_low_yield(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "feedback_subject"
    db_path = bootstrap_db(tmp_path)
    manifest_path = write_manifest(workspace_root, subject_id=subject_id)
    seed_feedback_state(db_path, subject_id=subject_id)

    result, _output_path, payload = build_plan(tmp_path, db_path, manifest_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert payload["next_action"]["action_kind"] == "facet_lead"
    assert payload["next_action"]["selected_facet"] == "sources"
    assert payload["next_action"]["selected_object_ref"] == "source_access:1"
    lead_scores = payload["lead_scores"]
    assert lead_scores[0]["object_ref"] == "source_access:1"
    assert lead_scores[1]["object_ref"] != "source_access:1"
    assert lead_scores[0]["score"] > lead_scores[1]["score"]
    explanation = payload["selection_explanation"]
    selected = explanation["selected_candidate"]
    assert selected["metadata"]["object_ref"] == payload["next_action"]["selected_object_ref"]
    assert selected["selected"] is True
    assert any(
        candidate["candidate_id"] == selected["candidate_id"]
        for candidate in explanation["considered_candidates"]
    )


def test_candidate_feedback_planner_deprioritizes_low_yield_locus_but_keeps_it_deferred(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "feedback_subject"
    db_path = bootstrap_db(tmp_path)
    manifest_path = write_manifest(workspace_root, subject_id=subject_id)
    seed_feedback_state(db_path, subject_id=subject_id)

    _result, _output_path, payload = build_plan(tmp_path, db_path, manifest_path)

    low_lead = next(item for item in payload["lead_scores"] if item["object_ref"] == "source_access:2")
    assert "repeated_low_yield" in low_lead["reason_codes"]
    assert low_lead["score"] < 0
    assert {
        "candidate_id": "source_access:2",
        "candidate_kind": "lead",
        "score": low_lead["score"],
        "reason": "repeated_low_yield",
    } in payload["deferred"]
    explanation = payload["selection_explanation"]
    assert any(
        candidate["candidate_id"] == "source_access:2"
        and candidate["reason"] == "repeated_low_yield"
        for candidate in explanation["excluded_candidates"]
    )


def test_unknown_extraction_status_is_neutral_for_source_access_leads(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "feedback_subject"
    db_path = bootstrap_db(tmp_path)
    manifest_path = write_manifest(workspace_root, subject_id=subject_id)
    seed_feedback_state(db_path, subject_id=subject_id)

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            conn.execute(
                """
                UPDATE extraction_record
                SET extraction_status=?
                WHERE summary_short = ?
                """,
                ("pending", "Useful high-yield extraction."),
            )
    finally:
        conn.close()

    _result, _output_path, payload = build_plan(tmp_path, db_path, manifest_path)

    source_access_lead = next(item for item in payload["lead_scores"] if item["object_ref"] == "source_access:1")
    assert source_access_lead["signals"]["successful_extractions"] == 0
    assert source_access_lead["signals"]["failed_extractions"] == 0
    assert any(
        "unknown extraction_status treated as neutral for source-access lead scoring" in warning
        for warning in payload["warnings"]
    )


def test_unknown_extraction_status_is_neutral_for_entity_leads(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "feedback_subject"
    db_path = bootstrap_db(tmp_path)
    manifest_path = write_manifest(workspace_root, subject_id=subject_id)
    seed_feedback_state(db_path, subject_id=subject_id)

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            conn.execute(
                """
                UPDATE extraction_record
                SET extraction_status=?
                WHERE summary_short = ?
                """,
                ("partial", "Useful high-yield extraction."),
            )
    finally:
        conn.close()

    _result, _output_path, payload = build_plan(tmp_path, db_path, manifest_path)

    detected_entity_lead = next(item for item in payload["lead_scores"] if item["lead_kind"] == "detected_entity")
    assert detected_entity_lead["signals"]["successful_extractions"] == 0
    assert detected_entity_lead["signals"]["failed_extractions"] == 0
    assert any(
        "unknown extraction_status treated as neutral for detected entity lead scoring" in warning
        for warning in payload["warnings"]
    )


def test_candidate_feedback_includes_entity_leads_without_extraction_records(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "feedback_subject"
    db_path = bootstrap_db(tmp_path)
    manifest_path = write_manifest(workspace_root, subject_id=subject_id)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            prov = canonical_store.record_provenance_event(
                conn,
                object_namespace="candidate-feedback-tests",
                object_id="entity-without-capture",
                event_type="feedback_test",
                actor_type="pytest",
                actor_id="pytest.candidate_feedback_selection",
                tool_name="tests.test_candidate_feedback_selection",
                run_id="entity-without-capture",
                event_timestamp=FIXED_CREATED_AT,
                note_text="candidate visibility fixture",
                provenance_event_key_v1="prov:feedback:test-entity",
            )
            entity = canonical_store.record_extraction_detected_entity(
                conn,
                provenance_event_ref=prov.event_key,
                entity_label="Unbacked Entity",
                normalized_label="unbacked entity",
                entity_type="person",
                review_state="proposed",
                confidence_score=0.81,
                workspace_id=subject_id,
                record_last_updated=FIXED_CREATED_AT,
            )
    finally:
        conn.close()

    _result, _output_path, payload = build_plan(tmp_path, db_path, manifest_path)

    assert payload["next_action"]["action_kind"] == "facet_lead"
    assert payload["next_action"]["selected_facet"] == "people"
    assert payload["next_action"]["selected_object_ref"] == f"detected_entity:{entity.row_id}"
    assert any(
        item["object_ref"] == f"detected_entity:{entity.row_id}"
        for item in payload["lead_scores"]
    )


def test_candidate_feedback_planner_can_record_selection_explanation_to_ledger(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "feedback_subject"
    db_path = bootstrap_db(tmp_path)
    manifest_path = write_manifest(workspace_root, subject_id=subject_id)
    seed_feedback_state(db_path, subject_id=subject_id)

    result, _output_path, payload = build_plan(
        tmp_path,
        db_path,
        manifest_path,
        extra_args=["--record-selection-ledger"],
    )

    assert result.returncode == 0, result.stdout + result.stderr
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        considered_count = conn.execute(
            "SELECT COUNT(*) AS count FROM cycle_candidate_considered"
        ).fetchone()["count"]
        excluded_count = conn.execute(
            "SELECT COUNT(*) AS count FROM cycle_candidate_excluded"
        ).fetchone()["count"]
        selected_rows = conn.execute(
            """
            SELECT candidate_ref_id
            FROM cycle_candidate_considered
            WHERE selected=1
            ORDER BY candidate_ref_id
            """
        ).fetchall()
    finally:
        conn.close()
    assert considered_count >= len(payload["selection_explanation"]["considered_candidates"])
    assert excluded_count >= len(payload["selection_explanation"]["excluded_candidates"])
    assert [row["candidate_ref_id"] for row in selected_rows] == [
        payload["selection_explanation"]["selected_candidate"]["candidate_id"]
    ]


def test_candidate_feedback_planner_open_lead_yield_increases_sources_score(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "feedback_subject"
    db_path = bootstrap_db(tmp_path)
    manifest_path = write_manifest(workspace_root, subject_id=subject_id)
    seed_feedback_state(db_path, subject_id=subject_id)

    _result, _output_path, payload = build_plan(tmp_path, db_path, manifest_path)

    sources = next(item for item in payload["facet_scores"] if item["facet"] == "sources")
    open_questions = next(item for item in payload["facet_scores"] if item["facet"] == "open_questions")
    assert sources["signals"]["open_leads"] >= 2
    assert "open_lead_yield" in sources["reason_codes"]
    assert sources["score"] > open_questions["score"]


def test_gather_consumes_feedback_plan_and_records_metadata(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "feedback_subject"
    db_path = bootstrap_db(tmp_path)
    manifest_path = write_manifest(workspace_root, subject_id=subject_id)
    seed_feedback_state(db_path, subject_id=subject_id)
    _planner_result, output_path, payload = build_plan(tmp_path, db_path, manifest_path)

    run_id = "feedback-guided-dry-run"
    result = run_driver(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--feedback-plan",
            str(output_path),
            "--db",
            str(db_path),
            "--mode",
            "dry-run",
            "--run-id",
            run_id,
            "--created-at",
            FIXED_CREATED_AT,
        ]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    batch_payload = json.loads(batch_path_for(workspace_root, run_id).read_text(encoding="utf-8"))
    prompt_text = prompt_path_for(workspace_root, run_id).read_text(encoding="utf-8")

    assert batch_payload["facet"]["name"] == payload["next_action"]["selected_facet"]
    assert batch_payload["cycle_depth"] == payload["next_action"]["cycle_depth"]
    assert batch_payload["feedback_plan"]["plan_hash"]
    assert batch_payload["feedback_plan"]["next_action_id"] == payload["next_action"]["action_id"]
    assert batch_payload["provenance"]["feedback_plan_hash"] == batch_payload["feedback_plan"]["plan_hash"]
    assert batch_payload["provenance"]["next_action_id"] == payload["next_action"]["action_id"]
    assert "NEXT ACTION SELECTION" in prompt_text
    assert payload["next_action"]["action_id"] in prompt_text
    assert "not accepted fact" in prompt_text
    assert "remain leads" in prompt_text


def test_feedback_guided_gather_rejects_subject_mismatch(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "feedback_subject"
    db_path = bootstrap_db(tmp_path)
    source_manifest = write_manifest(workspace_root, subject_id=subject_id)
    seed_feedback_state(db_path, subject_id=subject_id)
    _planner_result, output_path, _payload = build_plan(tmp_path, db_path, source_manifest)

    other_workspace = tmp_path / "other-workspace"
    other_workspace.mkdir()
    other_manifest = write_manifest(other_workspace, subject_id="other_subject")

    result = run_driver(
        [
            "--subject",
            str(other_manifest),
            "--workspace",
            str(other_workspace),
            "--feedback-plan",
            str(output_path),
            "--db",
            str(db_path),
            "--mode",
            "dry-run",
            "--run-id",
            "mismatch",
            "--created-at",
            FIXED_CREATED_AT,
        ]
    )

    assert result.returncode == 1
    assert "feedback plan subject_id does not match the resolved gather subject" in result.stderr


def test_feedback_guided_gather_rejects_disabled_facet_plan(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "feedback_subject"
    db_path = bootstrap_db(tmp_path)
    manifest_path = write_manifest(workspace_root, subject_id=subject_id)
    seed_feedback_state(db_path, subject_id=subject_id)
    _planner_result, output_path, payload = build_plan(tmp_path, db_path, manifest_path)

    payload["next_action"]["selected_facet"] = "taxonomy"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    result = run_driver(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--feedback-plan",
            str(output_path),
            "--db",
            str(db_path),
            "--mode",
            "dry-run",
        ]
    )

    assert result.returncode == 1
    assert "feedback plan failed validation" in result.stderr


def test_feedback_guided_gather_rejects_malformed_plan_before_use(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, subject_id="feedback_subject")
    db_path = bootstrap_db(tmp_path)
    plan_path = planner_output_path(tmp_path, "invalid-plan.json")
    plan_path.write_text('{"schema_version":"candidate-feedback-plan.v1"}\n', encoding="utf-8")

    result = run_driver(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--feedback-plan",
            str(plan_path),
            "--db",
            str(db_path),
            "--mode",
            "dry-run",
        ]
    )

    assert result.returncode == 1
    assert "feedback plan failed validation" in result.stderr


def test_candidate_feedback_planner_is_deterministic_for_same_store_and_options(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "feedback_subject"
    db_path = bootstrap_db(tmp_path)
    manifest_path = write_manifest(workspace_root, subject_id=subject_id)
    seed_feedback_state(db_path, subject_id=subject_id)

    first_result, first_path, first_payload = build_plan(tmp_path, db_path, manifest_path)
    second_result = run_planner(
        [
            "--db",
            str(db_path),
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--output-json",
            str(tmp_path / "candidate-feedback-plan-second.json"),
            "--generated-at",
            FIXED_CREATED_AT,
        ]
    )
    second_path = tmp_path / "candidate-feedback-plan-second.json"
    second_payload = validate_plan(second_path)

    assert first_result.returncode == 0, first_result.stdout + first_result.stderr
    assert second_result.returncode == 0, second_result.stdout + second_result.stderr
    assert first_payload == second_payload
    assert first_path.read_text(encoding="utf-8") == second_path.read_text(encoding="utf-8")


def test_feedback_plan_metadata_survives_candidate_batch_ingest_provenance(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "feedback_subject"
    db_path = bootstrap_db(tmp_path)
    manifest_path = write_manifest(workspace_root, subject_id=subject_id)
    seed_feedback_state(db_path, subject_id=subject_id)
    _planner_result, output_path, payload = build_plan(tmp_path, db_path, manifest_path)

    run_id = "feedback-guided-ingest"
    result = run_driver(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--feedback-plan",
            str(output_path),
            "--db",
            str(db_path),
            "--mode",
            "dry-run",
            "--run-id",
            run_id,
            "--created-at",
            FIXED_CREATED_AT,
        ]
    )
    assert result.returncode == 0, result.stdout + result.stderr

    batch_path = batch_path_for(workspace_root, run_id)
    batch, batch_hash = canonical_ingest.load_validated_candidate_batch(batch_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            report = canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=batch_path,
                batch_hash=batch_hash,
                db_path=db_path,
            )
            provenance = report["provenance_event"]
            note_text = conn.execute(
                "SELECT note_text FROM provenance_event WHERE provenance_event_key_v1=?",
                (provenance["event_key"],),
            ).fetchone()["note_text"]
    finally:
        conn.close()

    note_payload = json.loads(note_text)
    assert note_payload["feedback_plan_hash"] == batch["feedback_plan"]["plan_hash"]
    assert note_payload["next_action_id"] == payload["next_action"]["action_id"]
    assert note_payload["selected_object_ref"] == payload["next_action"]["selected_object_ref"]
