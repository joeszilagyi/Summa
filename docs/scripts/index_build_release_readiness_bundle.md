# Index Build Release Readiness Bundle

`tools/scripts/Index_Build_Release_Readiness_Bundle.sh` is the operator wrapper
for `tools/scripts/build_release_readiness_bundle.py`.

Purpose:

- assemble the upstream JSON reports required by
  `tools/validators/validate_release_readiness.py`
- stage those reports under the exact filenames the validator expects
- run the existing release-readiness validator against the staged directory
- write a final `release-readiness-report.json`
- write a `release-readiness-bundle-manifest.json` with sources, staged paths,
  hashes, commands, statuses, warnings, and errors
- optionally stage or generate `graph-closure-report.json` as a strict
  release-readiness signal

Required staged filenames:

- `doctor-report.json`
- `knowledge-tree-export-validator-report.json`
- `static-output-validator-report.json`
- `local-search-projection-validator-report.json`
- `leak-scan-report.json`

Collect mode copies prebuilt reports:

```bash
tools/scripts/Index_Build_Release_Readiness_Bundle.sh \
  --mode collect \
  --output-dir runs/release-readiness/run-001 \
  --doctor-report reports/doctor.json \
  --knowledge-tree-export-report reports/export-validator.json \
  --static-output-report reports/static-validator.json \
  --local-search-projection-report reports/search-validator.json \
  --leak-scan-report reports/leak-scan.json
```

Run mode generates every upstream report from supplied inputs:

```bash
tools/scripts/Index_Build_Release_Readiness_Bundle.sh \
  --mode run \
  --output-dir runs/release-readiness/run-002 \
  --repo-root . \
  --canonical-db canonical.sqlite \
  --knowledge-tree-export public/knowledge_tree_export.json \
  --static-output-manifest public/static/build-manifest.json \
  --local-search-projection public/search/local-search-projection.json \
  --leak-scan-target public/static
```

Mixed mode collects explicit report paths and generates the missing reports from
the corresponding upstream inputs. Explicit report paths win.

Strictness:

- default behavior returns nonzero when the final release-readiness report is
  `block`
- `--report-only` still records the blocked status but exits zero
- `--graph-closure-strict` turns true graph-closure orphan errors into a
  release-readiness block when a graph-closure report is supplied or generated
- missing required reports, invalid JSON, upstream command setup failures, and
  overwrite refusal always fail

Output layout:

- `<output-dir>/doctor-report.json`
- `<output-dir>/knowledge-tree-export-validator-report.json`
- `<output-dir>/static-output-validator-report.json`
- `<output-dir>/local-search-projection-validator-report.json`
- `<output-dir>/leak-scan-report.json`
- `<output-dir>/graph-closure-report.json` when `--graph-closure-report` or
  `--graph-closure-db` is supplied
- `<output-dir>/release-readiness-report.json`
- `<output-dir>/release-readiness-report.txt`
- `<output-dir>/release-readiness-bundle-manifest.json`

Safety model:

- no network access
- no LLM calls
- no canonical data mutation
- no publishing step
- no report is silently skipped
- generated files are written only under the requested output directory
- existing non-empty output directories are refused unless `--force` is supplied

Relationship to the validator:

`validate_release_readiness.py` remains the aggregator and validation authority.
The bundle builder is the producer that runs or collects upstream reports, stages
them under the expected names, and invokes that existing validator.
