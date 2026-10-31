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

npx supabase migration up --db-url "$DATABASE_URL?sslmode=disable" --include-all
