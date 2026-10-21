import unittest

from migcheck.common import Statement
from migcheck.mysql import REPLAY_SETUP, _lenient_truncate, build_script, parse_errors, parse_log

from .helpers import blocks, fixture

# The mysql client runs this when it connects.
VERSION_QUERY = "select @@version_comment limit 1"


def log(*entries):
    """Builds a general log from (connection ID, command, argument) tuples."""
    lines = [b"Time                 Id Command    Argument\n"]
    for conn, command, arg in entries:
        lines.append(f"2026-01-01T00:00:00.000000Z\t{conn:5} {command}\t{arg}\n".encode())
    return b"".join(lines).splitlines(keepends=True)


class ParseCapturedLogTest(unittest.TestCase):
    """Parses a log recorded from a real MySQL server by
    tests/capture_fixtures.py."""

    def setUp(self):
        with open(fixture("mysql.log"), "rb") as f:
            self.statements = parse_log(f, exclude={"mc_template"})

    def test_blocks(self):
        parsed = [b for b in blocks(self.statements) if b != (True, [VERSION_QUERY])]
        self.assertEqual(parsed, [
            (True, ["INSERT INTO users (name) VALUES ('ada')"]),
            (True, [
                "INSERT INTO users (name)\n  VALUES ('grace')",
                "UPDATE users SET name = 'Grace' WHERE name = 'grace'",
            ]),
            # Savepoints replay inside their transaction.
            (False, ["SAVEPOINT s1", "DELETE FROM users", "ROLLBACK TO SAVEPOINT s1"]),
            # With autocommit off, COMMIT and SET autocommit = 1 each end a
            # transaction.
            (True, ["INSERT INTO users (name) VALUES ('alan')"]),
            (True, ["INSERT INTO users (name) VALUES ('edsger')"]),
            # Bytes that aren't valid UTF-8 survive.
            (True, ["INSERT INTO blobs (data) VALUES (_binary '\udcff\udcfe')"]),
            # LOCK TABLES and UNLOCK TABLES are dropped.
            (True, ["SELECT 1 FROM users LIMIT 1"]),
            # ALTER TABLE commits the open transaction, and the next insert
            # runs on its own.
            (True, ["INSERT INTO users (name) VALUES ('barbara')"]),
            (True, ["INSERT INTO users (name) VALUES ('after')"]),
            # So does TRUNCATE, which runs on its own.
            (True, ["INSERT INTO users (name) VALUES ('x')"]),
            (True, ["CALL _migcheck_truncate('users')"]),
            # The session quit with this transaction open.
            (False, ["INSERT INTO users (name) VALUES ('never')"]),
            # migcheck's own statement that turns the log off is recorded too.
            # It succeeds on both schemas, so it can't change the result.
            (True, ["SET GLOBAL general_log = OFF"]),
        ])

    def test_skips_excluded_database_after_use(self):
        self.assertNotIn("SELECT COUNT(*) FROM users", [s.sql for s in self.statements])


