from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tools.source_db_tools import canonical_ingest, canonical_store

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER_PATH = REPO_ROOT / "tools" / "scripts" / "run_topic_gather.py"
FIXTURE_BATCH = REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "gather-candidate-batch.json"
FIXTURE_PROMPT = REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "rendered-prompt.txt"
FIXED_CREATED_AT = "2026-06-03T12:34:56Z"


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
        applied_by="pytest.gather_iteration",
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
                "scope_statement": "Synthetic gather iteration fixture manifest.",
                "languages": ["en"],
                "aliases": [],
                "disambiguation_terms": [],
                "excluded_senses": [],
                "enabled_facets": list(pack["enabled_facets"]),
                "query_families": [pack["query_families"][0]],
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


def batch_path_for(workspace_root: Path, run_id: str) -> Path:
    return workspace_root / "runs" / "gather" / run_id / "gather-candidate-batch.json"


def prompt_path_for(workspace_root: Path, run_id: str) -> Path:
    return workspace_root / "runs" / "gather" / run_id / "rendered-prompt.txt"


def write_seed_batch(tmp_path: Path, *, subject_id: str, run_id: str = "cycle-one") -> Path:
    payload = json.loads(FIXTURE_BATCH.read_text(encoding="utf-8"))
    payload["run_id"] = run_id
    payload["created_at"] = FIXED_CREATED_AT
    payload["subject"]["subject_id"] = subject_id
    seed_path = tmp_path / f"{run_id}-gather-candidate-batch.json"
    seed_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    prompt_path = seed_path.with_name("rendered-prompt.txt")
    prompt_path.write_text(FIXTURE_PROMPT.read_text(encoding="utf-8"), encoding="utf-8")
    return seed_path


def seed_cycle_one_state(db_path: Path, *, subject_id: str, extra_accepted_works: int = 0) -> None:
    seed_batch_path = write_seed_batch(db_path.parent, subject_id=subject_id)
    batch, batch_hash = canonical_ingest.load_validated_candidate_batch(seed_batch_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=seed_batch_path,
                batch_hash=batch_hash,
                db_path=db_path,
            )
            provenance = canonical_store.record_provenance_event(
                conn,
                object_namespace="fixture_seed",
                object_id=subject_id,
                event_type="fixture_seed",
                tool_name="pytest.gather_iteration",
                run_id="fixture-seed",
                event_timestamp=FIXED_CREATED_AT,
                provenance_event_key_v1=f"prov:gather-iteration:{subject_id}",
            )
            accepted_work = canonical_store.upsert_work(
                conn,
                work_key_v1=f"work:{subject_id}:accepted-alpha",
                provenance_event_ref=provenance.event_key,
                work_type="article",
                title="Accepted Alpha Work",
                review_state="accepted",
                confidence_score=0.95,
                workspace_id=subject_id,
                first_seen_at=FIXED_CREATED_AT,
                last_seen_at=FIXED_CREATED_AT,
                created_at=FIXED_CREATED_AT,
                record_last_updated=FIXED_CREATED_AT,
            )
            capture = canonical_store.record_capture_event(
                conn,
                provenance_event_ref=provenance.event_key,
                work_id=accepted_work.row_id,
                original_locator="https://example.test/accepted-alpha",
                captured_at=FIXED_CREATED_AT,
                capture_method="fixture_capture",
                content_hash="a" * 64,
                byte_count=128,
                mime_type="text/plain",
                workspace_id=subject_id,
                record_last_updated=FIXED_CREATED_AT,
            )
            extraction = canonical_store.record_extraction_record(
                conn,
                provenance_event_ref=provenance.event_key,
                capture_event_id=capture.row_id,
                extractor_name="pytest",
                extractor_version="1.0",
                extraction_method="fixture_extract",
                extraction_status="completed",
                summary_short="Accepted Alpha source summary.",
                input_hash="a" * 64,
                output_hash="b" * 64,
                byte_count_in=128,
                byte_count_out=64,
                encoding_handling="utf8",
                truncation_status="not_truncated",
                workspace_id=subject_id,
                created_at=FIXED_CREATED_AT,
                record_last_updated=FIXED_CREATED_AT,
            )
            canonical_store.record_extraction_detected_entity(
                conn,
                provenance_event_ref=provenance.event_key,
                extraction_id=extraction.row_id,
                capture_event_id=capture.row_id,
                entity_label="Accepted Alpha Entity",
                normalized_label="accepted alpha entity",
                entity_type="person",
                review_state="accepted",
                confidence_score=0.91,
                record_last_updated=FIXED_CREATED_AT,
            )
            canonical_store.record_source_claim(
                conn,
                provenance_event_ref=provenance.event_key,
                source_claim_key_v1=f"claim:{subject_id}:needs-review",
                about_object_ref=f"work:{accepted_work.row_id}",
                claim_text="This remains a needs-review lead rather than an accepted fact.",
                claim_type="lead_claim",
                review_state="needs_review",
                workspace_id=subject_id,
                created_at=FIXED_CREATED_AT,
                record_last_updated=FIXED_CREATED_AT,
            )
            for index in range(extra_accepted_works):
                canonical_store.upsert_work(
                    conn,
                    work_key_v1=f"work:{subject_id}:accepted-extra-{index}",
                    provenance_event_ref=provenance.event_key,
                    work_type="article",
                    title=f"Accepted Extra Work {index + 1}",
                    review_state="accepted",
                    confidence_score=0.90 - (index * 0.01),
                    workspace_id=subject_id,
                    first_seen_at=FIXED_CREATED_AT,
                    last_seen_at=FIXED_CREATED_AT,
                    created_at=FIXED_CREATED_AT,
                    record_last_updated=FIXED_CREATED_AT,
                )
    finally:
        conn.close()


