# Repository Capabilities

Summa keeps the runnable product surface in a machine-readable index:

- index: `config/repository_capabilities.v1.json`
- schema: `config/repository_capabilities.v1.schema.json`
- validator: `tools/scripts/validate_repository_capabilities.py`

This page is the operator-readable companion to that index. It is not a
marketing list and it is not a runtime registry. It records what is live,
package-exposed, wrapper-exposed, validator-only, standards-profile based,
internal, legacy, or intentionally excluded.

## Package Console Commands

The installable package exposes these current `summa-*` commands:

- `summa-new-topic`
- `summa-build-knowledge-tree`
- `summa-workspace-overview`
- `summa-subject-detail`
- `summa-source-intake-status`
- `summa-review-queue`
- `summa-local-doctor`
- `summa-operator-dashboard`
- `summa-operator-path-smoke`
- `summa-resolve-gather-domain-pack`
- `summa-init-canonical-store`
- `summa-run-gather`
- `summa-execute-source-adapter`
- `summa-ingest-gather-candidate-batch`
- `summa-ingest-execution-artifacts`
- `summa-run-topic-cycle`
- `summa-run-scheduled-topic-cycles`
- `summa-select-scheduled-workspaces`
- `summa-apply-review-decision`
- `summa-evaluate-network-safety-gate`
- `summa-replay-canonical-write-spool`
- `summa-audit-canonical-graph-closure`
- `summa-export-redacted-diagnostics`
- `summa-audit-rebuildability`

The capability index maps each command back to its Python target, wrapper when
one exists, docs path, tests, network posture, canonical-store mutation posture,
and public/private risk classification.

## Shell Wrappers

`tools/scripts/Index_*.sh` wrappers remain supported compatibility surfaces.
Wrappers that map to package commands are indexed with the equivalent
`summa-*` command.

Wrapper-only or excluded surfaces are indexed with an explicit reason:

- `tools/scripts/Index_Build_Release_Readiness_Bundle.sh` is live but currently
  wrapper-only.
- `tools/scripts/Index_Plan_Crown_Jewel_Backup.sh` is a legacy compatibility
  wrapper around crown-jewel backup planning.
- `tools/scripts/Index_Topic_Backup_Drill.sh` is a backup-drill serviceability
  wrapper outside the current package console surface.

## Release Readiness

Release readiness has two indexed surfaces:

- `tools/scripts/build_release_readiness_bundle.py` assembles the upstream
  reports required by the readiness validator.
- `tools/validators/validate_release_readiness.py` aggregates the staged
  readiness reports.

The bundle builder is not a publication redesign and does not create new
validation semantics. The capability index exists so these producer and
validator surfaces do not drift apart again.

## Standards Profiles

The current standards-profile layer is indexed as explicit adapters:

- `config/standards_profiles/dcmi.v1.json`
- `config/standards_profiles/premis.v1.json`
- `config/standards_profiles/rico.v1.json`
- `config/standards_profiles/nara_preservation_readiness.v1.json`

The standards-profile export command is:

- `tools/scripts/export_standards_profile.py`

The index does not claim native standards compliance. It records the checked-in
profile adapters and their validation coverage.

## Validators

Representative validator surfaces are indexed as validator-only capabilities,
including release readiness, knowledge-tree export, static output, local-search
projection, gather candidate batches, and this repository capability index.
Validators are not automatically package console commands.

## Internal Helpers

Core internal helper surfaces such as canonical store APIs, canonical ingestion,
canonical reconciliation, and review-decision application are indexed as
internal helper capabilities. This records that they are product-critical
without making them direct operator commands.

## Adding A Surface

When adding a new live operator surface, update:

- `config/repository_capabilities.v1.json`
- docs for the new surface, or an explicit exclusion reason
- tests that guard the surface
- `pyproject.toml` if it should be package-exposed

When adding a standards profile, update:

- `config/standards_profiles/`
- `config/repository_capabilities.v1.json`
- standards profile tests

When adding a release-readiness producer or validator, update:

- `config/repository_capabilities.v1.json`
- relevant release-readiness docs
- release-readiness tests

The drift tests fail if a new package command, `Index_*.sh` wrapper, standards
profile, or release-readiness builder surface appears without index coverage.
