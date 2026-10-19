"""Types and helpers that the database adapters share."""

import subprocess
from dataclasses import dataclass, field


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
