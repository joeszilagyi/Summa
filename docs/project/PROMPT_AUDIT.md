# Prompt Audit

This audit covers the currently active prompt files referenced by live domain
packs.

Audit posture:

- active prompts must stay topic-neutral and lead-discovery oriented
- active prompts must not ask for article prose, page copy, or presentation
  framing
- active prompts must treat wrapped source blocks as untrusted evidence

## general.v1

- `tools/prompts/general/general.sources.seed.prompt`
- `tools/prompts/general/general.sources.review.prompt`
- `tools/prompts/general/general.timeline.seed.prompt`
- `tools/prompts/general/general.timeline.review.prompt`
- `tools/prompts/general/general.people.seed.prompt`
- `tools/prompts/general/general.people.review.prompt`
- `tools/prompts/general/general.places.seed.prompt`
- `tools/prompts/general/general.places.review.prompt`
- `tools/prompts/general/general.works.seed.prompt`
- `tools/prompts/general/general.works.review.prompt`
- `tools/prompts/general/general.open_questions.seed.prompt`
- `tools/prompts/general/general.open_questions.review.prompt`

## organism.v1

- `tools/prompts/organism/taxonomy.seed.prompt`
- `tools/prompts/organism/taxonomy.review.prompt`
- `tools/prompts/organism/range.seed.prompt`
- `tools/prompts/organism/range.review.prompt`
- `tools/prompts/organism/habitat.seed.prompt`
- `tools/prompts/organism/habitat.review.prompt`
- `tools/prompts/organism/observations.seed.prompt`
- `tools/prompts/organism/observations.review.prompt`

## Current conclusion

- the active retained prompts are now checked in under `tools/prompts/`
- the wording is oriented to candidate discovery and review rather than legacy
  presentation framing
- domain-pack runtime metadata points to these files through `template_files`
