<p align="center">
  <img src="assets/logo.png" alt="migcheck logo" width="280">
</p>

<p align="center">
  <a href="https://github.com/Genetic-Pottery/migcheck/actions/workflows/demo.yml"><img src="https://github.com/Genetic-Pottery/migcheck/actions/workflows/demo.yml/badge.svg?branch=main" alt="Demo"></a>
  <a href="https://github.com/Genetic-Pottery/migcheck/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Genetic-Pottery/migcheck" alt="License"></a>
  <a href="#run-it-in-github-actions"><img src="https://img.shields.io/badge/GitHub_Action-available-2088FF?logo=githubactions&logoColor=white" alt="GitHub Action"></a>
  <a href="https://github.com/Genetic-Pottery/migcheck/releases/latest"><img src="https://img.shields.io/github/v/release/Genetic-Pottery/migcheck" alt="Release"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
</p>

# migcheck

migcheck checks that a release's database migrations don't break the release
running in production.

During a rolling deploy, the old release keeps serving traffic after the new
release's migrations run. After a rollback, it runs against the new schema
indefinitely. A migration that passes review and passes the new release's tests
can still take down the old release. Migration linters check the SQL. migcheck
runs the old release's queries against the new schema and tells you which ones
fail.

For example, this is migcheck's output for the [demo](#try-the-demo), where
release `v2-rename` renames a column that `v1` uses:

```
FAIL: 4 statements from v1 break after v2-rename's migrations.

  [42703] column "name" of relation "users" does not exist
      2 statements in 1 transactions. Examples:
      INSERT INTO users (name, email) VALUES ('Ada O''Brien', 'ada@example.com') RETURNING id
      UPDATE users SET name = 'Ada Lovelace' WHERE id = '1'

  [42703] column "name" does not exist
      2 statements in 1 transactions. Examples:
      SELECT id, name, email FROM users WHERE id = '1'

v1 fails during a rolling deploy of v2-rename, and after a rollback to v1.
```

migcheck supports Postgres and MySQL. It's an early release.

## What it catches

migcheck catches any change that makes the old release's queries fail,
including the following:

- Renamed or dropped columns and tables.
- New `NOT NULL` columns without a database default. The old release's inserts
  don't set them.
- New constraints that the old release's writes violate.
- Changed column types that the old release's values don't fit.

The `NOT NULL` case is easy to miss in Django. When a migration adds a field
with a `default`, Django fills existing rows with it and then drops the default
from the database. The migration looks safe, but the old release's inserts fail.

In a test against 25 migrations from an open-source Django app's history,
migcheck caught 6 of the 9 breaking migrations, with no false alarms. It missed
the other 3 because the app's tests never queried the changed tables.

## Install

migcheck needs Docker, git, and Python 3.10 or later. It has no Python
dependencies and doesn't need a database client on the host.

```sh
pipx install git+https://github.com/Genetic-Pottery/migcheck@v0.1.0
```

## Use it

Run migcheck from your app's repository:

```sh
migcheck --base <deployed-ref> --head <new-ref> \
  --migrate "<command that applies migrations>" \
  --workload "<command that runs your test suite>"
```

- `--base` is the release running in production, such as its tag.
- `--head` is the release you're about to deploy.
- `--migrate` and `--workload` are shell commands. migcheck runs them in a
  checkout of each ref, with `DATABASE_URL` pointing at a throwaway database.
- `--db` is the database your app uses: `postgres`, the default, or `mysql`.

Your app must read its database connection from `DATABASE_URL` in the test
environment. If it reads other variables, wrap the commands in a script that
sets them.

`DATABASE_URL` starts with `postgresql://` or `mysql://`. If your framework
expects another scheme, such as `mysql2://` for Rails, rewrite it in the
command:

```sh
migcheck --db mysql --base v5.2.0 --head main \
  --migrate 'DATABASE_URL=mysql2${DATABASE_URL#mysql} bin/rails db:migrate' \
  --workload 'DATABASE_URL=mysql2${DATABASE_URL#mysql} bin/rails test'
```

For example, for a Django app:

```sh
migcheck --base v1.4.0 --head main \
  --migrate "python manage.py migrate" \
  --workload "pytest"
```

migcheck exits with one of the following codes:

| Code | Meaning |
|---|---|
| `0` | Every query the old release made still works. |
| `1` | At least one query breaks. |
| `2` | migcheck couldn't compare, for example because every query failed on the old schema too. |

### Options

| Flag | Description |
|---|---|
| `--base` | Ref of the release running in production. Required. |
| `--head` | Ref of the release being deployed. Required. |
| `--migrate` | Shell command that applies migrations to `$DATABASE_URL`. Required. |
| `--workload` | Shell command that exercises the app against `$DATABASE_URL`, such as its test suite. Required. |
| `--db` | Database the app uses: `postgres` or `mysql`. Default: `postgres` |
| `--repo` | Git repository to check. Default: `.` |
| `--image` | Database Docker image. Use the major version you run in production. Default: `postgres:17-alpine` or `mysql:8.4` |
| `--head-name` | Name to show for `--head` in the result, such as `this pull request`. |
| `--markdown` | Also write the result as Markdown to this path, for a pull request comment. |

## See it in action

