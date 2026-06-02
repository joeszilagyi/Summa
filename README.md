# Summa

Summa is a local-first indexing toolbase for organizing source records, review state, and export-ready metadata. The repository contains generic schemas, validators, scripts, and tests. It does not include fed inputs, generated databases, runtime outputs, topic corpora, or prior-run fixtures.

## Repository Shape

- `config/` contains generic schema contracts.
- `tools/` contains validators, scripts, and reusable helpers.
- `tests/` contains synthetic tests that create any needed inputs at runtime.

Generated material belongs outside the repository. Do not commit local databases, runtime output, generated bundles, binary inputs, or staged source drops.

## Verification

Run the test suite with:

```bash
python3 -m pytest -q
```
