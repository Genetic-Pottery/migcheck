"""Helpers the tests share."""

import os
import subprocess

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    return os.path.join(FIXTURES, name)


def blocks(statements):
    """Groups statements into [(keep, [sql, ...]), ...] in block order, which
    reads more clearly in assertions than block numbers do."""
    out = []
    for s in statements:
        if not out or out[-1][0] != s.block:
            out.append((s.block, s.keep, []))
        out[-1][2].append(s.sql)
    return [(keep, sqls) for _, keep, sqls in out]


def mysql_stdin(db, database, sql, check=True):
    """Runs `sql` (bytes) with the mysql client in a MySQL adapter's container."""
    from migcheck.mysql import PASSWORD

    return subprocess.run(
        ["docker", "exec", "-i", "-e", f"MYSQL_PWD={PASSWORD}", db.container,
         "mysql", "-uroot", "--force", "--binary-mode", database],
        input=sql, capture_output=True, check=check,
    )


def have_docker():
    try:
        return subprocess.run(["docker", "info"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False
