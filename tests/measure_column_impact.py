#!/usr/bin/env python3
"""MEASURE-FIRST, standalone, content-free: can Veripsa pinpoint COLUMN-level DB
schema-change impact?  i.e. a migration that RENAMES / DROPS / ADDS a column X (of a
specific table) -> the EXACT code files that reference X (and will silently break).

Today Veripsa's schema graph (_cg_schema.py) couples at the TABLE-name level
(`alters`/`queries` on a table NAME).  This analyzer asks the finer, higher-value
question one granularity down: given a CHANGED column (table T, column C), which code
files REFERENCE that column?  And -- the make-or-break -- with what PRECISION?

THE TRAP (same ubiquitous-name trap as the call / config / table-name work): a column
name is often a UBIQUITOUS word (`id`, `name`, `status`, `created_at`, `type`, `value`).
Fanning `created_at` out to every file with a `.created_at` attribute = garbage.  So we
measure THREE resolution rules and the false-positive rate of each:

  (R0) BARE name match      : F references column C if C appears as a `.C` attribute or a
                              bareword SQL token anywhere in F.  (the naive baseline)
  (R1) + ubiquitous guard   : R0 but bare-name matches on a UBIQUITOUS column name are
                              DROPPED (never fan out a `.id` / `.name` / `.status`).
  (R2) + table-qualify      : R1, but a match is QUALIFIED -- F references column C of
                              table T only if F also references table T (the table is in
                              F's schema-table set: an ORM model for T, a `T.objects`
                              manager, a `FROM t`, or `ModelName.C` where ModelName maps to
                              T).  When the table can't be tied to the ref -> DON'T EMIT.

GROUND TRUTH for TRUE/FALSE of a "column C changed -> file F" prediction:
  * PRIMARY (co-change, the honest one): does F actually co-change with the migration that
    changed C?  We compute, per changed column, the git co-change LIFT between the changed
    file set and each predicted F.  A prediction whose file co-changes with the migration at
    ELEVATED lift is corroborated TRUE; one that co-changes at ~RANDOM is FALSE (a name
    collision).  We report mean co-change for predictions each rule KEEPS vs DROPS.
  * SECONDARY (structural, for the hand-labeled sample): a prediction is TRUE if F really
    references column C OF TABLE T (an ORM attribute on T's model / a manager query on T /
    raw SQL on t), FALSE if it's an unrelated `.C` of a DIFFERENT table or a bare ubiquitous
    name collision.  We hand-classify a sample and report the false-positive rate per rule.

Content-free: table + column NAMES + file paths + git co-change COUNTS only.  Never reads
data or stores bodies.  Standalone -- imports nothing from the engine; mirrors only the
public _cg_schema regexes for table resolution so the measurement reflects what the product
would actually see.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import defaultdict

# ---------------------------------------------------------------------------
# Ubiquitous column-name set (same SPIRIT as recall_measure.STOP for calls and the
# schema-precision table guard).  These are column names so generic that a BARE `.name`
# match carries ~no signal; we must qualify them by table or drop them.
UBIQUITOUS_COLUMNS = frozenset({
    "id", "pk", "name", "status", "type", "value", "created_at", "updated_at",
    "created", "updated", "modified", "timestamp", "date", "time", "key", "code",
    "title", "description", "label", "slug", "order", "position", "active",
    "enabled", "deleted", "is_active", "is_deleted", "uuid", "data", "content",
    "url", "path", "email", "user", "owner", "parent", "count", "amount", "price",
    "quantity", "state", "kind", "level", "version", "hash", "token", "metadata",
})

# ---------------------------------------------------------------------------
# COLUMN-LEVEL CHANGES from migrations.  Django ORM ops carry model_name + field name --
# this is the GIFT that makes column changes TABLE-QUALIFIED at the source (model_name=table,
# old_name/new_name/name=column).  Raw DDL ALTER ... RENAME/DROP/ADD COLUMN names the table
# explicitly.  Rails add_column/remove_column/rename_column(:table, :col) too.

# Django: RenameField(model_name="user", old_name="meta", new_name="metadata")
_DJ_RENAME = re.compile(
    r'RenameField\s*\([^)]*?model_name\s*=\s*[\'"]([A-Za-z_]\w*)[\'"][^)]*?'
    r'old_name\s*=\s*[\'"]([A-Za-z_]\w*)[\'"][^)]*?new_name\s*=\s*[\'"]([A-Za-z_]\w*)[\'"]',
    re.S)
# Django: RemoveField(model_name="user", name="meta")
_DJ_REMOVE = re.compile(
    r'RemoveField\s*\([^)]*?model_name\s*=\s*[\'"]([A-Za-z_]\w*)[\'"][^)]*?'
    r'name\s*=\s*[\'"]([A-Za-z_]\w*)[\'"]', re.S)
# Django: AddField(model_name="user", name="metadata", field=...)
_DJ_ADD = re.compile(
    r'AddField\s*\([^)]*?model_name\s*=\s*[\'"]([A-Za-z_]\w*)[\'"][^)]*?'
    r'name\s*=\s*[\'"]([A-Za-z_]\w*)[\'"]', re.S)
# Django: AlterField (a column whose type/constraints changed -- code referencing it may break)
_DJ_ALTER = re.compile(
    r'AlterField\s*\([^)]*?model_name\s*=\s*[\'"]([A-Za-z_]\w*)[\'"][^)]*?'
    r'name\s*=\s*[\'"]([A-Za-z_]\w*)[\'"]', re.S)

# Raw DDL: ALTER TABLE t RENAME COLUMN a TO b / DROP COLUMN c / ADD COLUMN d
_DDL_RENAME_COL = re.compile(
    r'alter\s+table\s+(?:if\s+exists\s+)?[`"\[]?([A-Za-z_][\w.]*)[`"\]]?\s+'
    r'rename\s+column\s+[`"\[]?([A-Za-z_]\w*)[`"\]]?\s+to\s+[`"\[]?([A-Za-z_]\w*)', re.I)
_DDL_DROP_COL = re.compile(
    r'alter\s+table\s+(?:if\s+exists\s+)?[`"\[]?([A-Za-z_][\w.]*)[`"\]]?\s+'
    r'drop\s+column\s+(?:if\s+exists\s+)?[`"\[]?([A-Za-z_]\w*)', re.I)
_DDL_ADD_COL = re.compile(
    r'alter\s+table\s+(?:if\s+exists\s+)?[`"\[]?([A-Za-z_][\w.]*)[`"\]]?\s+'
    r'add\s+column\s+(?:if\s+not\s+exists\s+)?[`"\[]?([A-Za-z_]\w*)', re.I)

# Rails: rename_column :users, :user_id, :account_id / remove_column :users, :legacy
_RB_RENAME_COL = re.compile(
    r'rename_column\s+:?[\'"]?([A-Za-z_]\w*)[\'"]?\s*,\s*:?[\'"]?([A-Za-z_]\w*)[\'"]?'
    r'\s*,\s*:?[\'"]?([A-Za-z_]\w*)')
_RB_REMOVE_COL = re.compile(
    r'remove_column\s+:?[\'"]?([A-Za-z_]\w*)[\'"]?\s*,\s*:?[\'"]?([A-Za-z_]\w*)')
_RB_ADD_COL = re.compile(
    r'add_column\s+:?[\'"]?([A-Za-z_]\w*)[\'"]?\s*,\s*:?[\'"]?([A-Za-z_]\w*)')


def _norm(s):
    return s.strip().strip('`"[]').split(".")[-1].strip('`"[]').lower() if s else None


def column_changes(text):
    """Extract (table, column, change_type) triples from a migration's text.  table+column
    are normalized lowercase.  change_type in {rename, drop, add, alter}.  For a rename, BOTH
    old and new column are emitted (old breaks references, new is what to use)."""
    out = []
    for m in _DJ_RENAME.finditer(text):
        t, old, new = _norm(m.group(1)), _norm(m.group(2)), _norm(m.group(3))
        out.append((t, old, "rename")); out.append((t, new, "rename"))
    for m in _DJ_REMOVE.finditer(text):
        out.append((_norm(m.group(1)), _norm(m.group(2)), "drop"))
    for m in _DJ_ADD.finditer(text):
        out.append((_norm(m.group(1)), _norm(m.group(2)), "add"))
    for m in _DJ_ALTER.finditer(text):
        out.append((_norm(m.group(1)), _norm(m.group(2)), "alter"))
    for m in _DDL_RENAME_COL.finditer(text):
        t = _norm(m.group(1))
        out.append((t, _norm(m.group(2)), "rename")); out.append((t, _norm(m.group(3)), "rename"))
    for m in _DDL_DROP_COL.finditer(text):
        out.append((_norm(m.group(1)), _norm(m.group(2)), "drop"))
    for m in _DDL_ADD_COL.finditer(text):
        out.append((_norm(m.group(1)), _norm(m.group(2)), "add"))
    for m in _RB_RENAME_COL.finditer(text):
        t = _norm(m.group(1))
        out.append((t, _norm(m.group(2)), "rename")); out.append((t, _norm(m.group(3)), "rename"))
    for m in _RB_REMOVE_COL.finditer(text):
        out.append((_norm(m.group(1)), _norm(m.group(2)), "drop"))
    for m in _RB_ADD_COL.finditer(text):
        out.append((_norm(m.group(1)), _norm(m.group(2)), "add"))
    return [(t, c, k) for (t, c, k) in out if t and c]


# ---------------------------------------------------------------------------
# TABLE RESOLUTION in code (mirror of _cg_schema's ORM patterns, kept local so this is
# standalone).  For each code file we build: which TABLES does it touch, and a map
# ModelClassName -> table for ModelName.col disambiguation.
_TABLENAME = re.compile(r'__tablename__\s*=\s*[\'"]([A-Za-z_]\w*)[\'"]')
_DBTABLE = re.compile(r'db_table[\'"]?\s*[:=]\s*[\'"]([A-Za-z_]\w*)[\'"]')
_MODEL_CLASS = re.compile(
    r'\bclass\s+([A-Za-z_]\w*)\s*\(\s*[\w.]*\b(?:models\.Model|db\.Model|Base)\b')
# Django: a model whose table is derived from its class name (lowercased, no db_table).
_FROM = re.compile(r'\bfrom\s+[`"\[]?([A-Za-z_]\w*)', re.I)


def _model_table_candidates(cls):
    """Table-name candidates a migration's `model_name` could equal for a model CLASS named
    `cls`.  Django derives the table from the (lowercased) model name, and the common
    abstract-model idiom names the class `Abstract<Model>` while the concrete table is just
    `<model>` (app-prefixed).  So a migration `model_name="stockrecord"` must tie to class
    `AbstractStockRecord`.  We emit: the full class lowercased, and the class with a leading
    `abstract` stripped -- both are content-free NAMES."""
    low = cls.lower()
    cands = {low}
    if low.startswith("abstract") and len(low) > 8:
        cands.add(low[len("abstract"):])
    return cands


def code_tables(text):
    """The set of table NAMES a code file plausibly touches, + a {ClassName_lower: table}
    map so `User.user_id` can be tied to table `user`.  Django default: a model's table is
    its class name lowercased; abstract-model idiom strips a leading `Abstract`."""
    tables = set()
    classmap = {}
    for m in _TABLENAME.finditer(text):
        tables.add(_norm(m.group(1)))
    for m in _DBTABLE.finditer(text):
        tables.add(_norm(m.group(1)))
    for m in _MODEL_CLASS.finditer(text):
        cls = m.group(1)
        for tbl in _model_table_candidates(cls):
            tables.add(tbl)
            classmap[cls.lower()] = tbl   # last wins; both class forms tie to same class key set below
        # also register the abstract-stripped class name as a lookup alias
        for cand in _model_table_candidates(cls):
            classmap[cand] = cand
    # raw SQL FROM (only if a SELECT is present, like _cg_schema)
    if re.search(r'\bselect\b', text, re.I):
        for m in _FROM.finditer(text):
            tables.add(_norm(m.group(1)))
    return tables, classmap


# ---------------------------------------------------------------------------
# COLUMN REFERENCES in a code file.  We collect:
#   - attribute refs `.<col>` and `obj.<col>` (ORM attribute access)
#   - `ClassName.<col>` (qualified -> ties to a table via classmap)
#   - string/kwarg keys `"<col>"` / `<col>=` in a queryset filter context
#   - raw SQL barewords (SELECT col, WHERE col =) -- only when a SELECT is present
_ATTR = re.compile(r'(?:([A-Za-z_]\w*)\s*\.)?([a-z_]\w*)\b')


def column_refs(text):
    """Return two structures for a file:
       bare_cols   : set of column names referenced as a bare `.col` (no class qualifier)
       qual_cols   : set of (ClassName_lower, col) referenced as `Class.col`
    Content-free: names only."""
    bare = set()
    qual = set()
    # attribute access: optional `Class.` then `.col`.  We only want DOT access, so require
    # a preceding `.`; scan for `<word>.<col>` and `.<col>`.
    for m in re.finditer(r'(?:([A-Za-z_]\w*)\s*\.\s*)([a-z_]\w*)\b', text):
        owner, col = m.group(1), m.group(2)
        if owner and owner[0].isupper():
            qual.add((owner.lower(), col))
        bare.add(col)
    # ALSO `.col` where the owner is a lowercase instance (obj.col) -- already captured above
    # since the owner group is optional-free; the regex requires an owner, so add the leading-dot
    # form explicitly for `self.col` etc (owner lowercase -> stays bare).
    return bare, qual


# ---------------------------------------------------------------------------
# git co-change ground truth.
def changed_files_per_commit(repo, paths_filter=None, max_commits=4000):
    """{commit: set(files)} from `git log --name-only`.  Content-free (paths only)."""
    cmd = ["git", "-C", repo, "log", f"-n{max_commits}", "--no-merges",
           "--pretty=format:__C__%H", "--name-only"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    commits = {}
    cur = None
    for line in out.splitlines():
        if line.startswith("__C__"):
            cur = line[5:]
            commits[cur] = set()
        elif line.strip() and cur is not None:
            commits[cur].add(line.strip())
    return commits


def cochange_lift(commits, file_a, file_b):
    """lift = P(b changed | a changed) / P(b changed).  >1 = a and b co-change above base
    rate; ~1 = unrelated.  Content-free (counts only)."""
    n = len(commits)
    if n == 0:
        return 0.0, 0
    ca = sum(1 for s in commits.values() if file_a in s)
    cb = sum(1 for s in commits.values() if file_b in s)
    cab = sum(1 for s in commits.values() if file_a in s and file_b in s)
    if ca == 0 or cb == 0:
        return 0.0, cab
    p_b = cb / n
    p_b_given_a = cab / ca
    return (p_b_given_a / p_b if p_b else 0.0), cab


# ---------------------------------------------------------------------------
def walk_py(repo):
    for dp, dns, fns in os.walk(repo):
        dns[:] = [d for d in dns if d not in (".git", "node_modules", "__pycache__",
                                              "static", "dist", "build", ".tox")]
        for fn in fns:
            if fn.endswith((".py", ".sql", ".rb")):
                yield os.path.join(dp, fn)


def is_migration(rel):
    low = rel.replace("\\", "/").lower()
    return ("/migrations/" in low or low.startswith("migrations/")
            or "/migrate/" in low or "alembic" in low)


def analyze_repo(repo, verbose=True):
    """Run the full measurement on one repo.  Returns a dict of aggregate stats."""
    repo = os.path.abspath(repo)
    name = os.path.basename(repo)

    # 1. column changes from migrations + the migration files that carry them.
    changes = defaultdict(set)          # (table,col) -> set of migration files (change source)
    change_types = defaultdict(set)
    code_table_sets = {}                # rel -> set(tables)
    code_classmaps = {}                 # rel -> {classlower: table}
    code_bare = {}                      # rel -> set(bare cols)
    code_qual = {}                      # rel -> set((classlower,col))

    for path in walk_py(repo):
        rel = os.path.relpath(path, repo)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if is_migration(rel):
            for (t, c, k) in column_changes(text):
                changes[(t, c)].add(rel)
                change_types[(t, c)].add(k)
        else:
            tables, classmap = code_tables(text)
            if tables or "." in text:
                code_table_sets[rel] = tables
                code_classmaps[rel] = classmap
                bare, qual = column_refs(text)
                code_bare[rel] = bare
                code_qual[rel] = qual

    # column popularity across CODE files (for ubiquity measurement on this repo).
    col_filecount = defaultdict(int)
    for rel, bare in code_bare.items():
        for c in bare:
            col_filecount[c] += 1

    # 2. For each changed column, resolve referencing files under R0/R1/R2.
    #    Build predictions (col_key -> set of files) per rule.
    preds_r0 = defaultdict(set)
    preds_r1 = defaultdict(set)
    preds_r2 = defaultdict(set)
    for (t, c), migs in changes.items():
        for rel, bare in code_bare.items():
            if rel in migs:
                continue
            qual = code_qual.get(rel, set())
            tables = code_table_sets.get(rel, set())
            classmap = code_classmaps.get(rel, {})
            # does this file reference column c at all (bare)?
            bare_hit = c in bare
            # qualified hit: Class.c where Class maps to table t (or class lowercased == t)
            qual_hit = any(col == c and (cls == t or classmap.get(cls) == t)
                           for (cls, col) in qual)
            # table tie: file touches table t
            table_tie = t in tables

            if bare_hit or qual_hit:
                preds_r0[(t, c)].add(rel)                       # R0: bare name anywhere
            # R1: drop bare ubiquitous unless qualified
            if qual_hit or (bare_hit and c not in UBIQUITOUS_COLUMNS):
                preds_r1[(t, c)].add(rel)
            # R2: must be tied to the table (qualified OR file touches table t)
            if qual_hit or (bare_hit and c not in UBIQUITOUS_COLUMNS and table_tie):
                preds_r2[(t, c)].add(rel)

    # 3. co-change ground truth.  Restrict to columns with a manageable prediction fan-out
    #    so the git scan is bounded; sample.
    commits = changed_files_per_commit(repo)

    def rule_cochange(preds, cap_cols=60, cap_files=12):
        """For predictions under a rule, mean co-change lift between each predicted file and
        the migration that changed the column.  Returns (mean_lift, n, frac_elevated)."""
        lifts = []
        cols = list(preds.items())
        cols.sort(key=lambda kv: len(kv[1]))   # smaller fan-outs first (more specific)
        seen = 0
        for (t, c), files in cols:
            if seen >= cap_cols:
                break
            migs = changes[(t, c)]
            mig = sorted(migs)[0] if migs else None
            if not mig:
                continue
            seen += 1
            for f in sorted(files)[:cap_files]:
                lift, _ = cochange_lift(commits, mig, f)
                lifts.append(lift)
        if not lifts:
            return 0.0, 0, 0.0
        mean = sum(lifts) / len(lifts)
        elevated = sum(1 for l in lifts if l >= 2.0) / len(lifts)
        return mean, len(lifts), elevated

    r0 = rule_cochange(preds_r0)
    r1 = rule_cochange(preds_r1)
    r2 = rule_cochange(preds_r2)

    def total_preds(p):
        return sum(len(v) for v in p.values())

    stats = {
        "repo": name,
        "n_changed_columns": len(changes),
        "n_commits_scanned": len(commits),
        "preds_r0": total_preds(preds_r0),
        "preds_r1": total_preds(preds_r1),
        "preds_r2": total_preds(preds_r2),
        "cochange_r0": r0, "cochange_r1": r1, "cochange_r2": r2,
        "ubiquitous_examples": sorted(col_filecount.items(), key=lambda kv: -kv[1])[:8],
    }
    if verbose:
        print(f"\n=== {name} ===")
        print(f"  changed columns (from migrations): {stats['n_changed_columns']}")
        print(f"  commits scanned for co-change:     {stats['n_commits_scanned']}")
        print(f"  predictions (file refs a changed col):")
        print(f"    R0 bare-name           : {stats['preds_r0']:6d}   mean co-change lift {r0[0]:5.2f}  (%elevated {r0[2]*100:4.0f}%, n={r0[1]})")
        print(f"    R1 +ubiquitous-guard   : {stats['preds_r1']:6d}   mean co-change lift {r1[0]:5.2f}  (%elevated {r1[2]*100:4.0f}%, n={r1[1]})")
        print(f"    R2 +table-qualify      : {stats['preds_r2']:6d}   mean co-change lift {r2[0]:5.2f}  (%elevated {r2[2]*100:4.0f}%, n={r2[1]})")
        print(f"  most-ubiquitous column names in code: {[c for c,_ in stats['ubiquitous_examples']]}")
    return stats


if __name__ == "__main__":
    repos = sys.argv[1:]
    if not repos:
        print("usage: measure_column_impact.py <repo> [<repo> ...]")
        sys.exit(2)
    for r in repos:
        analyze_repo(r)
