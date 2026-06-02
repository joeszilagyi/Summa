# Validation fixtures

This tree contains small committed fixtures for read-only validation runners.

Current layout:

```text
tests/fixtures/validators/<validator>/<scenario>/
  inputs/
  expected/
```

Rules:

- `inputs/` contains the minimal files under validation
- `expected/` contains committed golden outputs
- fixture inputs must stay tiny and reviewable in Git
- validators must write actual outputs to a temporary directory, not back into this tree

Current local replay command:

```bash
pytest tests -q
```