class ParseLogTest(unittest.TestCase):
    def test_interleaved_connections_get_separate_blocks(self):
        statements = parse_log(log(
            (1, "Connect", "root@localhost on app using TCP/IP"),
            (2, "Connect", "root@localhost on app using TCP/IP"),
            (1, "Query", "START TRANSACTION"),
            (2, "Query", "BEGIN"),
            (1, "Query", "SELECT 'a1'"),
            (2, "Query", "SELECT 'b1'"),
            (2, "Query", "ROLLBACK"),
            (1, "Query", "SELECT 'a2'"),
            (1, "Query", "COMMIT"),
        ), exclude=set())
        self.assertEqual(blocks(statements), [
            (True, ["SELECT 'a1'", "SELECT 'a2'"]),
            (False, ["SELECT 'b1'"]),
        ])

    def test_init_db_switches_database(self):
        statements = parse_log(log(
            (1, "Connect", "root@localhost on  using TCP/IP"),
            (1, "Init DB", "mc_template"),
            (1, "Query", "SELECT 'excluded'"),
            (1, "Init DB", "app"),
            (1, "Query", "SELECT 'kept'"),
        ), exclude={"mc_template"})
        self.assertEqual([s.sql for s in statements], ["SELECT 'kept'"])

    def test_skips_system_databases(self):
        statements = parse_log(log(
            (1, "Connect", "root@localhost on mysql using TCP/IP"),
            (1, "Query", "SELECT 'system'"),
        ), exclude=set())
        self.assertEqual(statements, [])

    def test_prepared_statements_replay_from_execute(self):
        statements = parse_log(log(
            (1, "Connect", "root@localhost on app using TCP/IP"),
            (1, "Prepare", "SELECT ?"),
            (1, "Execute", "SELECT 42"),
            (1, "Close stmt", ""),
            (1, "Query", "PREPARE s FROM 'SELECT 1'"),
            (1, "Query", "EXECUTE s"),
            (1, "Query", "DEALLOCATE PREPARE s"),
        ), exclude=set())
        self.assertEqual([s.sql for s in statements], ["SELECT 42"])

    def test_rollback_to_savepoint_does_not_end_the_transaction(self):
        statements = parse_log(log(
            (1, "Connect", "root@localhost on app using TCP/IP"),
            (1, "Query", "BEGIN"),
            (1, "Query", "SELECT 1"),
            (1, "Query", "ROLLBACK WORK TO SAVEPOINT s"),
            (1, "Query", "SELECT 2"),
            (1, "Query", "COMMIT"),
        ), exclude=set())
        self.assertEqual(blocks(statements), [(True, ["SELECT 1", "ROLLBACK WORK TO SAVEPOINT s", "SELECT 2"])])

    def test_autocommit_variants(self):
        for sql in ("SET autocommit=0", "SET SESSION autocommit = OFF", "SET @@autocommit = 'false'"):
            with self.subTest(sql=sql):
                statements = parse_log(log(
                    (1, "Connect", "root@localhost on app using TCP/IP"),
                    (1, "Query", sql),
                    (1, "Query", "SELECT 1"),
                    (1, "Query", "SELECT 2"),
                    (1, "Quit", ""),
                ), exclude=set())
                # Nothing committed before the connection quit.
                self.assertEqual(blocks(statements), [(False, ["SELECT 1", "SELECT 2"])])


class ReplayScriptTest(unittest.TestCase):
    def test_maps_errors_to_statements(self):
        statements = [
            Statement("SELECT 1", 0, True),
            Statement("SELECT\n  2;", 1, True),
            Statement("SELECT 3 -- comment", 1, True),
            Statement("SELECT 4", 2, False),
        ]
        script, starts = build_script(statements)
        lines = script.decode().splitlines()
        # Each statement starts on the line recorded for it.
        for s, start in zip(statements, starts):
            self.assertEqual(lines[start - 1], s.sql.splitlines()[0].rstrip(";"))
        stderr = "\n".join([
            f"ERROR 1054 (42S22) at line {starts[1] + 1}: Unknown column",
            f"ERROR 1146 (42S02) at line {starts[3]}: Table doesn't exist",
            "mysql: [Warning] something unrelated",
        ])
        self.assertEqual(parse_errors(stderr, starts, len(statements)), [
            (None, None),
            ("1054", "Unknown column"),
            (None, None),
            ("1146", "Table doesn't exist"),
        ])

    def test_wraps_blocks_in_transactions(self):
        script, _ = build_script([
            Statement("SELECT 1", 0, True),
            Statement("SELECT 2", 1, True),
            Statement("SELECT 3", 1, True),
            Statement("SELECT 4", 2, False),
        ])
        body = script.decode().removeprefix(REPLAY_SETUP)
        self.assertEqual(body, (
            "SELECT 1\n;\n"
            "START TRANSACTION;\nSELECT 2\n;\nSELECT 3\n;\nCOMMIT;\n"
            "START TRANSACTION;\nSELECT 4\n;\nROLLBACK;\n"
        ))

    def test_error_in_setup_raises(self):
        _, starts = build_script([Statement("SELECT 1", 0, True)])
        with self.assertRaisesRegex(RuntimeError, "replay setup failed"):
            parse_errors("ERROR 1064 (42000) at line 1: syntax", starts, 1)

    def test_keeps_first_error_per_statement(self):
        _, starts = build_script([Statement("SELECT 1", 0, True)])
        stderr = f"ERROR 1 (x) at line {starts[0]}: first\nERROR 2 (x) at line {starts[0]}: second"
        self.assertEqual(parse_errors(stderr, starts, 1), [("1", "first")])


class LenientTruncateTest(unittest.TestCase):
    def test_strips_schema_and_backticks(self):
        self.assertEqual(_lenient_truncate("`app`.`users`"), "CALL _migcheck_truncate('users')")

    def test_escapes_quotes_and_backslashes(self):
        self.assertEqual(_lenient_truncate("`it's\\`"), "CALL _migcheck_truncate('it''s\\\\')")


if __name__ == "__main__":
    unittest.main()
