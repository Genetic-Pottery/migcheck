# Plane

[Plane](https://github.com/makeplane/plane) is a Django and Postgres app.
Commit `2a29ab8d4` (January 22, 2026) adds migration `0117`, which renames
`issues.description` to `description_json`. Plane servers still running the
previous release fail on every issue insert, update, and select.

## Results

| Base → head | Migration | Result |
|---|---|---|
| `2a29ab8d4^` → `2a29ab8d4` | Renames `description` on issues, draft issues, and pages | FAIL: 17 statements in 12 transactions |
| `b5fe8a282^` → `b5fe8a282` | Adds nullable `issueview.archived_at` | PASS: 1,991 statements |

For results across 25 migrations, see the [main README](../../README.md#results-on-a-real-app).

The test suite doesn't touch draft issues or pages, so migcheck reports only
the `issues` rename.

## Run it

`requirements/` holds Plane's dependency files from `2a29ab8d4^`. They worked
for every commit from `0098` through `0122`. For a commit with different
dependencies, replace them with that commit's `apps/api/requirements/` and
rebuild.

```sh
docker build -t migcheck-plane-env examples/plane
docker run -d --rm --name migcheck-redis -p 127.0.0.1:6390:6379 redis:7-alpine

python3 -m migcheck --repo ~/Projects/plane --base 2a29ab8d4^ --head 2a29ab8d4 \
  --migrate "$PWD/examples/plane/run.sh python manage.py migrate -v0" \
  --workload "$PWD/examples/plane/run.sh pytest -q -p no:cacheprovider --create-db plane/tests"

docker rm -f migcheck-redis
```

`run.sh` points Celery at `memory://`, so tasks queue and never run.
