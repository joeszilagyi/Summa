# Validators

The Python entrypoints in this directory are checked-in local validators and
read-only aggregators for Summa contracts, manifests, view models, and release
gates.

Common behavior:

- validate one local file, directory, or artifact bundle at a time
- emit machine-readable reports through the shared `common.py` helpers
- support `--report-json` and `--report-text` where the surface is a standard
  validator report
- avoid mutation of source inputs

Representative surfaces in this directory include:

- schema and manifest validation, such as topic workspace registries and
  knowledge-tree build manifests
- public/private release gates
- local search projection and result validation
- source-adapter, handoff, and evidence contract validation
- operational aggregators such as release readiness

Two modules explicitly point at this README as their paired maintenance note:

- `validate_topic_workspace_registry.py`
- `validate_knowledge_tree_build_manifest.py`

Keep this README current when the shared validator posture changes in a way
that affects how operators should understand or run the tools here. Tool-
specific behavior still belongs in each module docstring, schema, and tests.
