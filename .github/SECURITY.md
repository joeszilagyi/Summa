# Security Policy

Report security issues privately before opening a public issue. If GitHub
private vulnerability reporting is enabled for this repository, use that path.
Otherwise, contact the repository owner directly through GitHub and provide a
minimal description of the affected surface.

Do not include secrets, tokens, private `.env` values, raw private payloads,
full extracted text, or sensitive local paths in public issues, pull requests,
logs, release notes, or screenshots.

## Repository-Specific Boundaries

- This is a local-first indexing workspace. Local runtime logs, execution manifests,
  backups, journals, locks, caches, and assistant session files are not public
  release artifacts.
- Raw PDFs, HTML, WARC files, screenshots, audio/video, OCR text, and full
  extracted text are transient processing inputs by default.
- Durable source/work metadata, hashes, short located highlights, manifests,
  validators, and reviewed reports may be tracked when the storage/export
  policy allows them.
- Prompt-injection, malicious source content, unsafe shell behavior, path
  traversal, secret exposure, and public/private export-boundary bugs should be
  treated as security-relevant.

For deeper project context, see
`docs/project/SECURITY_AND_PROMPT_INJECTION_THREAT_MODEL.md`,
`docs/project/PUBLIC_PRIVATE_EXPORT_BOUNDARY.md`, and
`docs/project/STORAGE_AND_PUBLICATION_POLICY.md`.
