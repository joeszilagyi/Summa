# Public / Private Export Boundary

## Purpose

This document describes the current boundary between local/private material and
public/exportable material in Summa's publication surfaces.

The boundary is implemented through a mix of producer filtering, schema
validation, and leak scanning. Treat unknown or unclassified fields as private
by default.

## Private or Internal Material

Private or internal material includes:

- raw payload captures and raw build manifests
- full extracted text and unrestricted excerpts
- prompt outputs and prompt-bundle internals
- runtime logs and log tails
- private absolute paths and local locator paths
- credentials and secret-looking tokens
- private operator notes and internal review notes
- direct database snapshots and other storage-oriented internals
- unreviewed source text and blocked publication material

These families are explicitly excluded from public sharing today. The current
publication helper and sharing-bundle builder use the same language:

- `private local payload paths`
- `raw prompt output`
- `runtime logs`
- `private operator notes`
- `unreviewed source text`
- `restricted files`
- `credentials`

`tools/scripts/build_public_sharing_bundle.py` adds explicit excluded families
for `raw_build_manifest`, `raw_payloads`, `prompt_outputs`, `runtime_logs`,
`private_paths`, and `restricted_text`.

## Public or Exportable Material

Current public/exportable material is a filtered projection that may include:

- validated `knowledge_tree_export.json`
- validated `public_presentation.json`
- rendered static HTML/CSS output
- sanitized public sharing bundle metadata summaries
- public-safe provenance or validation summaries that do not expose local
  locator paths, raw payloads, or private notes

Public output is not a direct dump of the canonical store, runtime directories,
or local workspace state.

## Current Publication Path

The current branch includes a real publication chain:

1. `tools/scripts/build_knowledge_tree_export.py`
2. `tools/scripts/build_public_knowledge_tree_presentation.py`
3. `tools/scripts/build_static_knowledge_tree.py`
4. `tools/scripts/build_publication_artifacts.py`
5. `tools/scripts/build_public_sharing_bundle.py`
6. `tools/scripts/build_public_safekeeping_manifest.py`

Current guardrails in that path:

- `build_knowledge_tree_export.py` and `tools/common/publication_builder.py`
  only project reviewed/current public-safe rows from the canonical store
- `build_public_knowledge_tree_presentation.py` validates the export before
  creating public presentation metadata
- `build_static_knowledge_tree.py` validates both export and presentation
  inputs before rendering
- `build_publication_artifacts.py` leak-scans the rendered static site before
  returning success
- `build_public_sharing_bundle.py` copies only public site files plus sanitized
  metadata summaries, then leak-scans the bundle again
- `build_public_safekeeping_manifest.py` records hashes and rights posture for
  manual preservation only and requires `upload_attempted: false`

## Review, Evidence, and Authority Gates

Current code treats review and publication posture as part of the public gate.

- `tools/common/publication_builder.py` treats a row as public only when its
  review state is in the searchable reviewed set, its `public_blocker` is
  clear, and its normalized publication state is in the public-searchable
  states
- unreviewed or blocked material is not published as settled public-safe
  output
- source claims remain source claims; they are not promoted into public fact
  just because they exist in storage
- public pages and summaries may expose validation posture, but they do so as
  sanitized page metadata rather than raw private review logs

## Conditional Public Fields

The checked-in page-family contract
`docs/project/STATIC_PAGE_FAMILY_QUERY_CONTRACT.md` uses
`conditional_public_fields` for fields that may become public only after
review, redaction, or blocker clearance.

Current implementation note:

- the current publication builders do not expose arbitrary field-level
  conditionals from storage
- instead, they emit a fixed sanitized export and presentation shape and rely
  on validators plus leak scanning
- any field not explicitly carried by the current public schemas should be
  treated as private by default

## What Must Never Enter Public Output

The current repo intends these to stay out of public presentation, static
output, sharing bundles, and safekeeping manifests by default:

- private filesystem paths
- raw payload bytes
- full extracted text
- credentials
- prompt outputs
- runtime logs
- internal notes
- unpublished local bundle internals

Leak scanning is a guardrail, not a substitute for correct producer filtering.

## Current Enforcement Surfaces

Current checked-in enforcement and regression paths include:

- `tools/scripts/scan_for_leaks.py`
- `tools/common/leak_scanner.py`
- `tools/validators/validate_knowledge_tree_export.py`
- `tools/validators/validate_public_knowledge_tree_presentation.py`
- `tools/validators/validate_static_knowledge_tree_output.py`
- `tools/validators/validate_public_safekeeping_manifest.py`
- `tests/test_leak_scanner.py`
- `tests/test_public_sharing_bundle.py`
- `tests/test_public_safekeeping_manifest.py`
- `tests/test_publication_artifact_roundtrip.py`

If a future surface wants to publish a new field family, it should add the
schema change, the validator/test coverage, and the boundary review together.
