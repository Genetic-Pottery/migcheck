# Test results

These results come from running migcheck against real upgrades of open-source
apps. Each run used the app's own test suite as the workload. The Akkoma results
reflect migcheck 0.2.1 with the comment fix listed under Unreleased in the
changelog. The n8n and Lemmy results reflect migcheck 0.2.1, and the others
reflect migcheck 0.2.0. When a release changes how migcheck records or replays
queries, the affected results are rerun.

| App | ORM | Database | Upgrades | Result |
|---|---|---|---|---|
| Plane | Django | Postgres | 25 migrations | Caught 6 of 9 breaks |
| Gitea | xorm (Go) | Postgres, MySQL | 2 | Caught the 1 break |
| Ghost | Knex (Node) | MySQL | 2 | Caught all 3 breaks |
| Keycloak | Hibernate (Java) | Postgres, MySQL | 2 | Caught all 3 breaks |
| Mastodon | Active Record (Rails) | Postgres | 2 | Passed both, as designed |
| n8n | TypeORM (Node) | Postgres | 2 | Caught the 1 break, with 1 false alarm |
| Lemmy | Diesel (Rust) | Postgres | 2 | Caught the 1 break |
| Akkoma | Ecto (Elixir) | Postgres | 2 | Caught all 4 breaks |

The n8n run raised the only false alarm left after the fix described in the
Mastodon section. For details, see the n8n section.

## Plane

migcheck checked 25 migrations from Plane's history. It caught 6 of the 9
breaking migrations. It missed the other 3 because Plane's tests never queried
the changed tables.

## Gitea

migcheck replayed Gitea's API tests for two upgrades, v1.23.8 to v1.24.7 and
v1.24.7 to v1.25.5, on both Postgres and MySQL. The runs replayed about a
million statements in total.

migcheck caught the one breaking migration on both databases. That migration
drops a column that v1.23.8 queries.

## Ghost

migcheck replayed Ghost's API tests on MySQL for two upgrades, v6.69.0 to
v6.70.0 and v6.71.0 to v6.72.0. Each upgrade ran once with one test worker and
once with four. Each run replayed 180,000 to 190,000 statements.

migcheck caught all three breaks in v6.70.0: two dropped tables and a dropped
column. It passed v6.72.0. The four-worker runs gave the same results as the
one-worker runs.

## Keycloak

migcheck replayed Keycloak's test suite for two upgrades, 26.6.0 to 26.7.0 and
26.7.0 to 26.8.0, on both Postgres and MySQL. The four runs replayed about 14
million statements in total. The largest run replayed 4.8 million.

| Upgrade | Postgres | MySQL |
|---|---|---|
| 26.6.0 to 26.7.0 | Pass | Fail |
| 26.7.0 to 26.8.0 | Fail | Fail |

On both databases, migcheck caught the two breaks in 26.8.0:

- 26.8.0 drops `IDENTITY_PROVIDER.ORGANIZATION_ID`, which 26.7.0 reads and
  writes.
- 26.8.0 makes `ORG_DOMAIN.REALM_ID` `NOT NULL`, and 26.7.0's inserts don't set
  it.

The database changed the verdict on 26.7.0. That release adds two columns to
the `JGROUPS_PING` table, and 26.6.0 inserts into it with a `VALUES` list and no
column names. Postgres fills the missing columns with `NULL`, so migcheck passed
26.7.0 on Postgres. MySQL rejects the insert, so migcheck failed 26.7.0 on
MySQL. On MySQL, a 26.6.0 node can't register with the cluster once the
database has the 26.7.0 schema.

26.7.0 also moves each realm's display name from the `REALM_ATTRIBUTE` table to
a new column. 26.6.0's queries still run, but they read and write the old
location. migcheck checks that queries succeed, not what they return, so it
doesn't catch data-only changes like this one.

Keycloak supports rolling upgrades only between patch releases. Between minor
releases, such as these, it requires stopping every node before the upgrade.
So these breaks show what migcheck detects, not bugs in Keycloak. The two patch
releases in these lines that change the schema add a SQL Server–only index and
backfill data, which migcheck doesn't check.

## Mastodon

Mastodon splits its migrations in two. It runs the pre-deployment
migrations, rolls out the new release, and runs the post-deployment
migrations after the old release stops. The old release must work with the
first set but not the second.

migcheck replayed Mastodon's specs for two upgrades, v4.5.19 to v4.6.9 and
v4.6.9 to v4.7.3, with only the pre-deployment migrations applied. It also
replayed v4.6.9 to v4.7.3 with all migrations applied.

| Upgrade | Migrations | Statements | Result |
|---|---|---|---|
| v4.5.19 to v4.6.9 | Pre-deployment | 215,330 | Pass |
| v4.6.9 to v4.7.3 | Pre-deployment | 237,781 | Pass |
| v4.6.9 to v4.7.3 | All | 237,583 | Fail |

Both pre-deployment runs passed, as Mastodon's design intends. The run with
all migrations failed on two post-deployment changes:

- v4.7.3 adds a unique index on `accounts.uri`. v4.6.9 inserts each local
  account with an empty `uri`, so the second insert fails.
- v4.7.3 turns `account_summaries` and `global_follow_recommendations` from
  materialized views into tables. v4.6.9 refreshes them as views.

