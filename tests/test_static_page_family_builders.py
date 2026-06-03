from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.common.publication_builder import PAGE_FAMILIES, build_knowledge_tree_export_payload, build_public_presentation_payload

from tests.publication_fixture_store import FIXED_TIMESTAMP, PRIVATE_SENTINEL, create_populated_canonical_store, create_sparse_canonical_store


def build_payloads(tmp_path: Path, *, sparse: bool) -> tuple[dict[str, object], dict[str, object]]:
    db_path = create_sparse_canonical_store(tmp_path) if sparse else create_populated_canonical_store(tmp_path)
    export_payload = build_knowledge_tree_export_payload(db_path, generated_at=FIXED_TIMESTAMP).payload
    presentation_payload = build_public_presentation_payload(export_payload)
    return export_payload, presentation_payload


@pytest.mark.parametrize("family", PAGE_FAMILIES)
def test_page_family_builders_emit_populated_family_models(tmp_path: Path, family: str) -> None:
    export_payload, presentation_payload = build_payloads(tmp_path, sparse=False)
    export_page = next(page for page in export_payload["pages"] if page["page_family"] == family)
    presentation_page = next(page for page in presentation_payload["page_inventory"] if page["page_family"] == family)

    assert export_page["route"] == presentation_page["route"]
    assert export_page["sections"]
    assert export_page["summary_cards"]
    assert presentation_page["reader_state"] == "ready"
    assert PRIVATE_SENTINEL not in json.dumps(export_page, ensure_ascii=False, sort_keys=True)
    assert PRIVATE_SENTINEL not in json.dumps(presentation_page, ensure_ascii=False, sort_keys=True)


@pytest.mark.parametrize(
    ("family", "expected_state"),
    [
        ("home", "sparse"),
        ("facet", "empty"),
        ("entity", "empty"),
        ("source", "empty"),
        ("collection", "empty"),
        ("timeline", "empty"),
        ("validation", "ready"),
        ("search_results", "empty"),
    ],
)
def test_page_family_builders_emit_sparse_family_models(tmp_path: Path, family: str, expected_state: str) -> None:
    export_payload, presentation_payload = build_payloads(tmp_path, sparse=True)
    export_page = next(page for page in export_payload["pages"] if page["page_family"] == family)
    presentation_page = next(page for page in presentation_payload["page_inventory"] if page["page_family"] == family)

    assert export_page["route"] == presentation_page["route"]
    assert export_page["sections"]
    assert export_page["summary_cards"]
    assert presentation_page["reader_state"] == expected_state
    if expected_state != "ready":
        assert presentation_page["empty_state"]
    assert PRIVATE_SENTINEL not in json.dumps(export_page, ensure_ascii=False, sort_keys=True)
    assert PRIVATE_SENTINEL not in json.dumps(presentation_page, ensure_ascii=False, sort_keys=True)

