"""Shared static page-family query contract constants."""

from __future__ import annotations


SCHEMA_VERSION = "static-page-family-query-contract.v1"
SCHEMA_PATH = "config/static_page_family_query_contract.schema.json"
CONTRACT_DOC = "docs/project/STATIC_PAGE_FAMILY_QUERY_CONTRACT.md"
CONTRACT_PATH = "config/static_page_family_query_contract.json"

REQUIRED_PAGE_FAMILIES = {
    "home",
    "facet",
    "entity",
    "source",
    "collection",
    "timeline",
    "validation",
    "search_results",
}

ALLOWED_INPUT_KINDS = {
    "query",
    "projection",
    "sidecar",
}

ALLOWED_READER_STATES = {
    "ready",
    "sparse",
    "empty",
    "blocked",
}
