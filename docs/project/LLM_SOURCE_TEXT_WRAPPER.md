# LLM Source Text Wrapper

`default.untrusted_source_text.v1` is the checked-in wrapper template for
passing hostile or otherwise untrusted source text to an LLM.

Current repo state matters here: this tree does not currently contain restored
live gather prompt files. The contract landed in this issue is therefore
anchored in:

- the checked-in wrapper template contract
- prompt-fixture validation coverage
- runtime prompt-bundle metadata so future prompt surfaces can declare the
  wrapper explicitly

## Required wrapper properties

- explicit begin and end delimiters
- `source_ref`, `provenance`, and `hazard_flags` metadata
- required instruction-negation guidance
- body separator before source text bytes

## Safety posture

- hostile strings must stay inside wrapped source blocks
- prompt fixtures fail when hostile source text appears outside the wrapper
- the validator never executes or interprets source text; it only checks
  deterministic wrapper structure
