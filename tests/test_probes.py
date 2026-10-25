import os
import subprocess
import tempfile
import unittest

from migcheck import probes
from migcheck.postgres import Dialect, Postgres

from .helpers import have_docker


def col(name, type_name="integer", notnull=False, default=False):
    return name, {"name": name, "type": type_name, "notnull": notnull, "default": default,
                  "generated": False, "identity": False}


def catalog(tables, fks=None, functions=None):
    return {
        "relations": {
            f"public.{name}": {"schema": "public", "name": name, "kind": kind, "columns": dict(cols)}
            for name, (kind, cols) in tables.items()
        },
        "fks": fks or {},
        "functions": functions or {},
        "enums": {},
        "domains": {},
    }


USERS = ("r", [col("id", notnull=True, default=True), col("name", "text", notnull=True), col("bio", "text")])


class GenerateTest(unittest.TestCase):
    def names(self, old, new):
        found, skipped = probes.generate(old, new, Dialect)
        return [(p.name, p.kind) for p in found], skipped

    def test_no_change_no_probes(self):
        self.assertEqual(self.names(catalog({"users": USERS}), catalog({"users": USERS})), ([], []))

    def test_dropped_table_and_column(self):
        old = catalog({"users": USERS, "posts": ("r", [col("id")])})
        new = catalog({"users": ("r", USERS[1][:2])})
        names, _ = self.names(old, new)
        self.assertEqual(names, [("users.bio", "column dropped"), ("posts", "table dropped")])

    def test_new_required_column(self):
        new = catalog({"users": ("r", [*USERS[1], col("plan", "text", notnull=True)])})
        found, _ = probes.generate(catalog({"users": USERS}), new, Dialect)
        self.assertEqual([(p.name, p.kind) for p in found], [("users.plan", "column requires a value")])
        self.assertEqual(found[0].sql, """INSERT INTO "public"."users" ("name") VALUES ((gen_random_uuid()::text)::text)""")

    def test_new_column_with_a_default_needs_no_probe(self):
        new = catalog({"users": ("r", [*USERS[1], col("plan", "text", notnull=True, default=True)])})
        self.assertEqual(self.names(catalog({"users": USERS}), new), ([], []))

    def test_column_made_required(self):
        new = catalog({"users": ("r", [*USERS[1][:2], col("bio", "text", notnull=True)])})
        names, _ = self.names(catalog({"users": USERS}), new)
        self.assertEqual(names, [("users.bio", "column requires a value")])

    def test_insert_creates_required_parent_rows(self):
        tables = {"users": USERS, "posts": ("r", [col("id", notnull=True, default=True),
                                                  col("user_id", notnull=True), col("editor_id")])}
        fks = {"public.posts": [
            {"table": "public.posts", "ref": "public.users", "columns": ["user_id"], "ref_columns": ["id"]},
            {"table": "public.posts", "ref": "public.users", "columns": ["editor_id"], "ref_columns": ["id"]},
        ]}
        sql = probes.insert(catalog(tables, fks), "public.posts", Dialect)
        self.assertEqual(sql, (
            """WITH p0_0 AS (INSERT INTO "public"."users" ("name") VALUES ((gen_random_uuid()::text)::text) RETURNING "id") """
            """INSERT INTO "public"."posts" ("user_id") SELECT p0_0."id" FROM p0_0"""
        ))

    def test_skips_cycles(self):
        tables = {"nodes": ("r", [col("id", notnull=True, default=True), col("parent_id", notnull=True)])}
        fks = {"public.nodes": [{"table": "public.nodes", "ref": "public.nodes",
                                 "columns": ["parent_id"], "ref_columns": ["id"]}]}
        new = catalog({"nodes": ("r", [*tables["nodes"][1], col("x", notnull=True)])}, fks)
        found, skipped = probes.generate(catalog(tables, fks), new, Dialect)
        self.assertEqual(found, [])
        self.assertEqual(skipped, [("nodes.x", "its required foreign keys form a cycle")])

    def test_dropped_function(self):
        funcs = {"public.score(integer, text)": {"schema": "public", "name": "score", "args": ["integer", "text"]}}
        found, _ = probes.generate(catalog({}, functions=funcs), catalog({}), Dialect)
        self.assertEqual(found[0].sql, 'SELECT "public"."score"(NULL::integer, NULL::text)')


