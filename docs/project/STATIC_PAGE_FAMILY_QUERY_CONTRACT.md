# Static Page Family Query Contract

## Purpose

This contract defines the query-facing input shape for Summa's static page
families. It sits between durable records and later HTML generation. The point
is to stop page builders from reaching directly into ad hoc joins or view-only
objects without naming what each page family expects.

## Scope

This contract covers:

- `home`
- `facet`
- `entity`
- `source`
- `collection`
- `timeline`
- `validation`
- `search_results`

It does not implement the page builder itself. It defines the query and
projection expectations those builders must follow.

## Core Rules

1. Each page family names at least one required input query or projection.
2. Every family defines a `sparse_state`, not just empty vs populated output.
3. Public and private fields are explicitly separated.
4. Optional public-safe summaries such as lineage or evidence notes are called
   out as conditional rather than treated as ordinary public fields.
5. Empty and populated examples are mandatory so later builders have fixture
   reality, not only prose.

## Input Vocabulary

The checked-in machine-readable contract allows three input kinds:

- `query`
- `projection`
- `sidecar`

`query` names the page-facing retrieval contract. `projection` names a derived
artifact such as `local-search-projection.v1`. `sidecar` names lineage, review,
or evidence contracts that affect what can safely appear on the page.

## Visibility Rules

Every page family carries `field_visibility` with:

- `public_fields`
- `private_fields`
- `conditional_public_fields`

`conditional_public_fields` exists for fields that may be public-safe only
after review, redaction, lineage resolution, or blocker clearance. This keeps
page contracts aligned with the repo's existing authority, evidence, and leak
gates.

## Sparse State

`sparse_state` is not failure. It means the page may publish with reduced
content because some reviewed/current data is missing or blocked while enough
public-safe structure remains for navigation and operator understanding.

## Checked-In Contract

The executable companion to this document is
`config/static_page_family_query_contract.json`.