These breaks are real, but Mastodon's deploy process avoids them, because
v4.6.9 has stopped by the time those migrations run.

The run with all migrations also exposed a false alarm in migcheck. A
post-deployment migration inserts rows into `keypairs`, and those rows took
keys that v4.6.9's replayed inserts also used. migcheck now gives a replayed
insert a new key when a migration already took its key.

The workload skipped Mastodon's browser specs and search specs. 14 of the
remaining 7,121 specs failed, because the Redis configuration specs expect a
default Redis URL. Those specs don't query the database.

## n8n

migcheck replayed n8n's CLI integration tests on Postgres for two upgrades,
2.40.6 to 2.41.7 and 2.44.6 to 2.45.3.

| Upgrade | Statements | Result |
|---|---|---|
| 2.40.6 to 2.41.7 | 486,841 | Fail |
| 2.44.6 to 2.45.3 | 602,226 | Pass |

migcheck caught the one break in 2.41.7. That release adds
`ai_preference.source` as `NOT NULL` with no default, on purpose, so that every
write must name its source. 2.40.6's 37 inserts into `ai_preference` don't set
it. The 2.45.3 migrations add columns with defaults, drop a `NOT NULL`, and add
a check that 2.44.6's rows meet, so migcheck passed it.

The 2.41.7 run also raised a false alarm. A 2.41.7 migration inserts three rows
into the `scope` table. At startup, 2.40.6 reads the `scope` table and inserts
the scopes that are missing. Replay runs the recorded insert, which includes
the three scopes the migration added, so it fails on the primary key. A real
2.40.6 server reads the table first and skips those scopes. The insert also
caused 227 later failures in the same transaction, which the report lists
separately.

migcheck still fails this insert, because an old release that inserts without
checking first breaks in production. The report labels it: it says that a row
from the head migrations holds the key, and that the insert isn't a break if
the old release checks for the row first.

Each run passed about 7,000 to 8,600 tests. 4 tests failed in each run, at
least 2 of them because `openssl` wasn't installed.

## Lemmy

migcheck replayed Lemmy's Rust tests on Postgres for two upgrades,
1.0.0-beta.0 to 1.0.0-beta.1 and 1.0.0-beta.1 to 1.0.0-beta.2.

| Upgrade | Statements | Result |
|---|---|---|
| 1.0.0-beta.0 to 1.0.0-beta.1 | 20,673 | Fail |
| 1.0.0-beta.1 to 1.0.0-beta.2 | 21,562 | Pass |

migcheck caught the one break in 1.0.0-beta.1. That release renames
`local_user.hide_media` to `hide_posts_with_media`. Diesel names every column
in its queries, so 445 of 1.0.0-beta.0's statements break: inserts and
updates of `local_user`, and the reads that join it, such as the post and
comment views.

1.0.0-beta.2 renames a column too, but the column is new in that release, so
1.0.0-beta.1 never uses it. migcheck passed it.

Lemmy sets `lemmy.protocol_and_hostname` as an option in its connection
string, and its triggers read the setting. The Postgres log doesn't record
connection options, so in the first runs, replay ran without the setting. 1,685
statements failed on both schemas and weren't checked. The runs in the table
set the option on the `postgres` role, and 60 statements failed on both
schemas. The verdicts didn't change.

Each run passed 270 to 280 tests. 2 tests failed in each run, because they
fetch pages from the internet.

## Akkoma

migcheck replayed Akkoma's tests on Postgres for two upgrades, v3.18.1 to
v3.19.0 and v3.19.0 to v3.20.1. Akkoma is a fork of Pleroma.

| Upgrade | Statements | Result |
|---|---|---|
| v3.18.1 to v3.19.0 | 76,342 | Fail |
| v3.19.0 to v3.20.1 | 74,256 | Pass |

migcheck caught all four breaks in v3.19.0, in 21,773 statements:

- v3.19.0 drops `users.skip_thread_containment`. Ecto names every field in
  its queries, so most of v3.18.1's reads and writes of `users` break.
- v3.19.0 drops `lists.ap_id`, which v3.18.1 reads and writes.
- v3.19.0 drops the `activity_visibility` and `thread_visibility` functions,
  which v3.18.1's timeline queries call.

v3.20.1's migrations add a column, replace indexes, and backfill data, so
migcheck passed it. v3.20.1 also replaces the unique index on
`users.nickname` with one under another name. v3.19.0 maps the old index's
name to a "nickname taken" error, so a duplicate nickname raises an exception
instead. The insert fails on both schemas, so migcheck doesn't catch this.
The tests didn't insert a duplicate nickname, so the run didn't show it.

Ecto's test sandbox runs each test in a transaction that it rolls back, and
turns the test's own transactions into savepoints. Replay rolls back each
test's transaction too, but it ignores savepoints, so it doesn't undo a
rollback to a savepoint. Only 18 statements failed on both schemas, so this
didn't cost the run any data that later queries needed.

This test found a bug in migcheck. One of Akkoma's queries has a comment with
"won't" in it, and migcheck read the apostrophe as the start of a string. It
left the query's later parameters unfilled, so the query failed on both
schemas. migcheck now skips comments.

Each run passed about 4,150 tests, and 1 test failed in each. The first run of
v3.18.1's tests under migcheck had 96 failures, which didn't happen again
with the same random seed.
