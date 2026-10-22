# Test results

These results come from running migcheck against real upgrades of open-source
apps. Each run used the app's own test suite as the workload.

| App | ORM | Database | Upgrades | Result |
|---|---|---|---|---|
| A Django app | Django | Postgres | 25 migrations | Caught 6 of 9 breaks |
| Gitea | xorm (Go) | Postgres, MySQL | 2 | Caught the 1 break |
| Ghost | Knex (Node) | MySQL | 2 | Caught all 3 breaks |
| Keycloak | Hibernate (Java) | Postgres, MySQL | 2 | Caught all 3 breaks |

No run raised a false alarm.

## Django

migcheck checked 25 migrations from an open-source Django app's history. It
caught 6 of the 9 breaking migrations. It missed the other 3 because the app's
tests never queried the changed tables.

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
MySQL. On MySQL, 26.6.0 nodes can't register with the cluster during a rolling
upgrade.

26.7.0 also moves each realm's display name from the `REALM_ATTRIBUTE` table to
a new column. 26.6.0's queries still run, but they read and write the old
location. migcheck checks that queries succeed, not what they return, so it
doesn't catch data-only changes like this one.
