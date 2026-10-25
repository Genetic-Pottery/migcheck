"""Schema probes: queries generated from the schema diff.

Replay catches breaks only in code the workload runs. Probes cover the rest
of the schema change. For each table, column, or function that the head
migrations drop or rename, and each table where an insert needs a value that
it didn't need before, migcheck writes a query that the base release might
run. Probes replay like recorded statements, on both schemas, and a probe
that works on the old schema and fails on the new one is a finding.

A probe shows what might break, not what does: the base release might never
run the query. So the report lists findings apart from replay's breaks, and
says where the base release's source mentions each name.
"""

import os
import re
import subprocess
from dataclasses import dataclass, field

from .common import Statement


@dataclass
class Probe:
    name: str  # The table, column, or function, such as "users.name".
    kind: str  # What changed, such as "column dropped".
    sql: str


@dataclass
class Finding:
    probe: Probe
    state: str
    msg: str
    files: list = field(default_factory=list)  # Base release files that mention the name.
    mentions: int = 0


def short(qualified):
    """Drops the schema from "schema.table" when it's the default one."""
    schema, _, name = qualified.partition(".")
    return name if schema in ("public", "") else qualified


def generate(old, new, dialect):
    """Returns (probes, skipped) for the changes from catalog `old` to catalog
    `new`. `skipped` lists (name, reason) for changes that migcheck couldn't
    write a probe for."""
    probes, skipped = [], []
    old_rels, new_rels = old["relations"], new["relations"]
    for key, rel in old_rels.items():
        q = dialect.table(rel["schema"], rel["name"])
        label = "view" if rel["kind"] in ("v", "m") else "table"
        if key not in new_rels:
            probes.append(Probe(short(key), f"{label} dropped", f"SELECT * FROM {q} LIMIT 0"))
            continue
        new_rel = new_rels[key]
        if rel["kind"] == "m" and new_rel["kind"] != "m":
            probes.append(Probe(short(key), "materialized view changed", f"REFRESH MATERIALIZED VIEW {q}"))
        for col in rel["columns"]:
            if col not in new_rel["columns"]:
                probes.append(Probe(
                    f"{short(key)}.{col}", "column dropped", f"SELECT {dialect.ident(col)} FROM {q} LIMIT 0"
                ))
        if rel["kind"] in ("r", "p") and new_rel["kind"] in ("r", "p"):
            needed = [c for c, info in new_rel["columns"].items() if _requires_value(info)
                      and not (c in rel["columns"] and _requires_value(rel["columns"][c]))]
            if needed:
                try:
                    sql = insert(old, key, dialect)
                except ProbeError as e:
                    skipped.extend((f"{short(key)}.{c}", str(e)) for c in needed)
                else:
                    probes.extend(Probe(f"{short(key)}.{c}", "column requires a value", sql) for c in needed[:1])
    new_funcs = set(new["functions"])
    for key, func in old["functions"].items():
        if key not in new_funcs:
            args = ", ".join(dialect.null(t) for t in func["args"])
            probes.append(Probe(
                f"{func['name']}()", "function dropped or changed",
                f"SELECT {dialect.table(func['schema'], func['name'])}({args})",
            ))
    return probes, skipped


def _requires_value(col):
    return col["notnull"] and not (col["default"] or col["generated"] or col["identity"])


class ProbeError(Exception):
    pass


def insert(catalog, key, dialect):
    """Returns an insert into table `key` that sets only the columns the old
    schema requires, with parent rows for its required foreign keys, as one
    statement."""
    ctes = []
    select = _insert(catalog, key, dialect, ctes, ())
    if not ctes:
        return select
    return "WITH " + ", ".join(ctes) + " " + select


def _insert(catalog, key, dialect, ctes, path, returning=None):
    """Appends a CTE for each parent row the insert into `key` needs, and
    returns the insert itself."""
    if key in path:
        raise ProbeError("its required foreign keys form a cycle")
    if len(path) > 8:
        raise ProbeError("its required foreign keys are nested too deeply")
    rel = catalog["relations"][key]
    values, sources = {}, []
    for fk in catalog["fks"].get(key, []):
        if not all(_requires_value(rel["columns"][c]) for c in fk["columns"]):
            continue
        parent = f"p{len(ctes)}_{len(path)}"
        body = _insert(catalog, fk["ref"], dialect, ctes, (*path, key), returning=fk["ref_columns"])
        ctes.append(f"{parent} AS ({body})")
        sources.append(parent)
        for c, ref in zip(fk["columns"], fk["ref_columns"]):
            values[c] = f"{parent}.{dialect.ident(ref)}"
    for c, info in rel["columns"].items():
        if c not in values and _requires_value(info):
            literal = dialect.literal(info["type"], catalog)
            if literal is None:
                raise ProbeError(f"migcheck can't make a value of type {info['type']} for {c}")
            values[c] = literal
    q = dialect.table(rel["schema"], rel["name"])
    cols = ", ".join(dialect.ident(c) for c in values)
    ret = f" RETURNING {', '.join(dialect.ident(c) for c in returning)}" if returning else ""
    if not values:
        return f"INSERT INTO {q} DEFAULT VALUES{ret}"
    exprs = ", ".join(values.values())
    if sources:
        return f"INSERT INTO {q} ({cols}) SELECT {exprs} FROM {', '.join(sources)}{ret}"
    return f"INSERT INTO {q} ({cols}) VALUES ({exprs}){ret}"


