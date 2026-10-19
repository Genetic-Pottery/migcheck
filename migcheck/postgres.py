"""Postgres adapter.

Runs a throwaway Postgres server in Docker with statement logging on, and
talks to it with the psql inside the container, so the host needs only Docker.
"""

import csv
import io
import json
import re
import subprocess
import time
import uuid

from .common import Statement
from .common import run as _run

DEFAULT_IMAGE = "postgres:17-alpine"

# Logged statements can be far larger than the csv module's 128 KiB default,
# for example inserts that carry file contents.
csv.field_size_limit(2**31 - 1)
PASSWORD = "migcheck"
LOG_FILE = "/tmp/pglog/pg.csv"

# csvlog column positions (stable since Postgres 9.0).
COL_DATABASE = 2
COL_SESSION = 5
COL_SEVERITY = 11
COL_MESSAGE = 13
COL_DETAIL = 14

# Statements that control the client's transaction. Replay keeps the
# transaction boundaries but issues its own control statements, so these are
# dropped.
TXN_BEGIN = re.compile(r"^\s*(BEGIN|START\s+TRANSACTION)\b", re.IGNORECASE)
TXN_COMMIT = re.compile(r"^\s*(COMMIT|END)\b(?!\s+PREPARED)", re.IGNORECASE)
TXN_ROLLBACK = re.compile(r"^\s*(ROLLBACK|ABORT)\b(?!\s+(TO|PREPARED)\b)", re.IGNORECASE)
TXN_CONTROL = re.compile(
    r"^\s*(BEGIN|START\s+TRANSACTION|COMMIT|END|ROLLBACK|ABORT|SAVEPOINT|RELEASE|PREPARE\s+TRANSACTION|SET\s+CONSTRAINTS)\b",
    re.IGNORECASE,
)
# Schema changes come from test runners building their database, not from the
# app's runtime queries.
DDL = re.compile(r"^\s*(CREATE|ALTER|DROP|COMMENT|GRANT|REVOKE)\b", re.IGNORECASE)
# Test runners empty tables between tests by name. On the new schema a listed
# table can be gone, or a new table can reference one and block the TRUNCATE.
# Either way the tables stay full and every later test collides with old rows,
# so replay empties whichever listed tables exist, with CASCADE.
TRUNCATE = re.compile(
    r"^\s*TRUNCATE\s+(?:TABLE\s+)?(.*?)(\s+(?:RESTART|CONTINUE|CASCADE|RESTRICT)\b.*)?;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
STATEMENT = re.compile(r"^(?:statement|execute [^:]+): (.*)$", re.DOTALL)
PARAM = re.compile(r"\$(\d+) = ('(?:[^']|'')*'|NULL)")
ROW_ID = "migcheck-id "

# Many apps let the database generate primary keys, through a sequence or a
# function such as Mastodon's timestamp_id(). Replay would generate different
# keys than the workload got, and later statements that reference the
# original keys would fail on both schemas. So every table the workload
# creates logs the id of each row inserted, and replay pins inserts to those
# ids. Installing this in template1 reaches databases the test runner creates.
CAPTURE_IDS = """
CREATE FUNCTION public._migcheck_log_id() RETURNS trigger AS $f$
BEGIN
  RAISE LOG 'migcheck-id %', json_build_array(TG_TABLE_NAME, to_jsonb(NEW) -> 'id');
  RETURN NULL;
END
$f$ LANGUAGE plpgsql;

CREATE FUNCTION public._migcheck_attach() RETURNS event_trigger AS $f$
DECLARE r record;
BEGIN
  FOR r IN SELECT objid::regclass AS t FROM pg_event_trigger_ddl_commands()
           WHERE command_tag = 'CREATE TABLE' AND object_type = 'table' LOOP
    EXECUTE format('CREATE TRIGGER _migcheck_id AFTER INSERT ON %s FOR EACH ROW EXECUTE FUNCTION public._migcheck_log_id()', r.t);
  END LOOP;
END
$f$ LANGUAGE plpgsql;

CREATE EVENT TRIGGER _migcheck_attach ON ddl_command_end
  WHEN TAG IN ('CREATE TABLE') EXECUTE FUNCTION public._migcheck_attach();
"""

PIN_IDS = """
CREATE FUNCTION _migcheck.pin_id() RETURNS trigger AS $f$
DECLARE
  ids jsonb := coalesce(nullif(current_setting('migcheck.ids', true), ''), '[]')::jsonb;
  pos int := coalesce(nullif(current_setting('migcheck.pos', true), ''), '0')::int;
  e jsonb;
BEGIN
  IF pos < jsonb_array_length(ids) THEN
    e := ids -> pos;
    IF e ->> 0 = TG_TABLE_NAME THEN
      PERFORM set_config('migcheck.pos', (pos + 1)::text, true);
      IF e -> 1 <> 'null'::jsonb THEN
        NEW := jsonb_populate_record(NEW, jsonb_build_object('id', e -> 1));
      END IF;
    END IF;
  END IF;
  RETURN NEW;
END
$f$ LANGUAGE plpgsql;

DO $f$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.oid::regclass AS t FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema', '_migcheck')
             AND n.nspname NOT LIKE 'pg_toast%' LOOP
    EXECUTE format('CREATE TRIGGER _migcheck_pin BEFORE INSERT ON %s FOR EACH ROW EXECUTE FUNCTION _migcheck.pin_id()', r.t);
  END LOOP;
END
$f$;
"""


class Postgres:
    name = "Postgres"
    default_image = DEFAULT_IMAGE

    def __init__(self, image=DEFAULT_IMAGE):
        self.image = image
        self.container = None
        self.port = None

    def start(self):
        self.container = f"migcheck-{uuid.uuid4().hex[:8]}"
        _run([
            "docker", "run", "-d", "--rm", "--name", self.container,
            "-e", f"POSTGRES_PASSWORD={PASSWORD}",
            "-p", "127.0.0.1::5432",
            self.image,
            "-c", "log_statement=all",
            "-c", "logging_collector=on",
            "-c", "log_destination=csvlog",
            "-c", "log_directory=/tmp/pglog",
            "-c", "log_filename=pg.log",
            "-c", "log_rotation_size=0",
            # Postgres otherwise copies the whole statement into every log
            # line raised during it, including one per inserted row from the
            # id capture below. A large multi-row insert then fills the log
            # with gigabytes of copies. migcheck reads statements from their
            # own log lines, so it doesn't need the copies.
            "-c", "log_min_error_statement=panic",
            "-c", "fsync=off",
        ])
        mapping = _run(["docker", "port", self.container, "5432/tcp"])
        self.port = int(mapping.splitlines()[0].rsplit(":", 1)[1])
        # The image's init step runs a socket-only server first, so a TCP
        # check waits for the real one.
        for _ in range(60):
            check = subprocess.run(
                ["docker", "exec", self.container, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                capture_output=True,
            )
            if check.returncode == 0:
                self._psql("template1", CAPTURE_IDS)
                return
            time.sleep(0.5)
        raise RuntimeError("Postgres did not become ready in 30 seconds")

    def stop(self):
        if self.container:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True)
            self.container = None

    def url(self, db):
        return f"postgresql://postgres:{PASSWORD}@127.0.0.1:{self.port}/{db}"

    def create_db(self, name, template=None):
        sql = f'CREATE DATABASE "{name}"'
        if template:
            sql += f' TEMPLATE "{template}"'
        self._psql("postgres", sql)

    def record(self):
        """Starts recording statements. Postgres logs every statement from
        the start, so this does nothing."""

    @staticmethod
    def is_integrity_error(state):
        return state.startswith("23")

    @staticmethod
    def is_foreign_key_error(state):
        return state == "23503"

    def statements(self, exclude):
        """Returns the statements logged against any database not in `exclude`,
        with bind parameters inlined.

        Test runners often create their own database, such as Django's
        test_<name>, so this doesn't filter on a single name.
        """
        exclude = {"postgres", "template0", "template1", *exclude}
        # The log can be far larger than the statements kept from it, so read
        # it as a stream instead of loading it whole.
        proc = subprocess.Popen(["docker", "exec", self.container, "cat", LOG_FILE], stdout=subprocess.PIPE)
        assert proc.stdout is not None
        rows = csv.reader(io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace", newline=""))
        blocks = []  # [keep, [[sql, ids], ...]] in the order transactions started
        open_blocks = {}  # session ID -> its open block
        last = {}  # session ID -> its last replayed [sql, ids], or None
        for row in rows:
            if row[COL_DATABASE] in exclude or row[COL_SEVERITY] != "LOG":
                continue
            session = row[COL_SESSION]
            if row[COL_MESSAGE].startswith(ROW_ID):
                if last.get(session):
                    last[session][1].append(json.loads(row[COL_MESSAGE][len(ROW_ID):]))
                continue
            match = STATEMENT.match(row[COL_MESSAGE])
            if not match:
                continue
            sql = match.group(1).strip()
            last[session] = None
            if TXN_BEGIN.match(sql):
                open_blocks[session] = [True, []]
                blocks.append(open_blocks[session])
            elif TXN_COMMIT.match(sql) or TXN_ROLLBACK.match(sql):
                block = open_blocks.pop(session, None)
                if block:
                    block[0] = bool(TXN_COMMIT.match(sql))
            if not sql or TXN_CONTROL.match(sql) or DDL.match(sql):
                continue
            if row[COL_DETAIL].lower().startswith("parameters: "):
                params = {int(n): v for n, v in PARAM.findall(row[COL_DETAIL])}
                sql = _inline_params(sql, params)
            if truncate := TRUNCATE.match(sql):
                sql = _lenient_truncate(truncate)
            last[session] = [sql, []]
            if session in open_blocks:
                open_blocks[session][1].append(last[session])
            else:
                blocks.append([True, [last[session]]])  # autocommit
        if proc.wait() != 0:
            raise RuntimeError(f"reading the Postgres log failed with exit code {proc.returncode}")
        # A transaction still open when the workload exits never committed.
        for block in open_blocks.values():
            block[0] = False
        return [
            Statement(sql, i, keep, ids)
            for i, (keep, stmts) in enumerate(b for b in blocks if b[1])
            for sql, ids in stmts
        ]

    def replay(self, db, statements):
        """Runs each statement in order and returns a list of (sqlstate, message).

        A statement that succeeds keeps its effects until the end of its
        block, so later statements see the rows earlier ones wrote. A failing
        statement rolls back on its own and replay continues. Both entries are
        None on success.
        """
        tag = f"$mc_{uuid.uuid4().hex}$"
        lines = [
            "ALTER EVENT TRIGGER _migcheck_attach DISABLE;",
            "CREATE SCHEMA _migcheck;",
            "CREATE TABLE _migcheck.stmt (id int PRIMARY KEY, block int NOT NULL, keep bool NOT NULL, row_ids text NOT NULL, sql text NOT NULL);",
            "CREATE TABLE _migcheck.result (id int PRIMARY KEY, sqlstate text, message text);",
            """
CREATE FUNCTION _migcheck.truncate(names text[], restart bool) RETURNS void AS $f$
DECLARE
  tables text[] := '{}';
  t regclass;
BEGIN
  FOR i IN 1 .. coalesce(array_length(names, 1), 0) LOOP
    t := to_regclass(names[i]);
    IF t IS NOT NULL THEN
      tables := array_append(tables, t::text);
    END IF;
  END LOOP;
  IF array_length(tables, 1) > 0 THEN
    EXECUTE 'TRUNCATE ' || array_to_string(tables, ', ')
      || CASE WHEN restart THEN ' RESTART IDENTITY' ELSE '' END || ' CASCADE';
  END IF;
END
$f$ LANGUAGE plpgsql;""",
            PIN_IDS,
        ]
        for i, s in enumerate(statements):
            ids = json.dumps(s.ids)
            lines.append(f"INSERT INTO _migcheck.stmt VALUES ({i}, {s.block}, {s.keep}, {tag}{ids}{tag}, {tag}{s.sql}{tag});")
        # Results collect in variables because a rolled-back block also rolls
        # back any rows it wrote.
        lines.append("""
DO $do$
DECLARE
  b record;
  s record;
  ids int[] := '{}';
  states text[] := '{}';
  msgs text[] := '{}';
BEGIN
  -- Replay runs in one transaction, so deferred constraints (Django's foreign
  -- keys, for example) would fail the whole run at commit instead of failing
  -- the statement that broke them.
  SET CONSTRAINTS ALL IMMEDIATE;
  FOR b IN SELECT block, bool_and(keep) AS keep FROM _migcheck.stmt GROUP BY block ORDER BY block LOOP
    BEGIN
      FOR s IN SELECT id, row_ids, sql FROM _migcheck.stmt WHERE block = b.block ORDER BY id LOOP
        BEGIN
          PERFORM set_config('migcheck.ids', s.row_ids, true);
          PERFORM set_config('migcheck.pos', '0', true);
          EXECUTE s.sql;
          ids := array_append(ids, s.id);
          states := array_append(states, NULL::text);
          msgs := array_append(msgs, NULL::text);
        EXCEPTION WHEN OTHERS THEN
          ids := array_append(ids, s.id);
          states := array_append(states, SQLSTATE);
          msgs := array_append(msgs, SQLERRM);
        END;
      END LOOP;
      IF NOT b.keep THEN
        RAISE EXCEPTION 'migcheck rollback' USING ERRCODE = 'MC001';
      END IF;
    EXCEPTION WHEN SQLSTATE 'MC001' THEN NULL;
    END;
  END LOOP;
  INSERT INTO _migcheck.result SELECT * FROM unnest(ids, states, msgs);
END
$do$;
COPY (SELECT id, sqlstate, message FROM _migcheck.result ORDER BY id) TO STDOUT WITH CSV;
""")
        raw = self._psql(db, "\n".join(lines))
        results: list[tuple[str | None, str | None]] = [(None, None)] * len(statements)
        for row in csv.reader(io.StringIO(raw)):
            results[int(row[0])] = (row[1] or None, row[2] or None)
        return results

    def _psql(self, db, sql):
        return _run(
            ["docker", "exec", "-i", self.container,
             "psql", "-U", "postgres", "-d", db, "-X", "-q", "-v", "ON_ERROR_STOP=1"],
            stdin=sql,
        )


def _lenient_truncate(match):
    """Rewrites a TRUNCATE to call _migcheck.truncate, which replay defines."""
    names = []
    for name in match.group(1).split(","):
        name = re.sub(r"^\s*ONLY\s+", "", name, flags=re.IGNORECASE).strip().rstrip("*").strip()
        names.append("'" + name.replace("'", "''") + "'")
    restart = bool(re.search(r"RESTART\s+IDENTITY", match.group(2) or "", re.IGNORECASE))
    return f"SELECT _migcheck.truncate(ARRAY[{', '.join(names)}]::text[], {restart})"


def _inline_params(sql, params):
    """Replaces $N placeholders with logged literals, skipping quoted text."""
    out = []
    i = 0
    while i < len(sql):
        c = sql[i]
        if c in "'\"":
            end = i + 1
            while end < len(sql):
                if sql[end] == c:
                    if end + 1 < len(sql) and sql[end + 1] == c:
                        end += 2
                        continue
                    break
                end += 1
            out.append(sql[i:end + 1])
            i = end + 1
            continue
        m = re.match(r"\$(\d+)", sql[i:])
        if m and int(m.group(1)) in params:
            out.append(params[int(m.group(1))])
            i += len(m.group(0))
            continue
        out.append(c)
        i += 1
    return "".join(out)

