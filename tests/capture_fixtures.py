"""Records the log fixtures the parser tests read.

Runs a small workload against each database with migcheck's own adapter, then
saves the server's log. To regenerate the fixtures, run this from the
repository root. It needs Docker.

    python3 -m tests.capture_fixtures
"""

import os
import subprocess

from migcheck.mysql import LOG_FILE as MYSQL_LOG
from migcheck.mysql import MySQL
from migcheck.postgres import LOG_FILE as POSTGRES_LOG
from migcheck.postgres import Postgres

from .helpers import mysql_stdin

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

POSTGRES_SCHEMA = """
CREATE TABLE users (id serial PRIMARY KEY, name text NOT NULL);
CREATE TABLE posts (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, user_id int REFERENCES users, body text);
"""

# Each entry runs in its own psql session, in order.
POSTGRES_SESSIONS = [
    # An autocommit insert. Its id is captured.
    "INSERT INTO users (name) VALUES ('ada');",
    # A committed transaction with a multi-row insert.
    "BEGIN;\n"
    "INSERT INTO users (name) VALUES ('grace'), ('alan');\n"
    "UPDATE users SET name = 'Grace' WHERE name = 'grace';\n"
    "COMMIT;",
    # A rolled-back transaction.
    "BEGIN;\nDELETE FROM users;\nROLLBACK;",
    # Bind parameters over the extended protocol, including a quote and a
    # literal $1 inside a string that must stay as is.
    "SELECT name, '$1' AS literal FROM users WHERE name = $1 \\bind 'O''Brien' \\g\n"
    "INSERT INTO posts (user_id, body) VALUES ($1, $2) \\bind 1 'hello' \\g",
    # A statement that fails. It's logged, then an ERROR line follows.
    "SELECT missing FROM users;",
    # A savepoint inside a committed transaction.
    "BEGIN;\n"
    "SAVEPOINT s1;\n"
    "INSERT INTO users (name) VALUES ('tmp');\n"
    "ROLLBACK TO SAVEPOINT s1;\n"
    "END;",
    # A test runner emptying tables.
    "TRUNCATE ONLY users, posts RESTART IDENTITY CASCADE;",
    # A schema change, which replay skips.
    "ALTER TABLE users ADD COLUMN email text;",
    # A transaction left open when the session exits.
    "BEGIN;\nINSERT INTO users (name) VALUES ('never');",
]

MYSQL_SCHEMA = """
CREATE TABLE users (id int AUTO_INCREMENT PRIMARY KEY, name varchar(50) NOT NULL);
CREATE TABLE blobs (id int AUTO_INCREMENT PRIMARY KEY, data varbinary(10));
"""

# Each entry runs in its own mysql client session, in order.
MYSQL_SESSIONS = [
    # An autocommit insert.
    "INSERT INTO users (name) VALUES ('ada');",
    # A committed transaction, with a statement that spans lines.
    "START TRANSACTION;\n"
    "INSERT INTO users (name)\n  VALUES ('grace');\n"
    "UPDATE users SET name = 'Grace' WHERE name = 'grace';\n"
    "COMMIT;",
    # A rolled-back transaction with a savepoint rollback inside it.
    "BEGIN;\n"
    "SAVEPOINT s1;\n"
    "DELETE FROM users;\n"
    "ROLLBACK TO SAVEPOINT s1;\n"
    "ROLLBACK;",
    # Implicit transactions with autocommit off, ended by COMMIT and by
    # turning autocommit back on.
    "SET autocommit = 0;\n"
    "INSERT INTO users (name) VALUES ('alan');\n"
    "COMMIT;\n"
    "INSERT INTO users (name) VALUES ('edsger');\n"
    "SET autocommit = 1;",
    # A switch to the template database, which is excluded.
    "USE mc_template;\nSELECT COUNT(*) FROM users;",
    # Bytes that aren't valid UTF-8.
    b"INSERT INTO blobs (data) VALUES (_binary '\xff\xfe');",
    # A table lock, which is session-only.
    "LOCK TABLES users WRITE;\nSELECT 1 FROM users LIMIT 1;\nUNLOCK TABLES;",
    # A schema change inside a transaction, which commits it.
    "START TRANSACTION;\n"
    "INSERT INTO users (name) VALUES ('barbara');\n"
    "ALTER TABLE users ADD COLUMN email varchar(50);\n"
    "INSERT INTO users (name) VALUES ('after');",
    # A test runner emptying a table inside a transaction.
    "START TRANSACTION;\nINSERT INTO users (name) VALUES ('x');\nTRUNCATE TABLE `users`;\nCOMMIT;",
    # A transaction left open when the session exits.
    "START TRANSACTION;\nINSERT INTO users (name) VALUES ('never');",
]


def capture_postgres():
    db = Postgres()
    try:
        db.start()
        db.create_db("mc_template")
        db._psql("mc_template", POSTGRES_SCHEMA)
        db.create_db("mc_capture", template="mc_template")
        assert db.container
        for sql in POSTGRES_SESSIONS:
            subprocess.run(
                ["docker", "exec", "-i", db.container, "psql", "-U", "postgres", "-d", "mc_capture", "-X", "-q"],
                input=sql, capture_output=True, text=True,
            )
        _save(db.container, POSTGRES_LOG, "postgres.csv")
    finally:
        db.stop()


def capture_mysql():
    db = MySQL()
    try:
        db.start()
        db.create_db("mc_template")
        mysql_stdin(db, "mc_template", MYSQL_SCHEMA.encode())
        db.create_db("mc_capture", template="mc_template")
        db.record()
        for sql in MYSQL_SESSIONS:
            mysql_stdin(db, "mc_capture", sql if isinstance(sql, bytes) else sql.encode(), check=False)
        subprocess.run(db._client("-e", "SET GLOBAL general_log = OFF"), check=True)
        _save(db.container, MYSQL_LOG, "mysql.log")
    finally:
        db.stop()


def _save(container, path, name):
    log = subprocess.run(["docker", "exec", container, "cat", path], capture_output=True, check=True).stdout
    with open(os.path.join(FIXTURES, name), "wb") as f:
        f.write(log)
    print(f"wrote tests/fixtures/{name} ({len(log):,} bytes)")


if __name__ == "__main__":
    os.makedirs(FIXTURES, exist_ok=True)
    capture_postgres()
    capture_mysql()
