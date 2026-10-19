#!/usr/bin/env bash
# Builds a demo git repo with one base release and three candidate releases:
#   v1          users table with a name column
#   v2-rename   renames name to full_name (breaks v1)
#   v2-notnull  adds a NOT NULL column without a default (breaks v1 inserts)
#   v2-expand   adds full_name next to name (safe)
#
# Usage: make-demo.sh DIR
set -euo pipefail

dir=${1:?usage: make-demo.sh DIR}
mkdir -p "$dir"
cd "$dir"
git init -q
git checkout -q -b main

mkdir -p migrations

cat > migrate.py <<'EOF'
"""Applies migrations/*.sql that aren't recorded in schema_migrations."""
import os, pathlib, psycopg

with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    for path in sorted(pathlib.Path("migrations").glob("*.sql")):
        if path.stem not in done:
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations VALUES (%s)", (path.stem,))
EOF

cat > migrations/001_create_users.sql <<'EOF'
CREATE TABLE users (
  id    serial PRIMARY KEY,
  name  text NOT NULL,
  email text NOT NULL UNIQUE
);
EOF

cat > app.py <<'EOF'
import os, psycopg

def conn():
    return psycopg.connect(os.environ["DATABASE_URL"])

def create_user(c, name, email):
    return c.execute(
        "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id", (name, email)
    ).fetchone()[0]

def get_user(c, user_id):
    return c.execute("SELECT id, name, email FROM users WHERE id = %s", (user_id,)).fetchone()

def rename_user(c, user_id, name):
    c.execute("UPDATE users SET name = %s WHERE id = %s", (name, user_id))
EOF

cat > test_app.py <<'EOF'
"""Stands in for the app's test suite: exercises every query the app makes."""
import app

with app.conn() as c:
    uid = app.create_user(c, "Ada O'Brien", "ada@example.com")
    assert app.get_user(c, uid)[1] == "Ada O'Brien"
    app.rename_user(c, uid, "Ada Lovelace")
    assert app.get_user(c, uid)[1] == "Ada Lovelace"
print("ok")
EOF

git add -A && git commit -q -m "v1" && git tag v1

git checkout -q -b rename v1
cat > migrations/002_rename_name.sql <<'EOF'
ALTER TABLE users RENAME COLUMN name TO full_name;
EOF
sed -i 's/\bname\b/full_name/g' app.py test_app.py
git add -A && git commit -q -m "v2: rename name to full_name" && git tag v2-rename

git checkout -q -b notnull v1
cat > migrations/002_add_plan.sql <<'EOF'
ALTER TABLE users ADD COLUMN plan text NOT NULL;
EOF
git add -A && git commit -q -m "v2: add required plan column" && git tag v2-notnull

git checkout -q -b expand v1
cat > migrations/002_add_full_name.sql <<'EOF'
ALTER TABLE users ADD COLUMN full_name text;
UPDATE users SET full_name = name;
EOF
git add -A && git commit -q -m "v2: add full_name alongside name" && git tag v2-expand

git checkout -q main
echo "Demo repo ready at $dir (tags: v1, v2-rename, v2-notnull, v2-expand)"
