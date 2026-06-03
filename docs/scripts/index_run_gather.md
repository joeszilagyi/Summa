# Index Run Gather

`tools/scripts/Index_Run_Gather.sh` is the operator-facing wrapper for
`tools/scripts/run_topic_gather.py`.

Purpose:

- resolve one subject manifest/runtime and its domain pack
- render one active gather prompt bundle into the exact prompt body that would
  be sent to the engine
- optionally invoke the configured engine only through
  `tools/scripts/lib/llm_runner.sh`
- optionally consume a validated candidate-feedback plan so the next run can
  follow the highest-yield facet or lead selected from prior canonical state
- optionally inject bounded prior canonical state for the same subject when
  explicit prior-state flags are supplied
- emit a validated workspace-local `gather-candidate-batch.v1` artifact under
  `runs/gather/<run-id>/`

Examples:

```bash
tools/scripts/Index_Run_Gather.sh \
  --subject /path/to/workspace/.indexer/subject_manifest.json \
  --workspace /path/to/workspace \
  --facet sources \
  --mode dry-run \
  --run-id reviewable-dry-run \
  --created-at 2026-06-03T12:34:56Z
```

```bash
tools/scripts/Index_Run_Gather.sh \
  --subject topic.fixture \
  --workspace /path/to/workspace \
  --facet timeline \
  --mode live \
  --engine codex
```

Safety model:

- dry-run mode never invokes Codex, Claude, API keys, or any engine binary
- any supplied source text bytes must go through the checked-in
  `default.untrusted_source_text.v1` wrapper before entering the prompt
- live mode uses the shared `llm_runner.sh` abstraction instead of calling an
  engine binary directly
- output stays in the workspace-local run directory; no canonical persistence is
  performed
- any consumed feedback plan is validated before use, and its selected leads are
  treated as next-run context only
- prior canonical context is opt-in, bounded, and labeled so proposed or
  needs-review rows remain leads rather than accepted facts

Current scope limits:

- source acquisition and fetching are out of scope for this command
- canonical accepted-candidate persistence is out of scope for this command
- LLM output is recorded only as unverified candidate artifact content, never as
  source material
