# Domain Packs

Checked-in pack count: 2.

The checked-in domain packs live under `config/domain_packs/`. They define the
subject kinds, enabled gather facets, query families, prompt bundles, and
source-text wrapper template IDs that the current local gather runtime can use.

This index mirrors the current JSON configs. `config/domain_packs/*.json`
remains the source of truth, and `tests/test_domain_pack_index.py` checks that
the pack count and statuses documented here do not drift.

## `general.v1`

- Display name: `General topic starter domain pack`
- Status: `runtime`
- Subject kinds: `topic.general`, `topic.person_or_group`, `topic.place_scope`, `topic.work_or_media`, `topic.event_or_thread`
- Enabled facets (6): `sources`, `timeline`, `people`, `places`, `works`, `open_questions`
- Query families (8): `web_search`, `book_catalogs`, `newspaper_archives`, `film_and_video_records`, `broadcast_records`, `archive_catalogs`, `local_document_ingest`, `reference_chaining`
- Prompt bundles: 6
- Wrapper template IDs: `default.untrusted_source_text.v1`
- Runtime readiness: every enabled facet is exercised through `tools/scripts/run_topic_gather.py` in dry-run mode, the live bridge path is covered in `tests/test_run_topic_gather.py`, and F19/F20 tests cover canonical ingest plus bounded cycle-two prior-state reuse for a place-shaped fixture after deterministic fixture ingestion
- Topic-neutral: yes
- README flagship example: currently routes to `general.v1`, but is not yet a fixture-proven safe first-cycle coverage example for place-dominant recreation subjects such as `trout fly fishing in Montana`
- Known limitations: this is still the neutral starter pack, not a specialized `person`, `place`, `work`, or `event` pack; first-cycle place/recreation coverage remains unvalidated until a deterministic gather fixture proves usable place/work/open-lead output from `general.v1` itself

## `organism.v1`

- Display name: `Organism example domain pack`
- Status: `example`
- Subject kinds: `organism.taxon`, `organism.common_name_scope`
- Enabled facets (4): `taxonomy`, `range`, `habitat`, `observations`
- Query families (4): `taxonomy_references`, `field_guides`, `regional_observations`, `museum_or_specimen_records`
- Prompt bundles: 4
- Wrapper template IDs: `default.untrusted_source_text.v1`
- Runtime readiness: prompt bundles resolve, prompt files are checked in, and dry-run gather reachability is covered, but the pack is retained as illustrative scaffolding rather than the default broad-topic runtime path
- Topic-neutral: yes
- README flagship example: no; broad generic topics still map to `general.v1`
- Known limitations: narrow example coverage only; it exists to show that topic-neutral does not require one ontology

## Planned specialization

The current tree does not yet ship separate `person`, `place`, `work`, or
`event` packs. Those remain future specialization work. They are not checked in
yet because the current prompt surface would make them renamed copies of
`general.v1` rather than meaningfully narrower runtime packs. If future
deterministic fixture validation shows that place-dominant topics need a
different first-cycle shape, `place.v1` is the next specialization to scope.
