#!/bin/sh
# Applies the Auth and Storage servers' migrations and then
# supabase/migrations to $DATABASE_URL, as `supabase start` does.
set -eu
# The image you pass to migcheck's --image.
image=supabase/postgres:17.11.0.004
url_as() { echo "$DATABASE_URL" | sed "s|//postgres:|//$1:|"; }
password=$(echo "$DATABASE_URL" | sed 's|^[^:]*://[^:]*:\([^@]*\)@.*|\1|')

# The Auth and Storage servers and PostgREST log in as roles that only a
# superuser can change. The image gives the superuser, supabase_admin, the
# postgres password.
echo "ALTER ROLE supabase_auth_admin PASSWORD :'pw'; ALTER ROLE supabase_storage_admin PASSWORD :'pw';
  ALTER ROLE authenticator PASSWORD :'pw';" |
  docker run -i --rm --network host "$image" psql -q -v pw="$password" "$(url_as supabase_admin)"

docker run --rm --network host \
  -e GOTRUE_DB_DRIVER=postgres \
  -e GOTRUE_DB_DATABASE_URL="$(url_as supabase_auth_admin)?sslmode=disable" \
  -e API_EXTERNAL_URL=http://127.0.0.1 -e GOTRUE_SITE_URL=http://127.0.0.1 \
  -e GOTRUE_JWT_SECRET=unused-by-migrations-but-required \
  supabase/gotrue:v2.197.0 auth migrate > /dev/null

# Creates the storage schema, such as storage.buckets. The keys and bucket
# settings are required but unused.
docker run --rm --network host \
  -e DATABASE_URL="$(url_as supabase_storage_admin)?sslmode=disable" \
  -e ANON_KEY=unused -e SERVICE_KEY=unused -e AUTH_JWT_SECRET=unused-by-migrations-but-required \
  -e STORAGE_BACKEND=file -e FILE_STORAGE_BACKEND_PATH=/tmp -e TENANT_ID=stub -e REGION=local \
  -e GLOBAL_S3_BUCKET=stub -e DB_INSTALL_ROLES=false \
  --entrypoint node supabase/storage-api:v1.79.36 dist/scripts/migrate-call.js > /dev/null

# Supabase's image allows pg_cron only in the postgres database, and migcheck
# migrates into its own, so CREATE EXTENSION pg_cron fails. For migrations
# that use it, this applies a copy of them without their CREATE and DROP
# EXTENSION pg_cron statements, and creates a cron schema whose functions
# record jobs without running them. Jobs don't run during a migcheck run
# either way.
workdir=.
if grep -qil pg_cron supabase/migrations/*.sql 2>/dev/null; then
  workdir=$(mktemp -d)
  trap 'rm -rf "$workdir"' EXIT
  cp -R supabase "$workdir/"
  perl -0pi -e 's/\b(create|drop)\s+extension\b[^;]*?\bpg_cron\b[^;]*;/-- $1 extension pg_cron: removed by migcheck-migrate.sh/gi' \
    "$workdir"/supabase/migrations/*.sql
  docker run -i --rm --network host "$image" psql -q -v ON_ERROR_STOP=1 "$(url_as supabase_admin)" <<'EOF'
create schema if not exists cron;
create table if not exists cron.job (
  jobid bigserial primary key, schedule text not null, command text not null,
  nodename text not null default 'localhost', nodeport int not null default 5432,
  database text not null default current_database(), username text not null default current_user,
  active boolean not null default true, jobname text unique
);
create table if not exists cron.job_run_details (
  jobid bigint, runid bigserial primary key, job_pid int, database text, username text, command text,
  status text, return_message text, start_time timestamptz, end_time timestamptz
);
create or replace function cron.schedule(schedule text, command text) returns bigint language sql as
  $$ insert into cron.job (schedule, command) values (schedule, command) returning jobid $$;
create or replace function cron.schedule(job_name text, schedule text, command text) returns bigint language sql as
  $$ insert into cron.job (jobname, schedule, command) values (job_name, schedule, command)
     on conflict (jobname) do update set schedule = excluded.schedule, command = excluded.command
     returning jobid $$;
create or replace function cron.schedule_in_database(job_name text, schedule text, command text, database text,
    username text default null, active boolean default true) returns bigint language sql as
  $$ select cron.schedule(job_name, schedule, command) $$;
create or replace function cron.unschedule(job_id bigint) returns boolean language sql as
  $$ with d as (delete from cron.job where jobid = job_id returning 1) select exists (select from d) $$;
create or replace function cron.unschedule(job_name text) returns boolean language sql as
  $$ with d as (delete from cron.job where jobname = job_name returning 1) select exists (select from d) $$;
create or replace function cron.alter_job(job_id bigint, schedule text default null, command text default null,
    database text default null, username text default null, active boolean default null) returns void language sql as
  $$ update cron.job set schedule = coalesce(alter_job.schedule, job.schedule),
       command = coalesce(alter_job.command, job.command), active = coalesce(alter_job.active, job.active)
     where jobid = job_id $$;
grant usage on schema cron to postgres;
grant all on all tables in schema cron to postgres;
grant all on all sequences in schema cron to postgres;
grant execute on all functions in schema cron to postgres;
EOF
fi

npx supabase migration up --workdir "$workdir" --db-url "$DATABASE_URL?sslmode=disable" --include-all
