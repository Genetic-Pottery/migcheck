"""Checks that a release's migrations don't break the release before it.

During a rolling deploy, or after a rollback, the previous release runs
against the new schema. migcheck records the queries the previous release
makes, then replays them against its schema and against the new one. A query
that works on the old schema and fails on the new one breaks production.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict

from .mysql import MySQL
from .postgres import Postgres

ADAPTERS = {"postgres": Postgres, "mysql": MySQL}


def main():
    parser = argparse.ArgumentParser(prog="migcheck", description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--repo", default=".", help="git repository to check (default: .)")
    parser.add_argument("--base", required=True, help="ref of the release running in production")
    parser.add_argument("--head", required=True, help="ref of the release being deployed")
    parser.add_argument("--migrate", required=True, help="shell command that applies migrations to $DATABASE_URL")
    parser.add_argument("--workload", required=True, help="shell command that exercises the app against $DATABASE_URL, such as its test suite")
    parser.add_argument("--db", choices=ADAPTERS, default="postgres", help="database the app uses (default: postgres)")
    defaults = " or ".join(a.default_image for a in ADAPTERS.values())
    parser.add_argument("--image", help=f"database Docker image (default: {defaults})")
    args = parser.parse_args()

    repo = os.path.abspath(args.repo)
    workdir = tempfile.mkdtemp(prefix="migcheck-")
    base_dir = os.path.join(workdir, "base")
    head_dir = os.path.join(workdir, "head")
    adapter = ADAPTERS[args.db]
    db = adapter(args.image or adapter.default_image)
    try:
        _git(repo, "worktree", "add", "--detach", "-q", base_dir, args.base)
        _git(repo, "worktree", "add", "--detach", "-q", head_dir, args.head)

        step(f"Starting {db.image}")
        db.start()

        step(f"Applying {args.base} migrations")
        db.create_db("mc_template")
        _sh(args.migrate, base_dir, db.url("mc_template"))

        step(f"Recording {args.base} workload queries")
        db.create_db("mc_capture", template="mc_template")
        db.record()
        # Failing tests still issue queries, so a failing workload is a warning.
        workload_exit = _sh(args.workload, base_dir, db.url("mc_capture"), check=False)
        statements = db.statements(exclude={"mc_template"})
        if not statements:
            fail("the workload ran no queries, so there is nothing to check")

        step(f"Applying {args.head} migrations")
        db.create_db("mc_old", template="mc_template")
        db.create_db("mc_new", template="mc_template")
        _sh(args.migrate, head_dir, db.url("mc_new"))

        step(f"Replaying {len(statements)} statements against both schemas")
        old = db.replay("mc_old", statements)
        new = db.replay("mc_new", statements)
    except Exception as e:  # Exit code 1 means a query breaks, so nothing else may use it.
        fail(f"{type(e).__name__}: {e}")
    finally:
        db.stop()
        for d in (base_dir, head_dir):
            if os.path.isdir(d):
                _git(repo, "worktree", "remove", "--force", d)
        shutil.rmtree(workdir, ignore_errors=True)

    code = report(db, args.base, args.head, statements, old, new)
    if workload_exit:
        print(f"\nNote: the workload exited with code {workload_exit}. If it stopped early, this result covers only the queries it ran.")
    sys.exit(code)


def report(db, base, head, statements, old, new):
    skipped = sum(1 for state, _ in old if state)
    broken = any(o is None and n is not None for (o, _), (n, _) in zip(old, new))

    print()
    if skipped == len(statements):
        print(f"ERROR: all {skipped} statements fail on {base}'s own schema, so there is nothing to compare.")
        print(f"First error: [{old[0][0]}] {old[0][1]}")
        return 2
    if skipped:
        print(f"Skipped {skipped} statements that also fail on {base}'s schema.")
    if not broken:
        print(f"PASS: all {len(statements) - skipped} statements from {base} still work after {head}'s migrations.")
        return 0

    # The first break in a transaction is a root cause. A later integrity
    # error in the same transaction usually follows from it, such as a foreign
    # key to a row whose insert failed. A foreign key error after any earlier
    # break is also a knock-on, because apps often commit each request
    # separately, so the failed insert and the reference to it can sit in
    # different transactions.
    roots = defaultdict(list)
    knock_on = defaultdict(int)
    broken_blocks = set()
    for s, (old_state, _), (new_state, new_msg) in zip(statements, old, new):
        if old_state is not None or new_state is None:
            continue
        key = (new_state, new_msg)
        follows_break = s.block in broken_blocks and db.is_integrity_error(new_state)
        if key not in roots and (follows_break or (broken_blocks and db.is_foreign_key_error(new_state))):
            knock_on[key] += 1
            continue
        broken_blocks.add(s.block)
        roots[key].append(s)

    count = sum(len(v) for v in roots.values())
    print(f"FAIL: {count} statements from {base} break after {head}'s migrations.\n")
    for (state, msg), stmts in roots.items():
        blocks = len({s.block for s in stmts})
        print(f"  [{state}] {msg}")
        print(f"      {len(stmts)} statements in {blocks} transactions. Examples:")
        examples = dict.fromkeys(" ".join(s.sql.split())[:160] for s in stmts)
        for sql in list(examples)[:3]:
            print(f"      {sql}")
        print()
    if knock_on:
        print("  Later failures in the same transactions, likely caused by the errors above:")
        for (state, msg), n in knock_on.items():
            print(f"      [{state}] {msg} ({n})")
        print()
    print(f"{base} fails during a rolling deploy of {head}, and after a rollback to {base}.")
    return 1


def fail(msg):
    """Exits with code 2, which means migcheck couldn't compare."""
    print(f"migcheck: {msg}", file=sys.stderr)
    sys.exit(2)


def step(msg):
    print(f"==> {time.strftime('%H:%M:%S')} {msg}", file=sys.stderr)


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True)


def _sh(cmd, cwd, url, check=True):
    proc = subprocess.run(
        cmd, shell=True, cwd=cwd, env={**os.environ, "DATABASE_URL": url},
        capture_output=True, text=True,
    )
    if proc.returncode != 0 and not check:
        print(f"migcheck: warning: command exited with {proc.returncode}: {cmd}", file=sys.stderr)
        sys.stderr.write(proc.stderr[-2000:])
    elif proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        fail(f"command failed in {cwd}: {cmd}")
    return proc.returncode


if __name__ == "__main__":
    main()
