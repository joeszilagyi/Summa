from __future__ import annotations

import json
from pathlib import Path

from tools.source_db_tools import canonical_store


FIXED_TIMESTAMP = "2026-06-03T09:00:00Z"
PRIVATE_SENTINEL = "PRIVATE_SENTINEL_DO_NOT_PUBLISH"
UNREVIEWED_SENTINEL = "UNREVIEWED_SENTINEL_DO_NOT_PUBLISH"


def create_sparse_canonical_store(tmp_path: Path, *, name: str = "canonical.sqlite") -> Path:
    db_path = tmp_path / name
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_TIMESTAMP,
        applied_by="pytest.publication_fixture",
    )
    return db_path


def create_populated_canonical_store(tmp_path: Path, *, name: str = "canonical.sqlite") -> Path:
    db_path = create_sparse_canonical_store(tmp_path, name=name)
    conn = canonical_store.connect_canonical_store(db_path)
    try:
        conn.execute(
            """
            INSERT INTO provenance_event (
              provenance_event_id,
              provenance_event_key_v1,
              object_namespace,
              object_id,
              event_type,
              actor_type,
              actor_id,
              actor_label,
              tool_name,
              tool_version,
              model_name,
              prompt_id,
              run_id,
              source_object_namespace,
              source_object_id,
              event_timestamp,
              confidence_score,
              note_text,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "prov.alpha.1",
                "work",
                "1",
                "capture",
                "tool",
                "fixture-builder",
                "Fixture Builder",
                "pytest",
                "1.0",
                None,
                None,
                "run-publication-fixture",
                None,
                None,
                "2026-06-01T00:30:00Z",
                0.9,
                f"{PRIVATE_SENTINEL} provenance note",
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO authority_record (
              authority_record_id,
              authority_key_v1,
              authority_type,
              preferred_label,
              label_norm,
              sort_label,
              source_namespace,
              source_id,
              reconciliation_status,
              review_state,
              confidence_score,
              authority_level,
              workspace_id,
              public_blocker,
              publication_state,
              provenance_event_ref,
              merged_into_authority_record_id,
              created_at,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "authority.alpha",
                "person",
                "Alpha Example",
                "alpha example",
                "Alpha Example",
                "fixture",
                "authority-alpha",
                "accepted",
                "reviewed",
                0.94,
                "curated",
                "demo_workspace",
                None,
                "public_release_allowed",
                "prov.alpha.1",
                None,
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO work (
              work_id,
              work_key_v1,
              work_type,
              title,
              rights_posture,
              refetchability_status,
              review_state,
              publication_state,
              confidence_score,
              raw_cite_text,
              workspace_id,
              authority_level,
              public_blocker,
              accepted_for_citation,
              provenance_event_ref,
              first_seen_at,
              last_seen_at,
              created_at,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "work.alpha",
                "article",
                "Alpha Chronicle",
                "redistributable",
                "local_only",
                "reviewed",
                "public_release_allowed",
                0.92,
                None,
                "demo_workspace",
                "curated",
                None,
                1,
                "prov.alpha.1",
                "2026-06-01T00:00:00Z",
                "2026-06-02T00:00:00Z",
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO capture_event (
              capture_event_id,
              work_id,
              source_locus_ref,
              original_locator,
              captured_at,
              capture_method,
              content_hash,
              byte_count,
              mime_type,
              byte_retention_status,
              full_text_retention_status,
              refetchability_status,
              payload_storage_policy_class,
              quality_warnings_json,
              transient_payload_note,
              review_state,
              workspace_id,
              public_blocker,
              provenance_event_ref,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1,
                "source:locus:alpha",
                f"/home/joe/private/{PRIVATE_SENTINEL}/alpha.txt",
                "2026-06-01T01:00:00Z",
                "local_copy",
                "sha256:" + ("1" * 64),
                1234,
                "text/plain",
                "transient",
                "redacted",
                "local_only",
                "transient_workspace",
                "[]",
                "transient fixture payload",
                "reviewed",
                "demo_workspace",
                None,
                "prov.alpha.1",
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO extraction_record (
              extraction_id,
              capture_event_id,
              extractor_name,
              extractor_version,
              extraction_method,
              summary_short,
              input_hash,
              output_hash,
              byte_count_in,
              byte_count_out,
              encoding_handling,
              extraction_status,
              bad_utf8_handling,
              truncation_status,
              hostile_replay_flags_json,
              review_state,
              workspace_id,
              public_blocker,
              provenance_event_ref,
              created_at,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1,
                "fixture-extractor",
                "1.0",
                "summary",
                "Reviewed source excerpt for Alpha Chronicle.",
                "sha256:" + ("1" * 64),
                "sha256:" + ("2" * 64),
                1234,
                180,
                "utf-8",
                "completed",
                "none",
                "not_truncated",
                "[]",
                "reviewed",
                "demo_workspace",
                None,
                "prov.alpha.1",
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO extraction_detected_entity (
              detected_entity_id,
              extraction_id,
              capture_event_id,
              entity_label,
              normalized_label,
              entity_type,
              source_span_start,
              source_span_end,
              authority_record_id,
              review_state,
              confidence_score,
              provenance_event_ref,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1,
                1,
                "Alpha Example Mention",
                "alpha example",
                "person",
                0,
                12,
                1,
                "reviewed",
                0.81,
                "prov.alpha.1",
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO work_subject (
              work_subject_id,
              work_id,
              authority_record_id,
              subject_object_ref,
              subject_role,
              source_note,
              review_state,
              confidence_score,
              provenance_event_ref,
              created_at,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1,
                1,
                "facet:alpha",
                "primary_subject",
                "Curated topic mapping",
                "reviewed",
                0.9,
                "prov.alpha.1",
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO source_relationship (
              source_relationship_id,
              from_object_ref,
              to_object_ref,
              predicate,
              target_label,
              evidence_note,
              review_state,
              publication_state,
              authority_level,
              public_blocker,
              workspace_id,
              confidence_score,
              provenance_event_ref,
              evidence_locator_ref,
              created_at,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "authority:1",
                "work:1",
                "documented_in",
                "Alpha Chronicle",
                f"{PRIVATE_SENTINEL} evidence note",
                "reviewed",
                "public_release_allowed",
                "curated",
                None,
                "demo_workspace",
                0.86,
                "prov.alpha.1",
                None,
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO source_claim (
              source_claim_id,
              source_claim_key_v1,
              about_object_ref,
              claim_text,
              public_summary,
              claim_type,
              review_state,
              publication_state,
              authority_level,
              public_blocker,
              workspace_id,
              confidence_score,
              provenance_event_ref,
              evidence_locator_ref,
              capture_event_id,
              extraction_id,
              created_at,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "claim.alpha.1",
                "authority:1",
                f"{PRIVATE_SENTINEL} raw claim text",
                "Alpha Chronicle documents Alpha Example.",
                "summary",
                "reviewed",
                "public_release_allowed",
                "curated",
                None,
                "demo_workspace",
                0.88,
                "prov.alpha.1",
                None,
                1,
                1,
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO source_claim (
              source_claim_id,
              source_claim_key_v1,
              about_object_ref,
              claim_text,
              public_summary,
              claim_type,
              review_state,
              publication_state,
              authority_level,
              public_blocker,
              workspace_id,
              confidence_score,
              provenance_event_ref,
              evidence_locator_ref,
              capture_event_id,
              extraction_id,
              created_at,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                2,
                "claim.alpha.2",
                "authority:1",
                f"{UNREVIEWED_SENTINEL} raw claim text",
                "Unreviewed summary should not publish.",
                "summary",
                "needs_review",
                "draft",
                "proposed",
                "needs_review",
                "demo_workspace",
                0.2,
                "prov.alpha.1",
                None,
                1,
                1,
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO topic_extension (
              topic_extension_id,
              topic_id,
              extension_type,
              summary_short,
              note_text,
              review_state,
              publication_state,
              authority_level,
              public_blocker,
              workspace_id,
              confidence_score,
              provenance_event_ref,
              created_at,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "alpha_topic",
                "collection",
                "Alpha collection entry",
                f"{PRIVATE_SENTINEL} topic note",
                "reviewed",
                "public_release_allowed",
                "curated",
                None,
                "demo_workspace",
                0.9,
                "prov.alpha.1",
                FIXED_TIMESTAMP,
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO source_access (
              source_access_id,
              work_id,
              source_locus_id,
              source_lead_id,
              original_locator,
              canonical_url,
              access_class,
              refetchability_status,
              rights_posture,
              citation_hint,
              review_state,
              publication_state,
              authority_level,
              public_blocker,
              workspace_id,
              first_seen_at,
              last_seen_at,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1,
                "source:locus:alpha",
                "lead.alpha",
                f"/home/joe/private/{PRIVATE_SENTINEL}/alpha-source.html",
                "https://example.invalid/alpha-chronicle",
                "web_page",
                "local_only",
                "redistributable",
                "Alpha Chronicle Archive",
                "reviewed",
                "public_release_allowed",
                "curated",
                None,
                "demo_workspace",
                "2026-06-01T00:45:00Z",
                "2026-06-02T00:45:00Z",
                FIXED_TIMESTAMP,
            ),
        )
        conn.execute(
            """
            INSERT INTO review_state_history (
              review_state_history_key_v1,
              target_namespace,
              target_id,
              previous_state,
              new_state,
              changed_by,
              changed_at,
              reason,
              note,
              source_namespace,
              source_id,
              source_tool,
              source_run_id,
              record_last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "review.alpha.1",
                "source_claim",
                "1",
                "needs_review",
                "reviewed",
                "fixture-reviewer",
                FIXED_TIMESTAMP,
                "accepted for public summary",
                None,
                "fixture",
                "review-1",
                "pytest",
                "run-publication-fixture",
                FIXED_TIMESTAMP,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path

