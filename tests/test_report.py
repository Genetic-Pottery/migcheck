import contextlib
import io
import os
import tempfile
import unittest

from migcheck import __main__ as cli
from migcheck.common import Statement
from migcheck.postgres import Postgres

OK = (None, None)


def run_report(statements, old, new, workload_exit=0, head_name=None):
    """Runs report() and returns (exit code, stdout)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.report(Postgres, "v1", "v2", statements, old, new, workload_exit, head_name)
    return code, out.getvalue()


class ClassifyTest(unittest.TestCase):
    def test_first_break_in_a_block_is_a_root_cause(self):
        statements = [Statement("a", 0, True), Statement("b", 0, True)]
        new = [("42703", "column missing"), ("23502", "null value")]
        roots, knock_on = cli._classify(Postgres, statements, [OK, OK], new)
        self.assertEqual(list(roots), [("42703", "column missing")])
        self.assertEqual(dict(knock_on), {("23502", "null value"): 1})

    def test_non_integrity_errors_after_a_break_are_root_causes(self):
        statements = [Statement("a", 0, True), Statement("b", 0, True)]
        new = [("42703", "column missing"), ("42P01", "table missing")]
        roots, knock_on = cli._classify(Postgres, statements, [OK, OK], new)
        self.assertEqual(list(roots), [("42703", "column missing"), ("42P01", "table missing")])
        self.assertEqual(knock_on, {})

    def test_foreign_key_error_in_a_later_block_is_a_knock_on(self):
        statements = [Statement("a", 0, True), Statement("b", 1, True), Statement("c", 2, True)]
        new = [("42703", "column missing"), ("23503", "foreign key"), ("23505", "duplicate")]
        roots, knock_on = cli._classify(Postgres, statements, [OK, OK, OK], new)
        self.assertEqual(list(roots), [("42703", "column missing"), ("23505", "duplicate")])
        self.assertEqual(dict(knock_on), {("23503", "foreign key"): 1})

    def test_a_known_root_cause_stays_a_root_cause(self):
        statements = [Statement("a", 0, True), Statement("b", 0, True)]
        new = [("23502", "null value"), ("23502", "null value")]
        roots, knock_on = cli._classify(Postgres, statements, [OK, OK], new)
        self.assertEqual(len(roots[("23502", "null value")]), 2)
        self.assertEqual(knock_on, {})

    def test_ignores_statements_that_fail_on_the_old_schema(self):
        statements = [Statement("a", 0, True)]
        roots, knock_on = cli._classify(Postgres, statements, [("42703", "x")], [("42703", "x")])
        self.assertEqual((roots, knock_on), ({}, {}))


class ReportTest(unittest.TestCase):
    def setUp(self):
        cli.MARKDOWN = None

    def tearDown(self):
        cli.MARKDOWN = None

    def test_pass(self):
        code, out = run_report([Statement("SELECT 1", 0, True)], [OK], [OK])
        self.assertEqual(code, 0)
        self.assertIn("PASS: all 1 statements from v1 still work after v2's migrations.", out)

    def test_fail(self):
        statements = [Statement("SELECT name FROM users", 0, True), Statement("SELECT 1", 1, True)]
        code, out = run_report(statements, [OK, OK], [("42703", 'column "name" does not exist'), OK])
        self.assertEqual(code, 1)
        self.assertIn("FAIL: 1 statements from v1 break after v2's migrations.", out)
        self.assertIn('[42703] column "name" does not exist', out)
        self.assertIn("SELECT name FROM users", out)

    def test_reports_skipped_statements_and_workload_exit(self):
        statements = [Statement("a", 0, True), Statement("b", 1, True)]
        code, out = run_report(statements, [("42703", "x"), OK], [("42703", "x"), OK], workload_exit=3)
        self.assertEqual(code, 0)
        self.assertIn("Skipped 1 statements that also fail on v1's schema.", out)
        self.assertIn("The workload exited with code 3.", out)

    def test_lists_the_most_common_skip_reasons(self):
        statements = [Statement(str(i), i, True) for i in range(4)]
        old = [("23505", "duplicate key 1"), ("42P01", "no table"), ("23505", "duplicate key 2"), OK]
        code, out = run_report(statements, old, old)
        self.assertEqual(code, 0)
        self.assertIn("    [23505] duplicate key 1 (2)\n    [42P01] no table (1)\n", out)

    def test_pass_with_a_failed_workload_is_a_warning(self):
        code, out = run_report([Statement("a", 0, True)], [OK], [OK], workload_exit=2)
        self.assertEqual(code, 0)
        self.assertIn(
            "PASS WITH WARNING: all 1 statements from v1 still work after v2's migrations, "
            "but the workload exited with code 2.", out,
        )
        self.assertIn("Warning: The workload exited with code 2.", out)
        self.assertIn("--require-workload-success", out)

    def test_fail_with_a_failed_workload_is_still_a_fail(self):
        code, out = run_report([Statement("a", 0, True)], [OK], [("42703", "x")], workload_exit=2)
        self.assertEqual(code, 1)
        self.assertIn("Note: The workload exited with code 2.", out)
        self.assertNotIn("--require-workload-success", out)

    def test_exits_2_when_every_statement_fails_on_the_old_schema(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as exit:
            run_report([Statement("a", 0, True)], [("42P01", "no table")], [("42P01", "no table")])
        self.assertEqual(exit.exception.code, 2)
        self.assertIn("[42P01] no table", err.getvalue())

    def test_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli.MARKDOWN = os.path.join(tmp, "result.md")
            statements = [Statement("SELECT a_b FROM t", 0, True), Statement("INSERT", 0, True)]
            new = [("42703", "column <a_b> missing"), ("23502", "null value")]
            run_report(statements, [OK, OK], new, head_name="this pull request")
            with open(cli.MARKDOWN) as f:
                md = f.read()
        self.assertTrue(md.startswith(cli.MARKER + "\n### migcheck: FAIL\n"))
        self.assertIn("1 statements from `v1` break after this pull request's migrations.", md)
        self.assertIn("**`42703` column &lt;a\\_b> missing**", md)
        self.assertIn("```sql\nSELECT a_b FROM t\n```", md)
        self.assertIn("- `23502` null value (1)", md)

    def test_markdown_lists_skip_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli.MARKDOWN = os.path.join(tmp, "result.md")
            statements = [Statement("a", 0, True), Statement("b", 1, True)]
            run_report(statements, [("42P01", "no <t>"), OK], [("42P01", "no <t>"), OK])
            with open(cli.MARKDOWN) as f:
                md = f.read()
        self.assertIn("<summary>Skipped 1 statements that also fail on <code>v1</code>'s schema</summary>", md)
        self.assertIn("- `42P01` no &lt;t> (1)", md)

    def test_markdown_pass_with_a_failed_workload(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli.MARKDOWN = os.path.join(tmp, "result.md")
            run_report([Statement("a", 0, True)], [OK], [OK], workload_exit=2)
            with open(cli.MARKDOWN) as f:
                md = f.read()
        self.assertTrue(md.startswith(cli.MARKER + "\n### migcheck: PASS with a warning\n"))
        self.assertIn("> [!WARNING]\n> The workload exited with code 2.", md)


class ExamplesTest(unittest.TestCase):
    def test_collapses_whitespace_dedupes_and_limits(self):
        statements = [Statement(sql, 0, True) for sql in ("SELECT\n  1", "SELECT 1", "SELECT 2", "SELECT 3", "SELECT 4")]
        self.assertEqual(cli._examples(statements, 8), ["SELECT 1", "SELECT 2", "SELECT 3"])

    def test_truncates_to_width(self):
        self.assertEqual(cli._examples([Statement("SELECT 123456", 0, True)], 8), ["SELECT 1"])


if __name__ == "__main__":
    unittest.main()