class SearchTest(unittest.TestCase):
    def test_singular(self):
        self.assertEqual([probes._singular(w) for w in ("users", "entries", "addresses", "batches", "status", "data")],
                         ["user", "entry", "address", "batch", "statu", "data"])

    def test_finds_mentions_outside_migrations(self):
        with tempfile.TemporaryDirectory() as root:
            files = {
                "app/models/user.rb": "class User\n  attr :skip_thread_containment",
                "src/user.ts": "users.skipThreadContainment = true",
                "test/user_test.rb": "User skip_thread_containment: true",
                "app/post.rb": "skip_thread_containment",
                "db/migrate/1_add.rb": "add_column :users, :skip_thread_containment",
                "node_modules/x/index.js": "skip_thread_containment",
                "app/other.rb": "skip_thread_containments",
            }
            for path, text in files.items():
                os.makedirs(os.path.join(root, os.path.dirname(path)), exist_ok=True)
                with open(os.path.join(root, path), "w") as f:
                    f.write(text)
            finding = probes.Finding(probes.Probe("users.skip_thread_containment", "column dropped", ""), "", "")
            probes.search(root, [finding])
            self.assertEqual(finding.mentions, 3)
            # In a git checkout, files the workload generated don't count.
            subprocess.run(["git", "init", "-q", root], check=True)
            subprocess.run(["git", "-C", root, "add", "app", "test", "db"], check=True)
            probes.search(root, [finding])
            self.assertEqual(finding.files, ["app/models/user.rb", "test/user_test.rb"])
        self.assertEqual(finding.mentions, 2)


@unittest.skipUnless(have_docker(), "needs Docker")
class PostgresProbeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Postgres()
        try:
            cls.db.start()
        except Exception:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.db.stop()

    def test_finds_untested_breaks(self):
        db = self.db
        db.create_db("base")
        db._psql("base", """
            CREATE TYPE mood AS ENUM ('happy', 'sad');
            CREATE TABLE users (id serial PRIMARY KEY, name varchar(20) NOT NULL, mood mood NOT NULL,
                                created timestamptz NOT NULL, bio text);
            CREATE TABLE posts (id bigserial PRIMARY KEY, user_id int NOT NULL REFERENCES users,
                                body text NOT NULL, tags text[] NOT NULL);
            CREATE TABLE notes (id serial PRIMARY KEY, body text);
            CREATE TABLE settings (id serial PRIMARY KEY, data jsonb NOT NULL);
            CREATE MATERIALIZED VIEW summary AS SELECT count(*) AS n FROM users;
            CREATE FUNCTION score(a int) RETURNS int AS 'SELECT a' LANGUAGE sql;
        """)
        db.create_db("old", template="base")
        db.create_db("new", template="base")
        db._psql("new", """
            ALTER TABLE users DROP COLUMN bio;
            ALTER TABLE posts ADD COLUMN slug text NOT NULL;
            ALTER TABLE settings ADD COLUMN kind text NOT NULL DEFAULT 'x';
            DROP TABLE notes;
            DROP MATERIALIZED VIEW summary;
            CREATE TABLE summary (n bigint);
            DROP FUNCTION score(int);
            CREATE FUNCTION score(a int, b int) RETURNS int AS 'SELECT a + b' LANGUAGE sql;
        """)
        found, skipped = probes.generate(db.catalog("old"), db.catalog("new"), Dialect)
        self.assertEqual(skipped, [])
        statements = probes.as_statements(found, 0)
        old, new = db.replay("old", statements), db.replay("new", statements)
        results, unchecked = probes.findings(found, old, new, set(), Postgres.cause)
        self.assertEqual(unchecked, [])
        self.assertEqual(sorted(f.probe.name for f in results),
                         ["notes", "posts.slug", "score()", "summary", "users.bio"])
