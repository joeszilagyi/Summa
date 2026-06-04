# Standards Profiles

Summa's canonical SQLite graph is an internal model. It is not renamed to match
external metadata, preservation, or linked-data standards. Standards support is
provided through explicit profile adapters.

The architecture is:

```text
Summa canonical model
-> standards profile config
-> standards export builder
-> conformance report
```

## First Profiles

Profile configs live in `config/standards_profiles/` and validate against
`config/standards_profiles/standards_profile.schema.json`.

- `dcmi.v1` maps selected `work`, `source_access`, `source_claim`,
  `source_relationship`, and `provenance_event` fields into deterministic JSON
  using `dcterms:*` keys.
- `premis.v1` maps capture and provenance rows into PREMIS-like object, event,
  agent, and rights sections.
- `rico.v1` maps works, authorities, relationships, and events into RiC-O
  profile JSON with stable URI-like identifiers under an operator-supplied base
  URI.
- `nara_preservation_readiness.v1` reports preservation readiness checks such
  as fixity, recorded actions, timestamps, payload policy, and transfer-package
  gaps.

## Conformance Is Explicit

Every profile declares:

- required mappings
- optional mappings
- controlled vocabulary posture
- cardinality expectations
- identifier policy
- public/private policy
- unsupported fields
- lossy mappings
- validation rules
- known limitations

Every export can emit `standards-profile-conformance-report.v1`. The report
states what was satisfied, missing, lossy, unsupported, or privacy-excluded.
Lossy mappings are not hidden, and missing required fields are not invented.

## Public And Private Boundaries

Standards exports are public-safe by default. Public exports exclude rows with
public blockers or private publication states and do not include raw payloads,
raw extracted text, private review notes, local paths, prompts, or secrets.

Internal/private exports require an explicit `--include-private` flag on the
export command.

## What This Does Not Claim

F33 does not claim full DCMI, PREMIS, RiC-O, NARA, EAD, METS, MODS, or any
other standards compliance. It adds testable adapters for the first four
profiles. Full external validation, XML/RDF serializations, transfer-package
assembly, format-registry validation, and additional profiles remain future
work unless explicitly implemented and tested.

## Adding A Future Profile

To add another standard profile:

1. Add a profile config under `config/standards_profiles/`.
2. Validate it with the shared profile schema.
3. Add exporter logic that reads canonical rows without mutating the store.
4. Emit a conformance report.
5. Add fixture tests for required, missing, lossy, unsupported, privacy, and
   deterministic behavior.
6. Update documentation without renaming internal canonical fields.