The [migcheck-example](https://github.com/Genetic-Pottery/migcheck-example)
repository runs the action on its pull requests. Each pull request adds a
migration, and migcheck comments with the result:

- [Rename a column](https://github.com/Genetic-Pottery/migcheck-example/pull/1)
  fails, because the production release still uses the old name.
- [Add a `NOT NULL` column and drop its default](https://github.com/Genetic-Pottery/migcheck-example/pull/2)
  fails, because the production release's inserts don't set it.
- [Add a nullable column](https://github.com/Genetic-Pottery/migcheck-example/pull/3)
  passes.

## Run it in GitHub Actions

The migcheck action runs the check on each pull request and posts the result
as a comment. It updates the same comment when the pull request changes.

```yaml
name: migcheck
on:
  pull_request:
    paths: ["migrations/**"]

permissions:
  contents: read
  pull-requests: write

jobs:
  migcheck:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      # Install your app's dependencies here.
      - uses: Genetic-Pottery/migcheck@v0
        with:
          migrate: python manage.py migrate
          workload: pytest
```

The checkout step must use `fetch-depth: 0`, so that migcheck can read the
base release and its tags.

By default, the base release is the newest tag reachable from the pull
request's base branch. If you tag releases differently, set `base` to the ref
running in production.

The action takes the following inputs:

| Input | Description |
|---|---|
| `migrate` | Shell command that applies migrations to `$DATABASE_URL`. Required. |
| `workload` | Shell command that exercises the app against `$DATABASE_URL`. Required. |
| `base` | Ref of the release running in production. Default: the newest tag reachable from the base branch. |
| `head` | Ref of the release being deployed. Default: `HEAD` |
| `db` | `postgres` or `mysql`. Default: `postgres` |
| `image` | Database Docker image. Default: `postgres:17-alpine` or `mysql:8.4` |
| `working-directory` | Directory of the repository to check. Default: `.` |
| `comment` | Whether to post a pull request comment. Default: `true` |
| `comment-key` | Keeps comments separate when a workflow runs migcheck more than once. Default: `default` |

The action fails the job when a query breaks or migcheck can't compare, and
sets the `result` output to `pass`, `fail`, or `error`. It also writes the
result to the job summary.

Pull requests from forks get a read-only token, so the action can't comment
on them. The result still appears in the job summary.

## Try the demo

The demo builds a small repository with one base release and three candidate
releases. Two of the candidates break the base release.

```sh
python3 -m venv /tmp/mc-venv
/tmp/mc-venv/bin/pip install "psycopg[binary]"
examples/make-demo.sh /tmp/mc-demo

migcheck --repo /tmp/mc-demo --base v1 --head v2-rename \
  --migrate "/tmp/mc-venv/bin/python migrate.py" \
  --workload "/tmp/mc-venv/bin/python test_app.py"
```

| Tag | Migration | Result |
|---|---|---|
| `v2-expand` | Adds `full_name` next to `name` | `PASS` |
| `v2-rename` | Renames `name` to `full_name` | `FAIL` |
| `v2-notnull` | Adds `plan text NOT NULL` with no default | `FAIL` |

## How it works

1. migcheck checks out the base and head refs into temporary git worktrees.
2. It starts a throwaway database container with statement logging on.
3. It applies the base release's migrations, runs the base release's workload,
   and records every query the workload makes.
4. It makes two copies of the base schema and applies the head release's
   migrations to one of them.
5. It replays the recorded queries against both copies, with bind parameters
   inlined.
6. It reports each query that succeeds on the old schema and fails on the new
   one.

Replaying against both copies filters out queries that fail for reasons
unrelated to the migration, such as missing seed data.

Replay follows the workload's transactions. If the workload rolled back a
transaction, as test runners do after each test, replay rolls back its effects
too. The report lists the first failure in each transaction as a root cause and
groups later integrity errors in that transaction separately.

migcheck also accounts for how test runners use the database:

- It records queries from every database the workload touches, because test
  runners such as Django's create their own.
- It drops DDL and transaction-control statements, which come from the test
  runner rather than the app.
- It replays `TRUNCATE` by emptying whichever listed tables exist, so cleanup
  between tests doesn't depend on the schema.
- On Postgres, it records the primary key of each row the workload inserts
  and gives replayed inserts the same keys, so later statements that
  reference those rows find them. On MySQL, replay starts from the same
  auto-increment counters as the workload, so inserts get the same keys when
  the workload runs one test at a time.

Everything runs locally. The database container listens only on `127.0.0.1`,
and migcheck removes the container and worktrees when it finishes.

## Limitations

- migcheck checks only the queries the workload makes. If your tests never
  touch a table, migcheck can't see it break.
- migcheck applies migrations to an empty database. A migration that fails or
  locks for a long time on a large production table can pass here.
- Replay runs one statement at a time. Failures that depend on concurrency
  don't show up.
- migcheck checks for errors, not wrong results. A query that returns
  different rows on the new schema passes.
- On Postgres, replay ignores `SAVEPOINT` and `ROLLBACK TO SAVEPOINT`.
- On MySQL, a workload that runs tests in parallel can leave replayed rows
  with different keys than the workload saw. Statements that reference those
  keys then fail on both schemas, and migcheck skips them.
- migcheck supports MySQL 8.0 and later. It doesn't support MariaDB, whose
  query log has a different format.
- Postgres words some errors differently for reads and writes, so one missing
  column can appear as two root causes.

## License

Apache 2.0. See [LICENSE](LICENSE).
