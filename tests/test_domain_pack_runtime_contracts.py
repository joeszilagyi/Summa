from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "tools" / "scripts"
RESOLVE_GATHER = SCRIPTS_DIR / "resolve_gather_domain_pack.py"
SUBJECT_DETAIL = SCRIPTS_DIR / "build_subject_detail_view.py"
RESOLVE_RUNTIME = SCRIPTS_DIR / "resolve_subject_runtime.py"


def load_module(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


resolve_gather_domain_pack = load_module(RESOLVE_GATHER, "resolve_gather_domain_pack_for_tests")
build_subject_detail_view = load_module(SUBJECT_DETAIL, "build_subject_detail_view_for_tests")
resolve_subject_runtime = load_module(RESOLVE_RUNTIME, "resolve_subject_runtime_for_tests")


def write_manifest(tmp_path: Path, *, domain_pack: str, enabled_facets: list[str]) -> Path:
    manifest = tmp_path / "subject_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "subject-manifest.v1",
                "subject_id": "topic.demo",
                "display_name": "Demo Topic",
                "domain_pack": domain_pack,
                "scope_statement": "Fixture manifest for domain-pack runtime tests.",
                "languages": ["en"],
                "aliases": [],
                "disambiguation_terms": [],
                "excluded_senses": [],
                "enabled_facets": enabled_facets,
                "query_families": [],
                "public_export_default": False,
                "legacy_substrate_paths": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest


def run_cli(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(path), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_resolve_gather_domain_pack_general_facets_are_runtime_resolvable() -> None:
    payload = resolve_gather_domain_pack.resolve_gather_domain_pack("general.v1", None)

    assert payload["schema_version"] == "gather-domain-pack-resolution.v1"
    assert payload["domain_pack"] == "general.v1"
    assert payload["selected_facets"] == [
        "sources",
        "timeline",
        "people",
        "places",
        "works",
        "open_questions",
    ]
    assert payload["facets"]["sources"]["prompt_bundle_id"] == "general.gather.sources.v1"
    assert payload["facets"]["sources"]["source_text_wrapper_template_id"] == "default.untrusted_source_text.v1"
    assert payload["facets"]["sources"]["01a_prompt"] == "general.sources.seed"
    assert payload["facets"]["sources"]["01r_prompt"] == "general.sources.review"
    assert payload["facets"]["sources"]["template_files"] == [
        "tools/prompts/general/general.sources.seed.prompt",
        "tools/prompts/general/general.sources.review.prompt",
    ]
    assert payload["facets"]["open_questions"]["prompt_bundle_id"] == "general.gather.open_questions.v1"
    assert payload["facets"]["open_questions"]["01a_output_stem"] == "general_gather_open_questions_v1"


def test_resolve_gather_domain_pack_organism_shape_defaults_all_enabled_facets() -> None:
    payload = resolve_gather_domain_pack.resolve_gather_domain_pack("organism.v1", None)

    assert payload["selected_facets"] == [
        "taxonomy",
        "range",
        "habitat",
        "observations",
    ]
    assert payload["facets"]["taxonomy"]["01a_prompt"] == "taxonomy.seed"
    assert payload["facets"]["range"]["01r_prompt"] == "range.review"
    assert payload["facets"]["habitat"]["prompt_bundle_id"] == "organism.gather.habitat.v1"
    assert payload["facets"]["habitat"]["source_text_wrapper_template_id"] == "default.untrusted_source_text.v1"
    assert payload["facets"]["observations"]["01a_output_stem"] == "organism_gather_observations_v1"


def test_non_article_facet_resolution_does_not_fall_back_to_article_assumptions() -> None:
    candidate_keys = resolve_subject_runtime.prompt_bundle_candidate_keys("open_questions")

    assert candidate_keys == ("gather.open_questions", "open_questions")
    payload = resolve_gather_domain_pack.resolve_gather_domain_pack("general.v1", "open_questions")
    assert payload["facets"]["open_questions"]["prompt_bundle_id"] == "general.gather.open_questions.v1"
    assert "article" not in payload["facets"]["open_questions"]["01a_output_stem"]
    assert payload["facets"]["open_questions"]["01a_prompt"] == "general.open_questions.seed"


def test_subject_detail_view_uses_normalized_prompt_bundle_metadata(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path, domain_pack="organism.v1", enabled_facets=["taxonomy", "habitat"])

    payload = build_subject_detail_view.build_subject_detail_payload(
        type("Args", (), {"manifest": str(manifest), "format": "json"})()
    )

    assert payload["schema_version"] == "subject-detail.v1"
    assert payload["domain_pack"]["pack_id"] == "organism.v1"
    facets = {entry["facet"]: entry for entry in payload["facets"]}
    assert facets["taxonomy"]["prompt_bundle_status"] == "ok"
    assert facets["taxonomy"]["prompt_bundle_id"] == "organism.gather.taxonomy.v1"
    assert facets["taxonomy"]["legacy_01a_output_stem"] == "organism_gather_taxonomy_v1"
    assert facets["taxonomy"]["phase_templates"] == {"01a": "taxonomy.seed", "01r": "taxonomy.review"}
    assert facets["taxonomy"]["source_text_wrapper_template_id"] == "default.untrusted_source_text.v1"
    assert [item["status"] for item in facets["taxonomy"]["template_file_statuses"]] == ["ok", "ok"]
    assert facets["habitat"]["prompt_bundle_status"] == "ok"
    assert facets["habitat"]["prompt_bundle_key"] == "gather.habitat"
    assert facets["habitat"]["phase_templates"] == {"01a": "habitat.seed", "01r": "habitat.review"}
    assert [item["status"] for item in facets["habitat"]["template_file_statuses"]] == ["ok", "ok"]


def test_domain_pack_runtime_tools_compile_and_cli_runs() -> None:
    subprocess.run(
        [sys.executable, "-m", "py_compile", str(RESOLVE_RUNTIME), str(RESOLVE_GATHER), str(SUBJECT_DETAIL)],
        cwd=REPO_ROOT,
        check=True,
    )

    result = run_cli(RESOLVE_GATHER, "--domain-pack", "general.v1")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "gather-domain-pack-resolution.v1"
