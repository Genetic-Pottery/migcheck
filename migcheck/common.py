"""Types and helpers that the database adapters share."""

import subprocess
from dataclasses import dataclass, field

# Appended to a duplicate key error when a row that the head migrations
# inserted holds the key. The old release might check for that row before
# inserting, which replay can't see, so the report explains this case.
MIGRATED_KEY = ", on a key that a row from the head migrations holds"


@dataclass
class Statement:
    sql: str
    # Statements from one workload transaction share a block. Replay rolls
    # back a block's effects if the workload rolled back that transaction,
    # the way a test runner discards each test's data.
    block: int
    keep: bool
    # [table, id] for each row the statement inserted, in insert order.
    ids: list = field(default_factory=list)


def run(cmd, stdin=None):
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}... failed:\n{proc.stderr.strip()}")
    return proc.stdout
