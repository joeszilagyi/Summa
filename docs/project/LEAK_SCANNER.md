# Leak Scanner

`scan_for_leaks.py` is the shared local leak scanner for generated artifacts and
hand-off bundles.

This issue closes the gap where multiple surfaces carried separate secret/path
regexes. The scanner is now the single reusable surface for:

- secret-looking tokens
- private absolute paths
- runtime-log path leaks
- raw prompt-output markers
- raw payload and full-text markers
- private-note markers

## Profiles

- `public_bundle`: strict gate for public-sharing bundles
- `support_bundle`: gate for redacted support bundles

## Allowlist posture

False-positive suppression is explicit and audited through
`leak-scan-allowlist.v1`.

Each entry must declare:

- `entry_id`
- `finding_code`
- `path_glob`
- `match_substring`
- `reason`
- `approved_by`

Suppressed findings stay visible in the machine-readable report with the
allowlist entry that matched them.
