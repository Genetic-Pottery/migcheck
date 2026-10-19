# Changelog

## v0.1.1 - 2026-10-20

### Changed

- On pull requests, the action's comment refers to "this pull request"
  instead of `HEAD`.

### Added

- `--head-name` sets the name the result uses for `--head`.

## v0.1.0 - 2026-10-19

The first release.

### Added

- Check that a release's migrations don't break the release before it, by
  replaying the old release's recorded queries against both schemas.
- Postgres support, with `--db postgres`.
- MySQL 8.0 and later support, with `--db mysql`.
- Replay that follows the workload's transactions, so rolled-back test data
  doesn't leak into later statements.
- Root causes reported separately from the errors that follow from them.
- On Postgres, primary keys that the database generates are pinned to the
  values the workload got.
- `--markdown` writes the result as Markdown.
- A GitHub Action that runs the check on pull requests and posts the result
  as a comment.
