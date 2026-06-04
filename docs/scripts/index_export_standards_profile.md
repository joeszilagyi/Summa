# Export Standards Profile

`tools/scripts/export_standards_profile.py` exports canonical Summa rows through
explicit standards-profile adapters. It does not rename canonical fields and it
does not mutate the canonical SQLite store.

## Purpose

- Build deterministic standards-profile JSON artifacts from canonical rows.
- Emit a conformance report that lists satisfied, missing, lossy, unsupported,
  and privacy-excluded mappings.
- Keep standards support as an adapter layer rather than a canonical schema
  rewrite.

## Profiles

- `dcmi.v1`: DCMI Metadata Terms JSON using `dcterms:*` keys.
- `premis.v1`: PREMIS-like JSON for object, event, agent, and rights coverage.
- `rico.v1`: RiC-O profile JSON with deterministic URI-like node identifiers.
- `nara_preservation_readiness.v1`: NARA-style preservation readiness report.

These profiles are partial or report-only unless their profile config says
otherwise. External XML/RDF/OWL validation is not performed by this first
profile layer.

## Examples

Export a work through the DCMI profile:

```bash
python3 tools/scripts/export_standards_profile.py \
  --db path/to/canonical.sqlite \
  --profile dcmi.v1 \
  --work-id 1 \
  --output out/dcmi-work.json \
  --conformance-report out/dcmi-work.conformance.json
```

Export a captured object through the PREMIS profile:

```bash
python3 tools/scripts/export_standards_profile.py \
  --db path/to/canonical.sqlite \
  --profile premis.v1 \
  --capture-id 1 \
  --output out/premis.json
```

Export a topic graph through the RiC-O profile JSON adapter:

```bash
python3 tools/scripts/export_standards_profile.py \
  --db path/to/canonical.sqlite \
  --profile rico.v1 \
  --subject-id example_topic \
  --base-uri https://example.org/summa/ \
  --output out/rico-profile.json
```

Build a NARA-style readiness report:

```bash
python3 tools/scripts/export_standards_profile.py \
  --db path/to/canonical.sqlite \
  --profile nara_preservation_readiness.v1 \
  --subject-id example_topic \
  --output out/nara-readiness.json
```

## Privacy

The default mode is public-safe. Public exports exclude rows with public
blockers or private publication states, and they do not include raw payloads,
raw extracted text, private review notes, or local filesystem paths.

Internal/private export requires `--include-private`. Use that only for local
operator work; the output is not a public publication artifact.

## Conformance Report

Every export can write `standards-profile-conformance-report.v1` with
`--conformance-report`. The report includes:

- profile id and standard reference
- export artifact hash
- record counts and scope
- required fields satisfied or missing
- optional fields emitted
- unsupported fields
- lossy mappings
- privacy exclusions
- validation and conformance status

Use `--strict` when a failed conformance check should make the command exit
nonzero.

## Limits

- DCMI output is JSON with `dcterms:*` keys, not external RDF validation.
- PREMIS output is a deterministic PREMIS-like JSON profile, not PREMIS XML.
- RiC-O output is profile JSON, not RDF/Turtle or OWL validation.
- NARA output is a readiness report, not a NARA transfer package.
