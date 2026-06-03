# Claim Types

`tools/source_db_tools/claim_types.py` loads the checked-in
`tools/source_db_tools/claim_types.yml` registry and exposes helper functions
used by schema-profile validation and export shaping.

Key expectations:

- The registry file keeps the historical `.yml` name but stores JSON content.
- Top-level payload keys are `schema_version` and `claim_types`.
- Each row must provide a non-empty `claim_type`.
- Registry lookups normalize values by trimming, lowercasing, and replacing
  spaces or hyphens with underscores.

Runtime checks exposed by the helper:

- unknown `source_claim.claim_type` values
- missing evidence for claim types with `evidence_mandatory`
- missing human approval when the registry marks a claim type as
  `human_review_required`

When changing claim semantics:

1. update `claim_types.yml`
2. keep `claim_types.py` normalization and evidence behavior aligned
3. rerun source-db registry and schema-profile tests

The helper is intentionally local and deterministic. It does not fetch remote
registries and it does not mutate source records.
