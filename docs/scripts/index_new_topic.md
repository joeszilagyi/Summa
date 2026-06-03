# Index New Topic

`tools/scripts/Index_New_Topic.sh` is the operator-facing wrapper for
`tools/scripts/bootstrap_topic_workspace.py`.

Purpose:

- create one isolated topic workspace root
- write the initial local substrate under that workspace
- register the workspace in the local topic workspace registry

Current scaffold behavior:

- creates the new workspace root and refuses to reuse an existing directory
- writes `.indexer/subject_manifest.json`
- writes `source.txt`
- creates local `state/` and `runs/` directories
- updates the topic workspace registry unless `--dry-run` is set

Important safety rules:

- registry writes are validated before commit
- tracked `config/` registry paths are refused unless the caller explicitly
  opts in with `--allow-tracked-registry`
- the tool is local-first and only touches repo-local files and the selected
  workspace root
- the tool does not take a cross-process registry lock, so run one bootstrap
  writer per registry at a time

Key inputs:

- `--topic-label`
- `--workspace-root`
- `--domain-pack`
- optional `--workspace-id`, `--subject-id`, `--display-name`,
  `--scope-statement`, `--languages`, `--aliases`,
  `--disambiguation-terms`, `--excluded-senses`,
  `--enabled-facets`, `--query-families`
- `--schedule-posture`, `--workspace-policy-class`, `--lifecycle-state`
- `--registry` or `INDEXER_TOPIC_WORKSPACE_REGISTRY`
- `--set-default`
- `--non-interactive`
- `--dry-run`
- `--format text|json`

Example:

```bash
tools/scripts/Index_New_Topic.sh --non-interactive --format json \
  --topic-label "Monarch butterflies" \
  --workspace-root "$HOME/indexer-workspaces/monarch_butterflies" \
  --domain-pack organism.v1
```

When changing workspace bootstrap layout, registry write rules, or CLI options,
keep this document, `bootstrap_topic_workspace.py`, and the related workspace
registry tests aligned.
