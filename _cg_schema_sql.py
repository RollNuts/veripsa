"""SQL DDL/DML schema extraction for the code-graph extractor.

Owns the raw-SQL substrate of the schema graph: the CREATE/ALTER/DROP TABLE + CREATE
INDEX/TRIGGER detectors (`_DDL_RE`, `_INDEX_RE`, `_TRIGGER_RE`), the DML table-reference
detectors (`_DML_ALWAYS`, `_DML_IF_SELECT`), column-level DDL/query extraction, the raw
`CREATE TABLE` creator detector, the SQL comment stripper, and the per-repo / per-table
output caps (`_MAX_TABLES`, `_MAX_COLUMNS`). Also owns `_norm_col` (column-name normalizer)
— it is used ONLY by the SQL column extractors, so it lives with them.

Split out of the former god-file `_cg_schema.py` as a PURE behaviour-preserving move (no
logic / regex / signature change). `_norm_table` and `_SQL_NONTABLE` are shared across all
language groups and are imported from `_cg_schema_shared`. Content-free: table/column NAMES
only, never data or bodies.
"""
import re

from _cg_schema_shared import _norm_table, _SQL_NONTABLE

_DDL_RE = re.compile(
    r'\b(?:create|alter|drop)\s+table\s+(?:if\s+(?:not\s+)?exists\s+)?(?:only\s+)?'
    r'[`"\[]?([A-Za-z_][\w.]*)', re.I)
# CREATE INDEX ... ON <table> — a migration that indexes `orders` TOUCHES the orders
# table (it is an `alters` of that shared resource), but it never names `orders` in a
# CREATE/ALTER/DROP TABLE clause, so _DDL_RE misses it entirely. We capture the table
# AFTER `ON` (skipping the optional index name), handling UNIQUE / CONCURRENTLY /
# IF NOT EXISTS / schema-qualified / quoted, and the Postgres anonymous-index form
# (`CREATE INDEX ON orders ...`, no index name).
_INDEX_RE = re.compile(
    r'\bcreate\s+(?:unique\s+)?index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?'
    r'(?:[A-Za-z_][\w.]*\s+)?on\s+(?:only\s+)?[`"\[]?([A-Za-z_][\w.]*)', re.I)
# CREATE TRIGGER ... ON <table> — a migration that defines a trigger on `orders` TOUCHES the
# orders table (the trigger fires on its writes — an `alters` of that shared resource), but it
# never names `orders` in a CREATE/ALTER/DROP TABLE clause, so _DDL_RE misses it. We capture the
# table AFTER `ON`, handling OR REPLACE / CONSTRAINT / multi-line bodies / schema-qualified /
# quoted. The `[^;]*?` between the trigger name and `ON` is bounded by `;` so a malformed
# `CREATE TRIGGER x;` can never leak the next statement's `ON <table>` into this match, and the
# trigger NAME itself is never mistaken for a table (it precedes the timing/event clause).
_TRIGGER_RE = re.compile(
    r'\bcreate\s+(?:or\s+replace\s+)?(?:constraint\s+)?trigger\s+[A-Za-z_][\w.]*\b'
    r'[^;]*?\bon\s+(?:only\s+)?[`"\[]?([A-Za-z_][\w.]*)', re.I | re.S)
# Bound on DISTINCT table nodes minted from one repo's schema pass (the schema analogue of
# _FILE_SIZE_CAP). The schema pass walks the tree itself (outside build_graph's per-file size
# cap / binary skip) and reads each .sql / source file in FULL, so a single attacker/customer
# `.sql` UNDER the size cap, packed with distinct `CREATE TABLE t1 … tN;`, would otherwise mint
# N table nodes + N edges with no ceiling — an unbounded-output blowup of the whole tenant's
# graph (memory, and the O(pairs) shared-resource adjacency downstream). Real schemas run
# hundreds → low thousands of tables, so this ceiling never clips a genuine repo; it only
# truncates a flood. Degrade gracefully: keep the first _MAX_TABLES distinct tables
# (deterministic, walk-order), then stop minting (edges to un-minted tables are dropped).
_MAX_TABLES = 20_000
# Per-table ceiling on DISTINCT column nodes minted during DDL extraction. Real tables
# rarely exceed a few hundred columns; a pathological CREATE TABLE with thousands of columns
# would otherwise cause an unbounded-output blowup. Degrade gracefully: keep the first
# _MAX_COLUMNS per table (deterministic, declaration order), then stop minting column nodes
# and edges for that table's overflow. Table-level edges are unaffected.
_MAX_COLUMNS = 500

