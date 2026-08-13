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
`leak-scan-allowlist.v2`.

Each entry must declare:

- `entry_id`
- `finding_fingerprint`: the exact `sha256:` fingerprint emitted on the finding
- `reason`
- `approved_by`: a bounded reviewer identity token
- `expires_at`: a timezone-qualified expiration timestamp

Each finding includes a `finding_fingerprint` derived from its path, marker,
location, excerpt, and a hash of the surrounding scanned context. The context
hash is emitted instead of the source text so the scanner can bind an approval
without re-disclosing a secret.

Allowlist entries match only that exact finding fingerprint; broad path or
substring suppressions are rejected. Expired entries no longer suppress
findings. Suppressed findings stay visible in the machine-readable report with
only the allowlist entry ID that matched them by default. The report's
`allowlist_audit` also emits only `entry_ids` by default;
`--debug-allowlist-audit` is a private/debug-only opt-in for including full
allowlist entries and their audit metadata.
