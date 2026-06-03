from __future__ import annotations

from tools.common.canonical_graph_model_contract import (
    DOCUMENTED_EXPECTED_SQLITE_TABLES,
    REQUIRED_NONCANONICAL_STAGING_TABLES,
    REQUIRED_RECORD_FAMILIES,
    REQUIRED_SCHEMA_METADATA_TABLES,
    REQUIRED_SUPPORTING_SQLITE_TABLES,
)
from tools.source_db_tools import canonical_store


def test_outline_maps_all_required_families_and_documented_tables() -> None:
    outline = canonical_store.load_canonical_outline()
    family_map = canonical_store.family_table_mapping(outline)

    assert set(family_map) == REQUIRED_RECORD_FAMILIES
    assert DOCUMENTED_EXPECTED_SQLITE_TABLES.issubset(canonical_store.expected_tables_from_outline(outline))
    assert family_map["confidence_assessment"] == {
        "authority_record",
        "source_claim",
        "source_relationship",
        "topic_extension",
    }
    assert family_map["review_annotation"] == {
        "authority_reconciliation",
        "review_state_history",
    }


def test_outline_classifies_supporting_metadata_and_staging_tables() -> None:
    outline = canonical_store.load_canonical_outline()

    assert canonical_store.supporting_tables_from_outline(outline) == REQUIRED_SUPPORTING_SQLITE_TABLES
    assert canonical_store.schema_metadata_tables_from_outline(outline) == REQUIRED_SCHEMA_METADATA_TABLES
    assert canonical_store.staging_tables_from_outline(outline) == REQUIRED_NONCANONICAL_STAGING_TABLES

    classified = canonical_store.classified_outline_tables(outline)
    assert classified["schema_version"] == "schema_metadata"
    assert classified["schema_migration_history"] == "schema_metadata"
    assert classified["source_access"] == "supporting"
    assert classified["source_locus"] == "noncanonical_staging"