# ---- COLUMN-LEVEL DDL extraction (additive — table nodes/edges are UNCHANGED) ----
# ALTER TABLE t ADD COLUMN col type — captures (table_name, col_name). ADD COLUMN is the only
# reliable DDL form for column extraction from ALTER TABLE: RENAME COLUMN and DROP COLUMN touch
# the column but are not "creating" a column node (they change or remove existing structure),
# and ADD COLUMN is the clearest signal that this table-file now owns a new column edge.
# IF NOT EXISTS variant handled by optional group.
_ALTER_ADD_COL_RE = re.compile(
    r'\balter\s+table\s+(?:if\s+(?:not\s+)?exists\s+)?(?:only\s+)?'
    r'[`"\[]?([A-Za-z_][\w.]*)[`"\]]?\s+'
    r'add\s+(?:column\s+)?(?:if\s+not\s+exists\s+)?[`"\[]?([A-Za-z_]\w*)',
    re.I)

# CREATE TABLE t (col1 type, col2 type, ...) — extract column names from the inline definition
# block.  Two-step: first capture the column-list body (everything between the outermost
# parentheses after the table name, bounded to 8192 chars to stay bounded on pathological DDL),
# then parse individual column entries.  Precision guard: only the FIRST token of each
# comma-separated entry is taken as the column name (the type follows), and entries that start
# with a SQL keyword (CONSTRAINT / PRIMARY / UNIQUE / FOREIGN / CHECK / INDEX / KEY / LIKE)
# are skipped — they are constraint/index clauses, not column names.  The bound on the body
# capture (8192 chars) and the per-table _MAX_COLUMNS cap make this never-crash.
_CREATE_TABLE_BODY_RE = re.compile(
    r'\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?(?:only\s+)?'
    r'[`"\[]?([A-Za-z_][\w.]*)[`"\]]?\s*\((.{1,8192}?)\)\s*(?:;|$|\bWITHOUT\b|\bSTRICT\b)',
    re.I | re.S)
# SQL keywords that begin a table-level CONSTRAINT or clause inside a CREATE TABLE body —
# entries starting with these are NOT column names.
_CREATE_TABLE_SKIP = frozenset({
    "constraint", "primary", "unique", "foreign", "check", "index", "key", "like",
    "exclude", "period",
})
# Single column name token from a CREATE TABLE entry (after stripping leading whitespace/quotes).
_COL_NAME_RE = re.compile(r'^[`"\[]?([A-Za-z_]\w*)', re.I)

# ---- COLUMN-LEVEL SQL query extraction (additive — table-level edges UNCHANGED) ----
# Only emit column edges when the column is EXPLICITLY named in a SQL context AND can be
# table-tied.  We restrict to two unambiguous forms:
#   (A) UPDATE t SET col = ...   — the table is explicit; the SET clause names the columns
#   (B) SELECT col1, col2 FROM t — explicit column list (NOT SELECT *); tied to the FROM table
# SELECT * / ORM calls without explicit column names / bare attribute access → no column edge.
# Precision-first: false column edges would corrupt the pause-tier's action_required judgment.

# UPDATE t SET col1 = ..., col2 = ... — captures (table, col) pairs.  The table comes from
# the existing _DML_ALWAYS UPDATE pattern; here we extract the SET clause columns.  Two-step:
# first match the full UPDATE ... SET ... clause (bounded to 1024 chars), then extract column
# names from the SET list.
_UPDATE_SET_RE = re.compile(
    r'\bupdate\s+[`"\[]?([A-Za-z_][\w.]*)[`"\]]?\s+set\s+(.{1,1024}?)(?=\bwhere\b|\bret'
    r'urning\b|;|$)', re.I | re.S)
# A single `col = value` assignment from a SET clause — captures the column name.
_SET_COL_RE = re.compile(r'[`"\[]?([A-Za-z_]\w*)[`"\]]?\s*=', re.I)

