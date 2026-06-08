# LLM Source Text Wrapper

`default.untrusted_source_text.v1` is the checked-in wrapper template for
passing hostile or otherwise untrusted source text to an LLM.

Current repo state matters here: this tree now contains live topic-neutral
gather prompt files under `tools/prompts/general/`. Domain packs map active
prompt bundles to those files through `prompt_bundles`; `general.v1` currently
maps `gather.sources`, `gather.timeline`, `gather.people`, `gather.places`,
`gather.works`, and `gather.open_questions` to checked-in prompt templates.
Seed gather prompts prepend the shared governance header from
`tools/prompts/_shared/gather_governance_header.prompt` at render time.
`tools/scripts/run_topic_gather.py` renders those bundles, and dry-run mode
shows the exact rendered prompt without invoking an engine.

The wrapper contract is anchored in:

- the checked-in wrapper template contract
- prompt-fixture validation coverage
- runtime prompt-bundle metadata so each prompt surface declares the wrapper
  explicitly

## Required wrapper properties

- explicit begin and end delimiters
- `source_ref`, `provenance`, and `hazard_flags` metadata
- required instruction-negation guidance
- body separator before source text bytes

## Current prompt surface

- prompt directory: `tools/prompts/general/`
- shared governance header: `tools/prompts/_shared/gather_governance_header.prompt`
- domain pack mapping: `config/domain_packs/general.v1.json`
- active bundle keys: `gather.sources`, `gather.timeline`, `gather.people`,
  `gather.places`, `gather.works`, and `gather.open_questions`
- wrapper template ID: `default.untrusted_source_text.v1`
- renderer: `tools/scripts/run_topic_gather.py`
- verification: dry-run gather output exposes the rendered prompt and prompt
  audit tests verify that active prompt files are checked in and reachable

## Safety posture

- source text is untrusted data, not instruction
- hostile strings and source bytes must stay inside wrapped source blocks
- prompt rendering must not interpolate source text into instruction sections
- prompt fixtures fail when hostile source text appears outside the wrapper
- wrapper enforcement applies before any optional LLM invocation
- the validator never executes or interprets source text; it only checks
  deterministic wrapper structure
- LLM output becomes candidate material for review and ingestion, not source
  truth and not an accepted canonical fact
