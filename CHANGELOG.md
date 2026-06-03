# Changelog

All notable repository-visible changes are recorded here. Build numbers follow
`.project_metadata`, and `CURRENT_BUILD` must always have a visible entry.
Entries describe tracked repository changes rather than private local runtime
data, and this changelog complements tests, docs, and contract files rather
than replacing them.

## [8.8.0.4]

### Added

- Added `CHANGELOG.md` as the checked-in build-history surface keyed to
  `.project_metadata`.

### Documentation

- Recorded that build history is tracked at the repository level and does not
  depend on private local runtime state.
- Preserved the current build as the first maintained changelog entry instead of
  leaving build-number history implicit.

### Validation

- Added a regression test that requires `.project_metadata` `CURRENT_BUILD` to
  appear in this changelog and keeps `PRIOR_BUILD` visible or explicitly
  acknowledged.

## [8.8.0.3]

Historical baseline referenced by `.project_metadata`. Detailed changelog
entries were not preserved in this tree before `CHANGELOG.md` was added.
