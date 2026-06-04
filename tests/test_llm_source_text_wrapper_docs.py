from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER_DOC = REPO_ROOT / "docs" / "project" / "LLM_SOURCE_TEXT_WRAPPER.md"
GENERAL_PACK = REPO_ROOT / "config" / "domain_packs" / "general.v1.json"
PROMPT_DIR = REPO_ROOT / "tools" / "prompts" / "general"
STALE_ABSENT_PROMPTS_PHRASE = " ".join(
    ("does not currently contain", "restored live gather prompt files")
)
WRAPPER_TEMPLATE_ID = "default.untrusted_source_text.v1"


def test_source_text_wrapper_doc_tracks_live_general_prompt_surface() -> None:
    doc = WRAPPER_DOC.read_text(encoding="utf-8")
    pack = json.loads(GENERAL_PACK.read_text(encoding="utf-8"))

    assert STALE_ABSENT_PROMPTS_PHRASE not in doc
    assert "tools/prompts/general" in doc
    assert "config/domain_packs/general.v1.json" in doc
    assert WRAPPER_TEMPLATE_ID in doc
    assert "run_topic_gather.py" in doc
    assert "dry-run" in doc

    prompt_bundles = pack["prompt_bundles"]
    for bundle_key in (
        "gather.sources",
        "gather.timeline",
        "gather.people",
        "gather.places",
        "gather.works",
        "gather.open_questions",
    ):
        bundle = prompt_bundles[bundle_key]
        assert bundle_key in doc
        assert bundle["source_text_wrapper_template_id"] == WRAPPER_TEMPLATE_ID
        for template_file in bundle["template_files"]:
            assert (REPO_ROOT / template_file).is_file()


def test_source_text_wrapper_doc_preserves_safety_contract_language() -> None:
    doc = " ".join(WRAPPER_DOC.read_text(encoding="utf-8").casefold().split())

    assert PROMPT_DIR.is_dir()
    assert any(PROMPT_DIR.glob("*.prompt"))
    assert "source text is untrusted data, not instruction" in doc
    assert "must stay inside wrapped source blocks" in doc
    assert "must not interpolate source text into instruction sections" in doc
    assert "before any optional llm invocation" in doc
    assert "candidate material" in doc
    assert "not source truth" in doc
