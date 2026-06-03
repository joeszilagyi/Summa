# Schema Profile Validation

`tools/source_db_tools/schema_profile_validation.py` validates canonical source
records loaded from SQLite against checked-in profile rules in
`tools/source_db_tools/schema_profiles.json`.

The paired CLI wrapper is `tools/source_db_tools/validate_schema_profile.py`.
That wrapper:

- loads records through `export_bibliography.load_records`
- validates them against one named profile
- emits a machine-readable `schema-profile-validation-report.v1`

Profile validation is intentionally a boundary check, not a database schema
migration. The SQLite database can remain permissive while this validator
applies stricter promotion, review, export, and handoff rules.

Important dependencies:

- `source_types.py` for source/work type policy
- `identifier_normalization.py` for identifier shape and normalization checks
- `rights_retention.py` for payload visibility and retention policy checks
- `relationship_predicates.py` for relationship vocabulary enforcement
- `claim_types.py` for claim vocabulary and evidence requirements
- `confidence_model.py` for confidence score and band validation

When changing profile semantics or report shape:

1. update `schema_profiles.json` if the rule set changes
2. keep `schema_profile_validation.py` and `validate_schema_profile.py` aligned
3. rerun source-db registry/profile tests and any export tests that consume the
   validator output

The validator is local-only and does not mutate the database.
