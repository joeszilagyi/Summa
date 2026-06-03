# Relationship Predicates

`tools/source_db_tools/relationship_predicates.py` loads the checked-in
`tools/source_db_tools/relationship_predicates.yml` registry and validates
`source_relationship` rows against it.

Key expectations:

- The registry file keeps the historical `.yml` name but stores JSON content.
- Top-level payload keys are `schema_version` and `predicates`.
- Each row must provide a unique non-empty `predicate`.
- Predicate matching is case-insensitive after trim/lower normalization.

The helper currently provides:

- registry loading and duplicate detection
- lookup by normalized predicate
- evidence checks for predicates that require `evidence_locator` or
  `evidence_highlight_id`
- inverse-row derivation when a registry definition declares
  `inverse_predicate`

When updating predicate semantics, keep the registry rows, helper behavior, and
profile/export tests aligned. This module is a local contract surface, not a
network-backed vocabulary source.
