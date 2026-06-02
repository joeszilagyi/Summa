# Canonical Graph Model

## Purpose

This document defines the intended canonical knowledge layer for Summa's
durable map. The canonical layer is not a presentation schema, a search index,
or a public export shape. It is the stable substrate that downstream views,
validators, and publication surfaces should project from.

## Core Position

- Canonical storage is local-first and durable.
- Presentation layers are downstream.
- Append-only evidence, review, and correction artifacts remain first-class.
- Later runtime work should improve the canonical layer without destructive
  overwrite.

## Canonical Record Families

The first canonical outline names six record families:

- `entity`: canonical people, groups, places, works, detected local entities,
  and reconciled authority-backed nodes.
- `relationship`: directed, source-backed links between canonical entities and
  scoped subject assignments.
- `assertion`: source-backed claims and extracted statements that may later
  support canonical entities or relationships.
- `provenance_event`: append-only events explaining discovery, capture,
  extraction, review, merge, and export actions.
- `confidence_assessment`: normalized confidence posture attached to canonical
  records without erasing original score context.
- `review_annotation`: operator review history and durable review posture for
  record- and field-level decisions.

## Sidecars

The canonical layer is surrounded by append-only sidecars rather than folding
those concerns into presentation records:

- `correction-ledger.v1`
- `field-review-state.v1`
- `evidence-locator.v1`

These sidecars preserve lineage and public-safety posture around canonical
records. They are not optional presentation metadata.

## Current SQLite Mapping

The current runtime already holds graph-shaped material in several places:

- `authority_record`, `extraction_detected_entity`, `work`, and `work_subject`
  provide entity-like durable rows.
- `source_relationship` and `work_subject` provide relationship-like edges.
- `source_claim` and `topic_extension` provide assertion-like content.
- `provenance_event`, `capture_event`, and `extraction_record` provide
  provenance history.
- `review_state_history` and `authority_reconciliation` provide review-oriented
  annotations.

This document does not require an immediate table rewrite. The first goal is to
make the canonical ownership model explicit so later runtime work stops
building directly on importer-specific or presentation-specific row shapes.

## Migration Direction

The first migration stages are documentation and contract work:

1. Map current SQLite families into canonical record families.
2. Treat correction, field review, and evidence locator artifacts as
   canonical-layer sidecars.
3. Move later runtime work such as feedback loops, projections, and publication
   gates onto canonical graph records rather than ad hoc joins.

The checked-in machine-readable outline in
`config/canonical_graph_model_outline.json` is the executable companion to this
document.