# SELECT col1, col2 FROM t — captures the explicit column list and the FROM table.
# Precision guards: (1) no match on SELECT * (the star or subquery form); (2) the column
# list is taken as the text between SELECT and FROM (bounded to 512 chars); (3) each token
# in the list must be a simple identifier (no dot-qualified, no function call) — alias forms
# like `col AS alias` are accepted (we take the first token before AS); (4) items containing
# `(` are skipped (they are function calls, not bare columns).
_SELECT_COLS_FROM_RE = re.compile(
    r'\bselect\s+(?!(?:\*|\bcount\b|\bdistinct\s+\*))(.{1,512}?)\s+from\s+'
    r'[`"\[]?([A-Za-z_][\w.]*)',
    re.I | re.S)

_DML_ALWAYS = [
    re.compile(r'\binsert\s+into\s+[`"\[]?([A-Za-z_][\w.]*)', re.I),
    re.compile(r'\bdelete\s+from\s+[`"\[]?([A-Za-z_][\w.]*)', re.I),
    re.compile(r'\bupdate\s+[`"\[]?([A-Za-z_][\w.]*)[`"\]]?\s+set\b', re.I),
]
# FROM/JOIN are only counted when the text actually contains a SELECT — so Python
# `from x import` (no SELECT nearby) never false-matches a "table". The negative
# lookahead on FROM is a second guard.
_DML_IF_SELECT = [
    re.compile(r'\bfrom\s+[`"\[]?([A-Za-z_][\w.]*)(?![\w.])(?!\s+import)', re.I),
    re.compile(r'\bjoin\s+[`"\[]?([A-Za-z_][\w.]*)', re.I),
]


def _norm_col(name):
    """Column name normalized: lowercased, quotes/brackets stripped. Returns None if the
    result is empty or a SQL keyword that cannot be a column name."""
    if not name:
        return None
    n = name.strip().strip('`"[]').lower()
    return n if n and n not in _SQL_NONTABLE else None


def _ddl_columns_from_create(text):
    """Extract (table_name, [col_name, ...]) pairs from CREATE TABLE ... ( col_list )
    statements in `text`.  Precision-first: only the first identifier of each
    comma-separated entry is taken as the column name; entries starting with SQL
    constraint keywords are skipped.  Bounded by _MAX_COLUMNS per table.
    Returns a list of (table_str, col_str) tuples (both already normalized)."""
    out = []
    for m in _CREATE_TABLE_BODY_RE.finditer(text):
        t = _norm_table(m.group(1))
        if not t or t in _SQL_NONTABLE:
            continue
        body = m.group(2)
        # Split on commas that are NOT inside nested parentheses (handles type args like
        # DECIMAL(10,2)).  We walk character-by-character with a paren depth counter.
        entries = []
        depth = 0
        start = 0
        for i, ch in enumerate(body):
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            elif ch == ',' and depth == 0:
                entries.append(body[start:i])
                start = i + 1
        entries.append(body[start:])
        count = 0
        for entry in entries:
            if count >= _MAX_COLUMNS:
                break
            stripped = entry.strip()
            if not stripped:
                continue
            # Skip constraint / index / clause entries by their first keyword.
            first_word = stripped.split()[0].strip('`"[]').lower() if stripped.split() else ""
            if first_word in _CREATE_TABLE_SKIP:
                continue
            cm = _COL_NAME_RE.match(stripped)
            if not cm:
                continue
            c = _norm_col(cm.group(1))
            if c:
                out.append((t, c))
                count += 1
    return out


def _ddl_columns_from_alter(text):
    """Extract (table_name, col_name) pairs from ALTER TABLE t ADD COLUMN col statements.
    Returns a list of (table_str, col_str) tuples (both already normalized)."""
    out = []
    for m in _ALTER_ADD_COL_RE.finditer(text):
        t = _norm_table(m.group(1))
        c = _norm_col(m.group(2))
        if t and c and t not in _SQL_NONTABLE and c not in _SQL_NONTABLE:
            out.append((t, c))
    return out


def _sql_update_col_edges(rel, text):
    """Extract column-level `queries` edges from UPDATE t SET col=... statements.
    Only emits edges when the table is explicit (from the UPDATE clause) and the
    SET columns are simple identifiers.  Returns list of (table, col) tuples."""
    out = []
    for m in _UPDATE_SET_RE.finditer(text):
        t = _norm_table(m.group(1))
        if not t or t in _SQL_NONTABLE:
            continue
        set_clause = m.group(2)
        for cm in _SET_COL_RE.finditer(set_clause):
            c = _norm_col(cm.group(1))
            if c and c not in _SQL_NONTABLE:
                out.append((t, c))
    return out


