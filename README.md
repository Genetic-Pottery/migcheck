# migcheck

migcheck checks that a release's database migrations don't break the release
running in production.

During a rolling deploy, the previous release keeps serving traffic against the
new schema. After a rollback, it runs against the new schema indefinitely.
Linters such as squawk and strong_migrations check the migration SQL. migcheck
checks the old release's queries against the new schema.

Prototype. Postgres only.

## How it works

1. migcheck checks out the base and head refs into temporary git worktrees.
2. It starts a throwaway Postgres container with statement logging on.
3. It applies the base release's migrations, then runs the base release's
   workload, for example its test suite, and records every query it makes.
4. It makes two copies of the base schema and applies the head release's
   migrations to one of them.
5. It replays the recorded queries against both copies, in order, with
   recorded bind parameters inlined.
6. It reports each query that succeeds on the old schema and fails on the new
   one, and exits with code `1`.

Replaying against both copies filters out queries that fail for reasons
unrelated to the migration, such as missing seed data.

Replay follows the workload's transactions. If the workload rolled back a
transaction, as test runners do after each test, replay rolls back its effects
too. The report lists the first failure in each transaction as a root cause,
and groups later integrity errors in that transaction separately.

migcheck also accounts for test runners:

- It records queries from every database the workload touches, because test
  runners such as Django's create their own.
- It drops DDL and transaction-control statements, which come from the test
  runner rather than the app.
- It replays `TRUNCATE` by emptying whichever listed tables exist, with
  `CASCADE`, so cleanup between tests doesn't depend on the schema.

## Results on a real app

[Plane](https://github.com/makeplane/plane) is a Django and Postgres app.
migcheck ran on 25 of its migrations, `0098` through `0122`, using Plane's
test suite as the workload:

| Result | Migrations |
|---|---|
| Real break, caught | `0104`, `0105`, `0113`, `0114`, `0116`, `0117` |
| Correct pass | 15 migrations |
| False alarm | None |
| Real break, missed | `0098`, `0100`, `0118`. Plane's tests never touch the changed tables. |
| Couldn't run | `0099`. Plane has two `0098` migrations at that commit. |

Four of the six caught breaks come from one Django behavior. When a migration
adds a column with a `default`, Django uses the default to fill existing rows
and then drops it from the database. The column ends up `NOT NULL` with no
database default, so the old release's inserts fail.

For setup details, see [examples/plane](examples/plane/README.md).

## Requirements

- Docker
- Python 3.10 or later

migcheck runs `psql` inside the container, so the host doesn't need a
Postgres client.

## Run the demo

The demo repo has one base release and three candidates:

| Tag | Migration | Expected result |
|---|---|---|
| `v1` | Creates `users` with a `name` column | Base |
| `v2-expand` | Adds `full_name` next to `name` | Pass |
| `v2-rename` | Renames `name` to `full_name` | Fail |
| `v2-notnull` | Adds `plan text NOT NULL` with no default | Fail |

The demo app uses psycopg. To run it, do the following:

```sh
python3 -m venv /tmp/mc-venv
/tmp/mc-venv/bin/pip install "psycopg[binary]"
examples/make-demo.sh /tmp/mc-demo

python3 -m migcheck --repo /tmp/mc-demo --base v1 --head v2-rename \
  --migrate "/tmp/mc-venv/bin/python migrate.py" \
  --workload "/tmp/mc-venv/bin/python test_app.py"
```

Output:

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

## Options

| Flag | Description |
|---|---|
| `--repo` | Git repository to check. Default: `.` |
| `--base` | Ref of the release running in production. |
| `--head` | Ref of the release being deployed. |
| `--migrate` | Shell command that applies migrations to `$DATABASE_URL`. migcheck runs it in each worktree. |
| `--workload` | Shell command that exercises the app against `$DATABASE_URL`. migcheck runs it in the base worktree. |
| `--image` | Postgres Docker image. Default: `postgres:17-alpine` |

## Limitations

- migcheck checks only the queries the workload makes. Coverage is as good as
  the base release's test suite.
- Replay runs one statement at a time. Failures that depend on concurrency,
  such as lock timeouts during a long migration, don't show up.
- Replay ignores `SAVEPOINT` and `ROLLBACK TO SAVEPOINT`, so effects that the
  workload rolled back to a savepoint stay until the end of the transaction.
- Postgres words some errors differently for reads and writes, so one missing
  column can appear as two root causes.
- migcheck applies migrations to an empty database. A migration that fails on
  a large or dirty production table can pass here.
- migcheck checks for errors, not wrong results. A query that returns
  different rows on the new schema passes.
- Multi-statement query strings and `$N` text inside dollar-quoted strings
  aren't handled.
