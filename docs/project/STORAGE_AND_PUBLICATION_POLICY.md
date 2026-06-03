# Storage And Publication Policy

## Core Position

Summa is local-first.

- local storage is the system of record
- publication is a filtered projection
- raw payloads are transient by default
- backups and preservation are explicit operator actions, not hidden network
  side effects

This document summarizes the current storage and publication posture in the
checked-in tree.

## Local Storage Layers

Current local storage layers include:

- workspace-local runtime state under ignored roots such as `runtime/`
- local SQLite stores under ignored roots such as `dbs/`
- the canonical graph store created by
  `tools/source_db_tools/init_canonical_store.py`
- sidecars and ledgers such as runtime and migration ledgers
- generated local search projections and indexes

The root `README.md` remains the clearest high-level statement: Summa is not a
raw-payload archive, and durable value lives in structured records, review
posture, provenance, and controlled projections.

## What Belongs Where

Local databases:

- canonical graph rows, review history, provenance, identifiers, and related
  structured records
- source/work and SQLite helper data that current `tools/source_db_tools/`
  surfaces already manage
- local search indexes and derived SQLite rollups when a tool explicitly emits
  them

Workspace and runtime outputs:

- local topic workspace registries
- runtime ledgers, locks, backup manifests, and drill reports
- temporary or operator-selected run artifacts
- publication build outputs before a contributor chooses to preserve them

Public output:

- validated export and presentation artifacts
- rendered static site output
- sanitized public sharing bundles
- safekeeping manifests for manual preservation

Tests and fixtures:

- safe stand-ins for schemas, prompts, leak findings, source adapters, and
  publication inputs
- contract examples, not real local operator data

## Raw Payload And Extracted Text Posture

Current policy from code and docs:

- raw payloads are transient processing inputs by default
- full extracted text may still contain private or restricted material
- extracted text does not automatically become public-safe just because it was
  parsed successfully
- publication builders and sharing bundles exclude raw payload and restricted
  text families by default

When current tools preserve anything payload-adjacent, they prefer hashes,
metadata, or sanitized summaries over durable raw bytes.

## Hashes, Metadata, And Capture / Extraction Posture

The current tree uses hashes and metadata in several places:

- publication outputs and safekeeping manifests record SHA-256 hashes
- backup drills and bundle manifests record file hashes and sizes
- canonical storage includes `capture_event` and `extraction_record` tables for
  durable capture/extraction metadata

The checked-in policy direction is to preserve structured metadata, provenance,
and review posture rather than treat raw payload dumps as the primary durable
object.

## Canonical Store And SQLite Helpers

The current branch includes a canonical-store bootstrap:

- `tools/source_db_tools/init_canonical_store.py`
- `tools/source_db_tools/canonical_store.py`
- `tools/source_db_tools/schema/migrations/0001_canonical_store.sql`

The canonical model is summarized in
[CANONICAL_GRAPH_MODEL.md](CANONICAL_GRAPH_MODEL.md). Historical context for
the wider SQLite helper line is summarized in
[../history/sqlite-tooling-history.md](../history/sqlite-tooling-history.md).

## Backup And Safekeeping

Current backup-related surfaces include:

- `config/durability_policies/local_first_crown_jewels.v1.json`
- `tools/common/crown_jewel_backup.py`
- `tools/scripts/Index_Plan_Crown_Jewel_Backup.sh`
- `tools/scripts/topic_backup_drill.py`
- `tools/source_db_tools/sqlite_safety.py`

Current preservation and publication-handoff surfaces include:

- `tools/scripts/build_public_sharing_bundle.py`
- `tools/scripts/build_public_safekeeping_manifest.py`
- `docs/project/PUBLIC_SAFEKEEPING_MANIFEST.md`

Current safekeeping posture is manual and explicit:

- bundle manifests and safekeeping manifests require `upload_attempted: false`
- no checked-in publication or safekeeping tool creates remotes or uploads
  archives automatically

## What Is Preserved Versus Regenerated

Prefer preserving:

- local registries
- canonical or curated SQLite state
- review history and provenance
- backup manifests, snapshots, and validation reports when explicitly created
- public sharing bundles or safekeeping manifests chosen for manual handoff

Prefer regenerating:

- static site output
- presentation artifacts
- local search projections and results
- view-model reports
- leak-scan reports

The public site is a projection from validated inputs, not a replacement for
the underlying local store.

## Git Boundary

The current `.gitignore` keeps these out of tracked history by default:

- `runtime/**`
- `dbs/**`
- `index/Places/**`
- `test_corpora/**`
- `.local/**`
- `out/**`
- `build/`
- `dist/`
- local database files and most PDFs

Tracked fixtures are exceptions made for tests. They are not permission to
check in real user data, secrets, runtime logs, or local payload archives.

## Publication Rule

Publication must remain a filtered projection.

- private/local/raw material must not be published by default
- local file paths should not appear in public bundles unless a surface
  explicitly treats them as public-safe
- review notes and internal validation state are private unless a schema and
  validator explicitly promote a sanitized summary
- leak scanning is a guardrail layered on top of producer filtering, not a
  substitute for correct filtering

See
[PUBLIC_PRIVATE_EXPORT_BOUNDARY.md](PUBLIC_PRIVATE_EXPORT_BOUNDARY.md) for the
current public/private boundary details.
