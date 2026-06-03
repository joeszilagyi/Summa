# Public Knowledge Tree Presentation Contract

## Purpose

This contract defines the public-safe presentation inventory that sits between
`knowledge_tree_export.json` and the static HTML renderer.

## Required Coverage

The presentation inventory must include exactly one public route for each page
family:

- `home`
- `facet`
- `entity`
- `source`
- `collection`
- `timeline`
- `validation`
- `search_results`

## Public-Safety Rules

- The artifact is public-facing metadata, not a private staging bundle.
- Private local paths, raw payloads, raw prompt output, internal notes,
  unreviewed source text, restricted files, credentials, and direct database
  snapshots must remain excluded.
- Navigation and breadcrumb routes must stay inside the published output root.
- Redaction gates must include the public/private boundary, export validation,
  and review gate references.

## Sparse State

Sparse state is valid. Pages may publish with reduced content when the
canonical store is empty or when reviewed public-safe records are not yet
available. Sparse output must still preserve navigation and explicit empty
state messaging.