def as_statements(probes, first_block):
    """Returns the probes as statements, each in its own block that replay
    rolls back."""
    return [Statement(p.sql, first_block + i, False) for i, p in enumerate(probes)]


def findings(probes, old, new, known, cause):
    """Returns (findings, unchecked) for the probes' replay results. Each
    finding is named by what its error names, through `cause`, when that
    resolves. Findings for names in `known`, the causes replay already found,
    are left out, because replay shows those breaks with the workload's own
    queries."""
    found, unchecked, seen = [], [], set(known)
    for p, (old_state, old_msg), (new_state, new_msg) in zip(probes, old, new):
        if old_state is not None:
            unchecked.append((p.name, f"the probe fails on the old schema too: [{old_state}] {old_msg}"))
        elif new_state is not None:
            name = cause(new_msg, p.sql) or p.name
            if name not in seen:
                seen.add(name)
                found.append(Finding(Probe(name, p.kind, p.sql), new_state, new_msg))
    return found, unchecked


# Directories that hold migrations, dependencies, or build output rather
# than the app's own code. Some apps commit these too.
SKIP_DIRS = re.compile(
    r"^(\..*|node_modules|vendor|dist|build|target|out|coverage|tmp|log|logs|public|__pycache__|venv|"
    r".*migrat.*)$",
    re.IGNORECASE,
)
SKIP_FILES = re.compile(
    r"(\.lock|-lock\.json|-lock\.yaml|\.min\.js|\.map|\.snap|^schema\.rb|^structure\.sql)$", re.IGNORECASE
)


def _forms(word):
    """Returns a pattern for `word` as written or without underscores,
    ignoring case, so it matches snake_case, camelCase, and PascalCase."""
    forms = {re.escape(word), re.escape(word.replace("_", ""))}
    return rf"(?<![\w$])(?:{'|'.join(sorted(forms))})(?![\w$])"


def _singular(word):
    for plural, single in (("ies", "y"), ("sses", "ss"), ("shes", "sh"), ("ches", "ch"), ("xes", "x")):
        if word.endswith(plural):
            return word[: -len(plural)] + single
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def subject(name):
    """Returns (table, word) for a finding's name: the table that a column
    belongs to, or None, and the column, table, or function name."""
    parts = name.removesuffix("()").split(".")
    return (parts[-2] if len(parts) > 1 else None), parts[-1]


def _source_files(root):
    """Returns the paths under `root` that git tracks, which leaves out what
    the workload built or installed, such as generated clients. Outside a git
    checkout, returns every file."""
    proc = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True)
    if proc.returncode == 0:
        return sorted(p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p)
    return sorted(
        os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
        for d, _, files in os.walk(root) for f in files
    )


def search(root, found, limit=3):
    """Fills in each finding's files: the base release's files outside
    migrations that mention its name, as written or without underscores,
    ignoring case. For a column, a file must also mention the table, by its
    name or its singular, the way models are often named. Only files that
    git tracks count. Test files sort last."""
    patterns = []
    for f in found:
        table, word = subject(f.probe.name)
        tables = {table, _singular(table)} if table else set()
        patterns.append((
            re.compile(_forms(word), re.IGNORECASE),
            re.compile("|".join(_forms(t) for t in sorted(tables)), re.IGNORECASE) if tables else None,
        ))
    hits = [[] for _ in found]
    for rel in _source_files(root):
        parts = rel.split("/")
        if any(SKIP_DIRS.match(d) for d in parts[:-1]) or SKIP_FILES.search(parts[-1]):
            continue
        path = os.path.join(root, rel)
        try:
            if os.path.getsize(path) > 2_000_000:
                continue
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if b"\0" in data[:1024]:
            continue
        text = data.decode("utf-8", "replace")
        for i, (word, table) in enumerate(patterns):
            if word.search(text) and (table is None or table.search(text)):
                hits[i].append(rel)
    for f, files in zip(found, hits):
        files.sort(key=lambda p: bool(re.search(r"(^|/)(tests?|spec|__tests__)(/|$)|[._-](test|spec)\.", p)))
        f.files, f.mentions = files[:limit], len(files)