def _sql_select_col_edges(rel, text):
    """Extract column-level `queries` edges from SELECT col1, col2 FROM t statements.
    Precision guards: SELECT * is excluded; function calls are excluded; only simple
    identifier tokens are accepted.  Returns list of (table, col) tuples."""
    out = []
    for m in _SELECT_COLS_FROM_RE.finditer(text):
        col_list_raw = m.group(1)
        t = _norm_table(m.group(2))
        if not t or t in _SQL_NONTABLE:
            continue
        # Parse the column list: split by comma, take the first token of each entry,
        # skip entries with `(` (function calls), skip `*`, skip AS-aliases.
        for entry in col_list_raw.split(","):
            entry = entry.strip()
            if not entry or '*' in entry or '(' in entry:
                continue
            # Handle `col AS alias` — take the first token (before AS).
            first = entry.split()[0] if entry.split() else ""
            c = _norm_col(first)
            if c and c not in _SQL_NONTABLE:
                out.append((t, c))
    return out


def _sql_query_edges(rel, text):
    """SQL embedded in ANY source file → `queries` edges (file → table NAME).
    Recall-biased but guarded: INSERT/UPDATE/DELETE are SQL-specific; FROM/JOIN count
    only when the file also has a SELECT."""
    pats = list(_DML_ALWAYS)
    if re.search(r'\bselect\b', text, re.I):
        pats += _DML_IF_SELECT
    out, seen = [], set()
    for pat in pats:
        for m in pat.finditer(text):
            t = _norm_table(m.group(1))
            if t and t not in seen:
                seen.add(t)
                out.append({"src": rel, "dst": t, "kind": "queries"})
    return out


# Raw .sql CREATE TABLE — the create-class subset of _DDL_RE (which also matches ALTER/DROP TABLE).
# Anchored on `create table`; ALTER TABLE / DROP TABLE / CREATE INDEX / CREATE TRIGGER are NOT
# creators (they touch an already-existing table).
_CREATE_TABLE_RE = re.compile(
    r'\bcreate\s+table\s+(?:if\s+(?:not\s+)?exists\s+)?(?:only\s+)?'
    r'[`"\[]?([A-Za-z_][\w.]*)', re.I)


def _sql_creator_refs(sql_text):
    """Tables a .sql file CREATES (raw `CREATE TABLE t`), content-free NAMES only. ALTER/DROP/INDEX/
    TRIGGER are excluded — only a CREATE TABLE brings the table into existence."""
    out = set()
    for m in _CREATE_TABLE_RE.finditer(sql_text):
        t = _norm_table(m.group(1))
        if t and t not in _SQL_NONTABLE:
            out.add(t)
    # DETERMINISM: emit in a STABLE order (set str-iteration order varies by PYTHONHASHSEED).
    # The caller feeds this into a per-table creator set (not edge order), but keep it canonical
    # like its sibling ref fns — content-free (sort by table NAME).
    return sorted(out)


# SQL comment stripper — removes `-- line comments` and `/* block comments */` from .sql text BEFORE the DDL
# regex scan, so commented-out / rollback DDL (`-- CREATE TABLE old_orders`, `/* DROP TABLE legacy_users */`)
# — extremely common as inline rollback documentation in migrations — does NOT mint a ghost table node (a
# false table → false coupling → a false PAUSE now that a material coupling blocks). Never-crash; the table
# regexes don't use line numbers so line structure need not be preserved. Quote-awareness is intentionally
# simple: a real DDL table name is never inside a string literal, and a `--`/`/*` inside a string literal in a
# schema file is vanishingly rare — the precision win on commented-out DDL far outweighs that edge case.
_SQL_LINE_COMMENT_RE = re.compile(r'--[^\n]*')
_SQL_BLOCK_COMMENT_RE = re.compile(r'/\*.*?\*/', re.S)


def _strip_sql_comments(text):
    """Strip SQL line (`--`) and block (`/* */`) comments so commented-out DDL never mints a ghost table."""
    return _SQL_BLOCK_COMMENT_RE.sub(" ", _SQL_LINE_COMMENT_RE.sub("", text))