def test_gather_iteration_empty_prior_state_dry_run_succeeds(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    db_path = bootstrap_db(tmp_path)
    manifest_path = write_manifest(workspace_root, subject_id="empty_subject")
    run_id = "empty-prior-state"

    proc = run_driver(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--facet",
            "sources",
            "--mode",
            "dry-run",
            "--db",
            str(db_path),
            "--use-prior-state",
            "--cycle-depth",
            "1",
            "--run-id",
            run_id,
            "--created-at",
            FIXED_CREATED_AT,
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(batch_path_for(workspace_root, run_id).read_text(encoding="utf-8"))
    prompt_text = prompt_path_for(workspace_root, run_id).read_text(encoding="utf-8")

    assert payload["iteration_mode"] == "prior_state"
    assert payload["cycle_depth"] == 1
    assert payload["prior_state"]["record_counts"]["works"] == {"total": 0, "selected": 0, "rendered": 0}
    assert payload["prior_state"]["record_counts"]["previous_runs"] == {"total": 0, "selected": 0, "rendered": 0}
    assert payload["prior_state"]["previous_run_ids"] == []
    assert payload["prior_state"]["context_hash"] == payload["provenance"]["prior_state_hash"]
    assert "PRIOR CANONICAL STATE CONTEXT" in prompt_text
    assert "No prior canonical records were selected for this subject." in prompt_text


def test_gather_iteration_cycle_two_sees_cycle_one_state(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "fixture_subject"
    db_path = bootstrap_db(tmp_path)
    seed_cycle_one_state(db_path, subject_id=subject_id)
    manifest_path = write_manifest(workspace_root, subject_id=subject_id)
    run_id = "cycle-two"

    proc = run_driver(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--facet",
            "sources",
            "--mode",
            "dry-run",
            "--db",
            str(db_path),
            "--use-prior-state",
            "--cycle-depth",
            "2",
            "--run-id",
            run_id,
            "--created-at",
            FIXED_CREATED_AT,
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(batch_path_for(workspace_root, run_id).read_text(encoding="utf-8"))
    prompt_text = prompt_path_for(workspace_root, run_id).read_text(encoding="utf-8")

    assert payload["cycle_depth"] == 2
    assert payload["iteration_mode"] == "prior_state"
    assert "cycle-one" in payload["previous_run_ids"]
    assert payload["prior_state"]["context_hash"]
    assert payload["prior_state"]["record_counts"]["works"]["total"] >= 1
    assert payload["prior_state"]["record_counts"]["entities"]["total"] >= 1
    assert payload["prior_state"]["record_counts"]["source_claims"]["total"] >= 1
    assert "PRIOR CANONICAL STATE CONTEXT" in prompt_text
    assert "Accepted Alpha Work" in prompt_text
    assert "Accepted Alpha Entity" in prompt_text
    assert "needs_review lead" in prompt_text
    assert "verified truth" in prompt_text
    assert "This remains a needs-review lead rather than an accepted fact." in prompt_text


def test_gather_iteration_applies_bounded_prior_state_limit_deterministically(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "fixture_subject"
    db_path = bootstrap_db(tmp_path)
    seed_cycle_one_state(db_path, subject_id=subject_id, extra_accepted_works=4)
    manifest_path = write_manifest(workspace_root, subject_id=subject_id)
    run_id = "bounded-prior-state"

    proc = run_driver(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--facet",
            "sources",
            "--mode",
            "dry-run",
            "--db",
            str(db_path),
            "--use-prior-state",
            "--cycle-depth",
            "2",
            "--prior-state-limit",
            "2",
            "--run-id",
            run_id,
            "--created-at",
            FIXED_CREATED_AT,
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(batch_path_for(workspace_root, run_id).read_text(encoding="utf-8"))
    prompt_text = prompt_path_for(workspace_root, run_id).read_text(encoding="utf-8")

    assert payload["prior_state"]["record_counts"]["works"]["total"] >= 3
    assert payload["prior_state"]["record_counts"]["works"]["selected"] == 2
    assert payload["prior_state"]["record_counts"]["works"]["rendered"] == 2
    assert "Accepted Alpha Work" in prompt_text
    assert "Accepted Extra Work 1" in prompt_text
    assert "Accepted Extra Work 3" not in prompt_text


def test_gather_iteration_one_shot_mode_remains_supported(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, subject_id="one_shot_subject")
    run_id = "one-shot"

    proc = run_driver(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--facet",
            "sources",
            "--mode",
            "dry-run",
            "--run-id",
            run_id,
            "--created-at",
            FIXED_CREATED_AT,
        ]
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(batch_path_for(workspace_root, run_id).read_text(encoding="utf-8"))
    prompt_text = prompt_path_for(workspace_root, run_id).read_text(encoding="utf-8")

    assert payload["iteration_mode"] == "one_shot"
    assert payload["cycle_depth"] == 1
    assert payload.get("prior_state") is None
    assert payload["provenance"]["prior_state_enabled"] is False
    assert "PRIOR CANONICAL STATE CONTEXT" not in prompt_text


def test_gather_iteration_missing_db_fails_clearly(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, subject_id="missing_db_subject")

    proc = run_driver(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--facet",
            "sources",
            "--mode",
            "dry-run",
            "--db",
            str(tmp_path / "missing.sqlite"),
            "--use-prior-state",
        ]
    )

    assert proc.returncode == 1
    assert "prior-state store is not usable" in proc.stderr


def test_gather_iteration_invalid_store_fails_clearly(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(workspace_root, subject_id="invalid_db_subject")
    invalid_db = tmp_path / "invalid.sqlite"
    invalid_db.write_text("not a sqlite database", encoding="utf-8")

    proc = run_driver(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--facet",
            "sources",
            "--mode",
            "dry-run",
            "--db",
            str(invalid_db),
            "--use-prior-state",
        ]
    )

    assert proc.returncode == 1
    assert "prior-state store is not usable" in proc.stderr


def test_gather_iteration_prior_state_hash_is_deterministic(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "fixture_subject"
    db_path = bootstrap_db(tmp_path)
    seed_cycle_one_state(db_path, subject_id=subject_id)
    manifest_path = write_manifest(workspace_root, subject_id=subject_id)
    run_id = "deterministic-prior-state"
    args = [
        "--subject",
        str(manifest_path),
        "--workspace",
        str(workspace_root),
        "--facet",
        "sources",
        "--mode",
        "dry-run",
        "--db",
        str(db_path),
        "--use-prior-state",
        "--cycle-depth",
        "2",
        "--run-id",
        run_id,
        "--created-at",
        FIXED_CREATED_AT,
    ]

    first = run_driver(args)
    assert first.returncode == 0, first.stdout + first.stderr
    first_payload = json.loads(batch_path_for(workspace_root, run_id).read_text(encoding="utf-8"))
    first_prompt = prompt_path_for(workspace_root, run_id).read_text(encoding="utf-8")

    second = run_driver(args)
    assert second.returncode == 0, second.stdout + second.stderr
    second_payload = json.loads(batch_path_for(workspace_root, run_id).read_text(encoding="utf-8"))
    second_prompt = prompt_path_for(workspace_root, run_id).read_text(encoding="utf-8")

    assert first_payload["prior_state"]["context_hash"] == second_payload["prior_state"]["context_hash"]
    assert first_payload["prior_state"]["context_text"] == second_payload["prior_state"]["context_text"]
    assert first_prompt == second_prompt


def test_gather_iteration_ingest_preserves_cycle_metadata_in_provenance(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    subject_id = "fixture_subject"
    db_path = bootstrap_db(tmp_path)
    seed_cycle_one_state(db_path, subject_id=subject_id)
    manifest_path = write_manifest(workspace_root, subject_id=subject_id)
    run_id = "cycle-two-ingest"

    proc = run_driver(
        [
            "--subject",
            str(manifest_path),
            "--workspace",
            str(workspace_root),
            "--facet",
            "sources",
            "--mode",
            "dry-run",
            "--db",
            str(db_path),
            "--use-prior-state",
            "--cycle-depth",
            "2",
            "--run-id",
            run_id,
            "--created-at",
            FIXED_CREATED_AT,
        ]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    batch_path = batch_path_for(workspace_root, run_id)
    payload = json.loads(batch_path.read_text(encoding="utf-8"))
    batch, batch_hash = canonical_ingest.load_validated_candidate_batch(batch_path)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        with conn:
            canonical_ingest.ingest_candidate_batch(
                conn,
                batch,
                batch_path=batch_path,
                batch_hash=batch_hash,
                db_path=db_path,
            )
        row = conn.execute(
            """
            SELECT note_text
            FROM provenance_event
            WHERE event_type='gather_candidate_batch_ingest' AND run_id=?
            ORDER BY provenance_event_id DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    finally:
        conn.close()

    note_payload = canonical_store.parse_gather_candidate_batch_ingest_note(row["note_text"])
    assert row["note_text"].startswith("gather_candidate_batch_ingest")
    assert note_payload["cycle_depth"] == 2
    assert note_payload["prior_state_hash"] == payload["prior_state"]["context_hash"]
    assert "cycle-one" in note_payload["previous_run_ids"]
