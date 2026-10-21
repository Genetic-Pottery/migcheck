"""Replays statements against real databases in Docker.

These tests start a Postgres and a MySQL container, so they take a minute.
They're skipped when Docker isn't available.
"""

import unittest

from migcheck.common import Statement
from migcheck.mysql import MySQL
from migcheck.postgres import Postgres

from .helpers import have_docker, mysql_stdin

OK = (None, None)


@unittest.skipUnless(have_docker(), "needs Docker")
class PostgresReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = Postgres()
        try:
            cls.db.start()
            cls.db.create_db("base")
            cls.db._psql("base", """
                CREATE TABLE users (id serial PRIMARY KEY, email text UNIQUE);
                CREATE TABLE posts (id serial PRIMARY KEY, user_id int REFERENCES users DEFERRABLE INITIALLY DEFERRED);
            """)
        except Exception:
            cls.db.stop()
            raise
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.db.stop()

    def replay(self, statements):
        # Replay sets up its own schema, so each call needs a fresh database.
        type(self).count += 1
        name = f"replay{self.count}"
        self.db.create_db(name, template="base")
        return self.db.replay(name, statements)

    def test_reports_each_failure(self):
        results = self.replay([
            Statement("SELECT 1", 0, True),
            Statement("SELECT missing FROM users", 1, True),
            Statement("SELECT 2", 1, True),
        ])
        self.assertEqual(results[0], OK)
        self.assertEqual(results[1], ("42703", 'column "missing" does not exist'))
        self.assertEqual(results[2], OK)

    def test_later_statements_see_earlier_writes(self):
        results = self.replay([
            Statement("INSERT INTO users (email) VALUES ('a')", 0, True),
            Statement("INSERT INTO users (email) VALUES ('a')", 1, True),
        ])
        self.assertEqual(results[0], OK)
        self.assertEqual(results[1][0], "23505")

    def test_rolled_back_blocks_leave_no_rows(self):
        results = self.replay([
            Statement("INSERT INTO users (email) VALUES ('a')", 0, False),
            Statement("INSERT INTO users (email) VALUES ('a')", 1, True),
        ])
        self.assertEqual(results, [OK, OK])

    def test_pins_inserted_ids(self):
        results = self.replay([
            Statement("INSERT INTO users (email) VALUES ('a')", 0, True, [["users", 42]]),
            Statement("INSERT INTO posts (user_id) VALUES (42)", 1, True),
        ])
        self.assertEqual(results, [OK, OK])

    def test_deferred_foreign_keys_fail_their_statement(self):
        results = self.replay([
            Statement("INSERT INTO posts (user_id) VALUES (999)", 0, True),
            Statement("SELECT 1", 1, True),
        ])
        self.assertEqual(results[0][0], "23503")
        self.assertEqual(results[1], OK)

    def test_truncate_skips_missing_tables(self):
        results = self.replay([
            Statement("SELECT _migcheck.truncate(ARRAY['users', 'gone']::text[], True)", 0, True),
        ])
        self.assertEqual(results, [OK])

    def test_statements_containing_dollar_quotes(self):
        results = self.replay([Statement("SELECT $$it's$$, $x$ $mc_ $x$", 0, True)])
        self.assertEqual(results, [OK])


@unittest.skipUnless(have_docker(), "needs Docker")
class MySQLReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = MySQL()
        try:
            cls.db.start()
            cls.db.create_db("base")
            mysql_stdin(cls.db, "base", b"""
                CREATE TABLE users (id int AUTO_INCREMENT PRIMARY KEY, email varchar(50) UNIQUE);
                CREATE TABLE blobs (data varbinary(10));
            """)
        except Exception:
            cls.db.stop()
            raise
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.db.stop()

    def replay(self, statements):
        type(self).count += 1
        name = f"replay{self.count}"
        self.db.create_db(name, template="base")
        return self.db.replay(name, statements)

    def test_maps_errors_to_their_statements(self):
        results = self.replay([
            Statement("SELECT 1", 0, True),
            Statement("SELECT\n  missing\nFROM users", 1, True),
            Statement("SELECT 2 -- trailing comment", 1, True),
            Statement("SELECT * FROM gone", 2, True),
        ])
        self.assertEqual(results[0], OK)
        self.assertEqual(results[1][0], "1054")
        self.assertEqual(results[2], OK)
        self.assertEqual(results[3][0], "1146")

    def test_rolled_back_blocks_leave_no_rows(self):
        results = self.replay([
            Statement("INSERT INTO users (email) VALUES ('a')", 0, False),
            Statement("INSERT INTO users (email) VALUES ('a')", 1, True),
            Statement("INSERT INTO users (email) VALUES ('a')", 2, True),
        ])
        self.assertEqual(results[:2], [OK, OK])
        self.assertEqual(results[2][0], "1062")

    def test_truncate_skips_missing_tables(self):
        results = self.replay([
            Statement("CALL _migcheck_truncate('users')", 0, True),
            Statement("CALL _migcheck_truncate('gone')", 1, True),
        ])
        self.assertEqual(results, [OK, OK])

    def test_binary_values(self):
        results = self.replay([Statement("INSERT INTO blobs VALUES (_binary '\udcff\udcfe')", 0, True)])
        self.assertEqual(results, [OK])


if __name__ == "__main__":
    unittest.main()
