# Export Redacted Diagnostics

`tools/scripts/export_redacted_diagnostics.py` writes a local diagnostic JSON
bundle for a Summa canonical store. The bundle is structural evidence for
debugging and audit. It is not a public knowledge-tree publication, not a
release-readiness bundle, not a standards-profile export, and not a canonical
backup.

Default output is redacted. Operators should still treat the bundle as
local/operator evidence and review it before sharing.

## Command

```bash
python3 tools/scripts/export_redacted_diagnostics.py \
  --db dbs/canonical.sqlite \
  --output-dir runs/diagnostics/export-001
```

Equivalent package command:

```bash
summa-export-redacted-diagnostics \
  --db dbs/canonical.sqlite \
  --output-dir runs/diagnostics/export-001
```

Shell wrapper:

```bash
tools/scripts/Index_Export_Redacted_Diagnostics.sh \
  --db dbs/canonical.sqlite \
  --output-dir runs/diagnostics/export-001
```

## Included Sections

The export writes a directory JSON bundle:

- `diagnostic-manifest.json`
- `canonical-summary.json`
- `graph-shape.json`
- `review-state-summary.json`
- `relationship-summary.json`
- `source-access-summary.json`
- `cycle-ledger-summary.json`
- `artifact-summary.json`
- `cycle-summary.json`
- `spool-summary.json`
- `graph-closure-summary.json`
- `redaction-report.json`
- `leak-scan-report.json`

The summaries include table row counts, review-state counts, relationship
predicate counts, graph-shape counts, source-access locator classes, cycle
status counts, artifact hashes, and graph-closure counts. They do not copy
source payload files, complete extracted text, prompt bodies, operator notes, or
local database snapshots.

## Redaction Defaults

Default behavior:

- local paths are omitted
- URLs are reduced to domains
- source text bodies are omitted
- complete extracted text is omitted
- model prompt bodies are omitted
- operator notes are omitted
- secret-looking values are redacted before writing
- content hashes and row counts are included
- the finished bundle is leak-scanned

Path redaction modes:

- `omit`
- `basename`
- `hashed`
- `hmac`

URL redaction modes:

- `omit`
- `domain_only`
- `hmac`
- `full`, only with `--internal-full-fidelity`

For deterministic HMAC redaction across exports, pass `--redaction-key`. The
key is never written into the bundle; only a short key fingerprint is recorded.

Example:

```bash
python3 tools/scripts/export_redacted_diagnostics.py \
  --db dbs/canonical.sqlite \
  --workspace runs \
  --output-dir runs/diagnostics/export-001 \
  --path-redaction hmac \
  --url-redaction domain_only \
  --redaction-key "$SUMMA_DIAGNOSTIC_REDACTION_KEY"
```

## Leak Scan

The exporter runs the shared leak scanner after writing the bundle and writes
`leak-scan-report.json`. In the default redacted mode, a leak-scan failure
blocks the export command. Internal mode is explicit and marks the manifest
`internal_private`.

## Internal Mode

`--internal-full-fidelity` is for local/private operator handling only. It can
permit full path or URL modes and marks the manifest as internal. It does not
turn the diagnostic bundle into public output.

## Boundaries

This command does not mutate the canonical store. It does not fetch sources,
invoke an LLM, apply review decisions, or publish public artifacts. It is a
read-only structural diagnostic export with privacy defaults.
