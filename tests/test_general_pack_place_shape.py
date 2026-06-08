from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from tools.common.llm_source_text_wrapper import load_template
from tools.scripts.run_topic_gather import render_prompt_text
from tools.source_db_tools import canonical_ingest, canonical_store

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER_PATH = REPO_ROOT / "tools" / "scripts" / "run_topic_gather.py"
FIXTURE_BATCH = REPO_ROOT / "tests" / "fixtures" / "canonical_ingest" / "gather-candidate-batch.json"
DOMAIN_PACKS_DOC = REPO_ROOT / "docs" / "project" / "DOMAIN_PACKS.md"
FIXED_CREATED_AT = "2026-06-03T12:34:56Z"


def bootstrap_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "canonical.sqlite"
    canonical_store.init_canonical_store(
        db_path,
        applied_at=FIXED_CREATED_AT,
        applied_by="pytest.general_pack_place_shape",
    )
    return db_path


def run_driver(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DRIVER_PATH), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def write_manifest(
    workspace_root: Path,
    *,
    subject_id: str,
    display_name: str,
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
                "display_name": display_name,
                "domain_pack": domain_pack,
                "scope_statement": f"Synthetic place-dominant fixture for {display_name}.",
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


def _render_place_shape_prompt(payload: dict[str, object]) -> str:
    fixture_prompt_text = (FIXTURE_BATCH.parent / "rendered-prompt.txt").read_text(encoding="utf-8")
    prompt_body = fixture_prompt_text.split("\n\nSubject runtime:\n", 1)[0]
    prompt_bundle = dict(payload["prompt_bundle"])
    prompt_bundle["source_text_wrapper_template_id"] = prompt_bundle["wrapper_template_id"]
    return render_prompt_text(
        prompt_body=prompt_body,
        subject=payload["subject"],
        facet=str(payload["facet"]["name"]),
        phase=str(payload["phase"]),
        bundle=prompt_bundle,
        wrapped_blocks=[],
        template=load_template(),
    )


def _place_shape_candidate_batch(
    *,
    subject_id: str,
    display_name: str,
    run_id: str,
    water_label: str,
    agency_source_label: str,
    agency_url: str,
    guide_title: str,
    guide_url: str,
    open_question_text: str,
) -> dict[str, object]:
    payload = copy.deepcopy(json.loads(FIXTURE_BATCH.read_text(encoding="utf-8")))
    payload["run_id"] = run_id
    payload["created_at"] = FIXED_CREATED_AT
    payload["subject"]["subject_id"] = subject_id
    payload["subject"]["display_name"] = display_name
    payload["subject"]["domain_pack"] = "general.v1"
    payload["subject"]["scope_statement"] = f"Synthetic place-dominant fixture for {display_name}."
    payload["domain_pack"]["pack_id"] = "general.v1"
    payload["domain_pack"]["status"] = "runtime"
    payload["domain_pack"]["selected_facet"] = "sources"
    payload["facet"]["name"] = "sources"
    payload["facet"]["candidate_type_hint"] = "source_lead"
    payload["phase"] = "01a"
    payload["mode"] = "dry_run"
    payload["engine"]["invoked"] = False
    payload["engine"]["engine_present"] = False
    payload["engine"]["resolved_engine"] = None
    payload["raw_engine_output"] = None
    payload["engine_output_ref"] = None
    payload["provenance"]["timestamp"] = FIXED_CREATED_AT
    payload["provenance"]["engine_invoked"] = False
    payload["provenance"]["engine_present"] = False
    payload["prompt"]["rendered_prompt_path"] = f"{run_id}/rendered-prompt.txt"
    payload["candidates"] = [
        {
            "candidate_id": f"cand:{subject_id}.source_lead",
            "candidate_type": "source_lead",
            "origin": "llm_proposed",
            "persistence_status": "workspace_run_only",
            "review_status": "unverified",
            "text": json.dumps(
                {
                    "source_lead_id": f"lead.{subject_id}.agency",
                    "original_locator": agency_url,
                    "canonical_url": agency_url,
                    "access_class": "web_reference",
                    "rights_posture": "quote_limited",
                    "citation_hint": agency_source_label,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
        {
            "candidate_id": f"cand:{subject_id}.work",
            "candidate_type": "work",
            "origin": "llm_proposed",
            "persistence_status": "workspace_run_only",
            "review_status": "unverified",
            "text": json.dumps(
                {
                    "work_key": f"work.{subject_id}.guide",
                    "title": guide_title,
                    "work_type": "guidebook",
                    "canonical_url": guide_url,
                    "original_locator": guide_url,
                    "citation_hint": guide_title,
                    "claim_text": f"{guide_title} points toward place-specific river access and hatch leads for {display_name}.",
                    "confidence_score": 0.92,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
        {
            "candidate_id": f"cand:{subject_id}.place",
            "candidate_type": "place",
            "origin": "llm_proposed",
            "persistence_status": "workspace_run_only",
            "review_status": "unverified",
            "text": json.dumps(
                {
                    "entity_label": water_label,
                    "normalized_label": water_label.lower(),
                    "entity_type": "water_body",
                    "confidence_score": 0.87,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
        {
            "candidate_id": f"cand:{subject_id}.open_question",
            "candidate_type": "open_question",
            "origin": "llm_proposed",
            "persistence_status": "workspace_run_only",
            "review_status": "unverified",
            "text": open_question_text,
        },
    ]
    rendered_prompt = _render_place_shape_prompt(payload)
    payload["prompt"]["rendered_prompt_hash"] = hashlib.sha256(
        rendered_prompt.encode("utf-8")
    ).hexdigest()
    return payload


def write_place_shape_batch(
    tmp_path: Path,
    *,
    subject_id: str,
    display_name: str,
    run_id: str,
    water_label: str,
    agency_source_label: str,
    agency_url: str,
    guide_title: str,
    guide_url: str,
    open_question_text: str,
) -> Path:
    payload = _place_shape_candidate_batch(
        subject_id=subject_id,
        display_name=display_name,
        run_id=run_id,
        water_label=water_label,
        agency_source_label=agency_source_label,
        agency_url=agency_url,
        guide_title=guide_title,
        guide_url=guide_url,
        open_question_text=open_question_text,
    )
    path = tmp_path / f"{run_id}-gather-candidate-batch.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    prompt_path = path.parent / str(payload["prompt"]["rendered_prompt_path"])
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(_render_place_shape_prompt(payload), encoding="utf-8")
    return path


def test_general_pack_place_shape_docs_do_not_overclaim_safe_first_cycle_coverage() -> None:
    doc = DOMAIN_PACKS_DOC.read_text(encoding="utf-8")

    assert "trout fly fishing in Montana" in doc
    assert "currently routes to `general.v1`" in doc
    assert "not yet a fixture-proven safe first-cycle coverage example for place-dominant recreation subjects" in doc
    assert "safe for broad-topic examples such as `trout fly fishing in Montana`" not in doc


def test_general_pack_place_shape_first_cycle_dry_run_is_runtime_reachable_but_not_coverage_proof(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    manifest_path = write_manifest(
        workspace_root,
        subject_id="topic.trout_fly_fishing_montana",
        display_name="trout fly fishing in Montana",
    )
    run_id = "montana-place-shape-cycle1"

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
    assert payload["domain_pack"]["pack_id"] == "general.v1"
    assert payload["domain_pack"]["status"] == "runtime"
    assert payload["facet"]["name"] == "sources"
    assert payload["candidates"] == []


def test_general_pack_place_shape_cycle_two_prior_state_is_subject_scoped_and_usable(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_db(tmp_path)
    montana_batch = write_place_shape_batch(
        tmp_path,
        subject_id="topic.trout_fly_fishing_montana",
        display_name="trout fly fishing in Montana",
        run_id="montana-seed",
        water_label="Missouri River",
        agency_source_label="Montana Fish, Wildlife & Parks river-access maps",
        agency_url="https://example.test/montana/fwp-river-access",
        guide_title="Missouri River Trout Access Guide",
        guide_url="https://example.test/montana/missouri-river-guide",
        open_question_text="Which Montana Fish, Wildlife & Parks archive or hatch-reference source should be reviewed next for trout fly fishing in Montana?",
    )
    connecticut_batch = write_place_shape_batch(
        tmp_path,
        subject_id="topic.trout_fly_fishing_connecticut",
        display_name="trout fly fishing in Connecticut",
        run_id="connecticut-seed",
        water_label="Farmington River",
        agency_source_label="Connecticut DEEP trout management reports",
        agency_url="https://example.test/connecticut/deep-trout-reports",
        guide_title="Farmington River Trout Access Guide",
        guide_url="https://example.test/connecticut/farmington-river-guide",
        open_question_text="Which Connecticut DEEP stocking, access, or hatch-reference source should be reviewed next for trout fly fishing in Connecticut?",
    )

    for batch_path in (montana_batch, connecticut_batch):
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
        finally:
            conn.close()

    conn = canonical_store.connect_canonical_store(db_path)
    try:
        montana_rows = conn.execute(
            "SELECT COUNT(*) FROM source_access WHERE workspace_id=?",
            ("topic.trout_fly_fishing_montana",),
        ).fetchone()[0]
        connecticut_rows = conn.execute(
            "SELECT COUNT(*) FROM source_access WHERE workspace_id=?",
            ("topic.trout_fly_fishing_connecticut",),
        ).fetchone()[0]
        detected_labels = {
            row["entity_label"]
            for row in conn.execute(
                "SELECT entity_label FROM extraction_detected_entity"
            ).fetchall()
        }
    finally:
        conn.close()

    assert int(montana_rows) >= 2
    assert int(connecticut_rows) >= 2
    assert "Missouri River" in detected_labels
    assert "Farmington River" in detected_labels

    montana_workspace = tmp_path / "montana-workspace"
    connecticut_workspace = tmp_path / "connecticut-workspace"
    montana_workspace.mkdir()
    connecticut_workspace.mkdir()
    montana_manifest = write_manifest(
        montana_workspace,
        subject_id="topic.trout_fly_fishing_montana",
        display_name="trout fly fishing in Montana",
    )
    connecticut_manifest = write_manifest(
        connecticut_workspace,
        subject_id="topic.trout_fly_fishing_connecticut",
        display_name="trout fly fishing in Connecticut",
    )

    montana_proc = run_driver(
        [
            "--subject",
            str(montana_manifest),
            "--workspace",
            str(montana_workspace),
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
            "montana-cycle-two",
            "--created-at",
            FIXED_CREATED_AT,
        ]
    )
    connecticut_proc = run_driver(
        [
            "--subject",
            str(connecticut_manifest),
            "--workspace",
            str(connecticut_workspace),
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
            "connecticut-cycle-two",
            "--created-at",
            FIXED_CREATED_AT,
        ]
    )

    assert montana_proc.returncode == 0, montana_proc.stdout + montana_proc.stderr
    assert connecticut_proc.returncode == 0, connecticut_proc.stdout + connecticut_proc.stderr

    montana_payload = json.loads(
        batch_path_for(montana_workspace, "montana-cycle-two").read_text(encoding="utf-8")
    )
    connecticut_payload = json.loads(
        batch_path_for(connecticut_workspace, "connecticut-cycle-two").read_text(encoding="utf-8")
    )
    montana_prompt = prompt_path_for(montana_workspace, "montana-cycle-two").read_text(encoding="utf-8")
    connecticut_prompt = prompt_path_for(connecticut_workspace, "connecticut-cycle-two").read_text(encoding="utf-8")

    assert montana_payload["domain_pack"]["pack_id"] == "general.v1"
    assert montana_payload["domain_pack"]["status"] == "runtime"
    assert montana_payload["cycle_depth"] == 2
    assert montana_payload["prior_state"]["context_hash"]
    assert montana_payload["prior_state"]["record_counts"]["works"]["total"] >= 1
    assert montana_payload["prior_state"]["record_counts"]["source_access"]["total"] >= 1
    assert montana_payload["prior_state"]["record_counts"]["source_claims"]["total"] >= 1
    assert "PRIOR CANONICAL STATE CONTEXT" in montana_prompt
    assert "Missouri River" in montana_prompt
    assert "https://example.test/montana/fwp-river-access" in montana_prompt
    assert "Missouri River Trout Access Guide" in montana_prompt
    assert "Which Montana Fish, Wildlife & Parks archive or hatch-reference source should be reviewed next" in montana_prompt
    assert "Farmington River" not in montana_prompt
    assert "Connecticut DEEP" not in montana_prompt

    assert connecticut_payload["cycle_depth"] == 2
    assert connecticut_payload["prior_state"]["context_hash"]
    assert "Farmington River" in connecticut_prompt
    assert "https://example.test/connecticut/deep-trout-reports" in connecticut_prompt
    assert "Farmington River Trout Access Guide" in connecticut_prompt
    assert "Which Connecticut DEEP stocking, access, or hatch-reference source should be reviewed next" in connecticut_prompt
    assert "Missouri River" not in connecticut_prompt
    assert "Montana Fish, Wildlife & Parks" not in connecticut_prompt
