"""Schema graph extraction for the code-graph extractor — ORCHESTRATOR.

Owns the two-pass entry point `_schema_graph` (the public surface `code_graph_extract`
imports), the migration / test-path predicates it drives (`_is_migration`, `_is_test_path`),
and the multi-definer suppression pass (`_suppress_multi_definer_tables`). The per-language
extraction helpers live in cohesive sibling submodules and are RE-EXPORTED from here so
callers and tests that do `import _cg_schema as S` and reach for `S._orm_table_refs` /
`S._strip_sql_comments` / `S._MAX_TABLES` / `S._ORM_CREATEMODEL_RE` are byte-for-byte
unaffected by the split:
  • `_cg_schema_shared` — `_norm_table`, `_SQL_NONTABLE` (used by all three language groups;
    a shared leaf so the ORM→Go import below cannot form a cycle).
  • `_cg_schema_sql`    — raw SQL DDL/DML detectors, column-level extraction, the SQL comment
    stripper, the `_MAX_TABLES` / `_MAX_COLUMNS` output caps, `_norm_col`.
  • `_cg_schema_go`     — Go gorm/ent struct + explicit-name detection (comment-stripped first).
  • `_cg_schema_orm`    — every other ORM/ActiveRecord framework (`_ORM_PATTERNS`, the model-name
    bridges, the block-aware Rails column-DDL detector, the CREATE-class creator patterns).

SCHEMA GRAPH (the differentiation code-only tools structurally miss): a DB table
is a shared resource. A migration that ALTERs `orders` and code that QUERIES `orders`
are coupled with NO code edge between them — Veripsa surfaces it because both edges
point at the same table NAME (res_adj in _claim_adjacency). Static + regex (no SQL
engine, no inference, content-free: table NAMES only, never data or bodies).

COVERAGE: raw DDL (CREATE/ALTER/DROP TABLE, CREATE INDEX/TRIGGER ... ON t) + DML
(INSERT/UPDATE/DELETE, FROM/JOIN guarded by a SELECT) + ORM table declarations. Most
modern apps declare tables through an ORM, not raw SQL — Django (`class Meta: db_table=`
/ model name; migration `model_name=` / `CreateModel`), SQLAlchemy (`__tablename__=`),
JPA (`@Table(name=)` / `@Entity`), Rails Active Record (`create_table :name` /
`self.table_name=` in db/migrate and db/schema.rb), Laravel Eloquent
(`Schema::create('name', ...)` / `Schema::table('name', ...)` / `protected $table=`),
Go gorm (`type T struct { gorm.Model }` / `func (T) TableName() string { return "t" }` /
gorm struct tags), Go ent (`type T struct { ent.Schema }` / `entsql.Annotation{Table: "t"}`),
Ecto/Phoenix (`schema "users" do` in model files; `create table(:users)` /
`alter table(:users)` in priv/repo/migrations/*.exs).
Without ORM coverage the moat silently no-ops on those repos: no `table` node is ever
minted, so the known-table set is empty and even raw embedded queries get dropped in
pass 2. An ORM model that declares table T becomes a `table` node and a shared-resource
participant, so a migration that touches T and code that touches T couple exactly as for
raw SQL.
"""
import os

from _cg_io import _mark_incomplete, _read_capped
from _cg_languages import _GRAMMAR_BY_EXT

# Cross-language primitives (shared leaf — keeps the ORM→Go import cycle-free).
from _cg_schema_shared import _norm_table, _SQL_NONTABLE  # noqa: F401  (re-exported)
# Raw-SQL substrate: DDL/DML detectors, column extraction, comment stripper, output caps.
from _cg_schema_sql import (  # noqa: F401  (re-exported — _MAX_TABLES/_MAX_COLUMNS/_strip_sql_comments are part of the public surface)
    _DDL_RE, _INDEX_RE, _TRIGGER_RE,
    _MAX_TABLES, _MAX_COLUMNS,
    _norm_col,
    _ddl_columns_from_create, _ddl_columns_from_alter,
    _sql_update_col_edges, _sql_select_col_edges,
    _sql_query_edges, _sql_creator_refs,
    _strip_sql_comments,
)
# ORM / ActiveRecord substrate (also pulls in the Go path via _cg_schema_orm → _cg_schema_go).
# _ORM_CREATEMODEL_RE is re-exported because tests reach for _cg_schema._ORM_CREATEMODEL_RE.
from _cg_schema_orm import (  # noqa: F401  (re-exported)
    _orm_table_refs, _orm_creator_refs,
    _ORM_CREATEMODEL_RE,
)

_MAX_SQL_PHYSICAL_LINE_BYTES = 250_000

# Directories skipped during walk — must match the set in code_graph_extract.
# Imported lazily to avoid a cycle: _schema_graph receives _SKIP_DIRS from the
# caller (build_graph) rather than importing it directly.


# ---- FIX 1: test-path guard (PRECISION) ----
# Test files that define an ORM model (``__tablename__``, ``@Entity``, ``models.Model``,
# ``create_table``, ``table=True``) enter the known-table set if we allow them to mint
# table nodes. A production migration touching the same table name then false-couples to
# those test files → false PAUSE. Measured: SQLAlchemy 6,284 spurious test-prod pairs;
# TypeORM 1,336; Django 275.
#
# RECALL-SAFE: test files still participate in pass-2 (the known-table guard). A test that
# writes raw SQL (``INSERT INTO users …``) is a genuine consumer of the ``users`` table and
# should still couple to the production migration that ALTERs it. Only the TABLE DECLARATION
# (ORM minting = adding the file to the known-table set) is skipped for test files.
#
# Segments that identify a test directory — mirror the standard test dir convention used by
# pytest / Jest / RSpec / Jasmine / Cypress / Playwright.
_TEST_SEGS = frozenset({
    "test", "tests", "spec", "specs", "fixtures", "e2e", "__tests__",
})

def _is_test_path(rel):
    """True when ``rel`` (a repo-relative POSIX path) lives inside a test directory.
    Checks every path segment so that nested test dirs (``src/tests/``, ``backend/spec/``,
    ``packages/ui/__tests__/``) are caught regardless of depth. File-name-level test
    markers (``_test.go``, ``test_foo.py``, ``foo.spec.ts``) are intentionally excluded:
    a source file whose NAME contains "test" is often a helper that also declares a real
    ORM model, and false-filtering it would be a recall loss. We only filter DIRECTORY
    segments, which are an unambiguous declaration that the whole subtree is test code."""
    normed = rel.replace("\\", "/").lower()
    # Split on "/" to get individual path segments; check each against the test-segment set.
    # Filename is the last segment — we deliberately INCLUDE it in the check because a
    # directory-level test segment is still a directory segment (e.g. ``tests/models.py``
    # → segments are ["tests", "models.py"] → "tests" is in _TEST_SEGS → True).
    # The filename itself (``models.py``) is NOT in _TEST_SEGS so it does not filter alone.
    return any(seg in _TEST_SEGS for seg in normed.split("/")[:-1])


def _is_migration(rel):
    """True if this code path looks like a DB migration (a Django/Alembic/Rails-style
    migrations dir, or a name that screams migration). A migration is an `alters` source
    (it changes the schema); other code that touches the same table is a `queries` source.
    The two couple iff they share a table name — the moat catch."""
    low = rel.replace("\\", "/").lower()
    # `/seg/` matches a mid-path segment; `seg/` (startswith) matches the same segment at
    # the repo root — a migrations / alembic-versions dir is a migration whether or not it
    # is nested. (Bare `"/alembic/versions/" in low` would silently miss a root-level
    # `alembic/versions/…`.)
    if any(f"/{seg}/" in low or low.startswith(f"{seg}/")
           for seg in ("migrations", "migrate", "alembic/versions", "db/migrate")):
        return True
    base = os.path.basename(low)
    return base.startswith("migration") or base.startswith("migrate_") or "_migration." in base


def _read_sql_text_bounded(
    path,
    incomplete_paths_out=None,
    relative_path=None,
):
    """Read SQL text while dropping pathological single physical lines.

    Large line-broken schema dumps are legitimate and valuable for recall, but one multi-MB line is usually
    minified/data/comment/ReDoS bait and makes the raw SQL regex layer spend seconds to minutes for little
    signal. This keeps the surrounding DDL and degrades only the overlong line to "not structurally analyzed"."""
    kept = []
    with open(path, "rb") as fh:
        for raw in fh:
            if len(raw) > _MAX_SQL_PHYSICAL_LINE_BYTES:
                _mark_incomplete(incomplete_paths_out, relative_path)
                continue
            kept.append(raw)
    return b"".join(kept).decode("utf-8", "replace")


def _schema_graph(root, source_files, incomplete_paths_out=None):
    """Backward-compatible diagnostics wrapper for schema extraction."""
    try:
        return _schema_graph_impl(
            root,
            source_files,
            incomplete_paths_out=incomplete_paths_out,
        )
    except Exception:
        for path, ext in source_files:
            if ext == ".sql" or ext == ".py" or ext == ".prisma" or ext in _GRAMMAR_BY_EXT:
                _mark_incomplete(
                    incomplete_paths_out,
                    os.path.relpath(path, root).replace(os.sep, "/"),
                )
        return [], []


def _schema_graph_impl(root, source_files, incomplete_paths_out=None):
    """Return (schema_nodes, schema_edges). Two passes for PRECISION (the toy-gate's
    silent failure mode): (1) .sql DDL + ORM declarations → table nodes + the
    KNOWN-TABLE set, plus an `alters` edge from each migration and a `queries` edge from
    each ORM model that declares a table; (2) code that QUERIES a KNOWN table →
    `queries` edges. A 'table' must really be declared somewhere (raw DDL or an ORM
    model), so prose like `FROM the` / `from either` in a comment never becomes an edge.

    Pass 1 mints table nodes from BOTH raw `.sql` DDL AND ORM declarations in code —
    most modern apps never write raw DDL, so without the ORM source the known-table set
    is empty and the whole moat no-ops (even raw embedded queries get dropped in pass 2).
    A `.sql` file is the `alters` source (the migration); it is NOT a `queries` source
    (the schema does not 'consume' itself = the 51 self-ref edges that were pure noise).
    For ORM/code, a migration file is an `alters` source and a non-migration model file a
    `queries` source — they couple iff they name the same table.

    COLUMN GRANULARITY (additive — table nodes/edges are BYTE-FOR-BYTE UNCHANGED):
    Where columns are CLEANLY KNOWN from raw DDL, column nodes + column-granular edges
    are also minted.  Two DDL forms are unambiguous:
      (a) CREATE TABLE t (col type, ...)  — every non-constraint entry is a column
      (b) ALTER TABLE t ADD COLUMN col    — an explicit column addition
    For SQL queries, only UPDATE t SET col=... and SELECT col1,col2 FROM t (no star)
    emit column-level `queries` edges.  SELECT * / ORM calls / bare attribute access →
    column edges are NOT emitted (recall-safe: the table-level edge still couples).
    Column edges are ONLY emitted to KNOWN tables (pass-2 guard mirrors the table-level
    gate).  The per-table _MAX_COLUMNS cap makes this bounded and never-crash.

    `source_files` is the (path, ext) list build_graph ALREADY filtered through the source-path
    guards (size cap, NUL-binary, generated/vendored, symlinks). The pass reads ONLY that list —
    it does NOT re-walk the tree, so an oversized / binary-renamed / generated .sql/.py can no
    longer be read + regex-scanned in FULL outside those guards (the old independent os.walk did).

    PRECISION (Fix 1): ORM table MINTING is skipped for files under a test directory
    (`test/`, `tests/`, `spec/`, `specs/`, `fixtures/`, `e2e/`, `__tests__/`). A test-file
    ORM model entering the known-table set causes every production migration touching the
    same table name to false-couple to the test file → false PAUSE. Test files are still
    included in pass-2 so raw SQL in tests (INSERT INTO …) still couples to production
    migrations (recall-safe). Measured: 6,284 / 1,336 / 275 spurious pairs eliminated for
    SQLAlchemy / TypeORM / Django repos with large test suites.

    RECALL (Fix 2): Three new ORM families added to _ORM_PATTERNS: SQLModel (`table=True`),
    Alembic (`op.create_table` / `op.add_column`), EF Core (`migrationBuilder.*` named-arg
    forms + Fluent `.ToTable()`). Previously those families produced 0 table nodes → the
    cross-substrate crown jewel was invisible for those repos.

    PERF (Fix 3): pass-2 is short-circuited when tableset is empty after pass-1 (no table
    declared anywhere). Empty tableset → no queries edge can survive the known-table guard →
    the entire pass-2 re-read is wasted work. One guard. Measured: 8.5s wasted on k8s repos."""
    nodes, edges = [], []
    tableset = set()
    code_files = []
    table_nodes = {}   # t -> node dict (mint each table node once; first declarer wins path)
    # column_nodes: (table, col) -> node dict  (mint each column node once)
    column_nodes = {}
    # per-table column count for the _MAX_COLUMNS cap
    col_count = {}   # table -> int
    # MULTI-DEFINER tracking (precision, mirrors the sibling P0 guards: openapi #329 / iac #330 /
    # api-contract #332 / config #339 / routes #340 — adapted for the table CREATE-then-ALTER
    # lifecycle). Per table, the set of DISTINCT files that CREATE it (bring it into existence),
    # split BY ROLE so the legitimate migration<->model "moat" pair is never mistaken for redundant
    # creation:
    #   sql_creators[t]    = files that CREATE t as a MIGRATION/DDL source: raw `CREATE TABLE t` in
    #                        `.sql`, OR an ORM migration file with a CREATE-class op (Schema::create,
    #                        create_table, op.create_table, CreateModel, migrationBuilder.CreateTable…).
    #   model_creators[t]  = files that DECLARE/OWN t as a NON-migration ORM model (`__tablename__`,
    #                        `@Entity`, a gorm/ent struct, Prisma `model`, …).
    # CRITICAL (recall): only CREATORS are counted. A migration that merely ALTERs an existing table
    # (add_column / Schema::table / op.add_column / CREATE INDEX / raw ALTER TABLE) is NOT a creator,
    # so the NORMAL one-CREATE-many-ALTER migration lifecycle keeps a table SINGLE-creator and its
    # couplings intact (MEASURED: counting all touchers wrongly suppressed the Laravel 1-create+1-alter
    # `users` fixture). A pure QUERIER (raw `SELECT … FROM t`) is never a creator. A table is AMBIGUOUS
    # only when it has >1 sql-creator OR >1 model-creator: two migrations both CREATE-ing `orders`, or
    # two ORM models both mapping `orders`, are redundant same-role definitions whose tables merely
    # share a NAME — coupling every querier to ALL of them (and the creators to each other) is the
    # false fan-out the siblings measured. ONE create migration + ONE model (1 sql-creator + 1
    # model-creator) is the crown-jewel moat pair, NOT ambiguous → it survives untouched.
    sql_creators = {}    # t -> set(files)
    model_creators = {}  # t -> set(files)
    schema_loss = set()

    def _flush_schema_loss():
        if not schema_loss:
            return
        for lost_path in schema_loss:
            _mark_incomplete(incomplete_paths_out, lost_path)
        # A dropped definition can be referenced by any schema candidate.
        for candidate_path, candidate_ext in source_files:
            if (
                candidate_ext == ".sql"
                or candidate_ext == ".py"
                or candidate_ext == ".prisma"
                or candidate_ext in _GRAMMAR_BY_EXT
            ):
                _mark_incomplete(
                    incomplete_paths_out,
                    os.path.relpath(candidate_path, root).replace(os.sep, "/"),
                )

    def _mint(t, rel, language):
        """Mint table node `t` once (first declarer wins the path). Returns True if `t` is a
        KNOWN table after this call (already minted, or newly minted under the cap), False if it
        was refused because the per-repo table ceiling is hit. Callers append a table edge only
        when this is True, so a capped-out flood produces neither orphan nodes nor orphan edges."""
        if t in table_nodes:
            return True
        if len(table_nodes) >= _MAX_TABLES:
            _mark_incomplete(schema_loss, rel)
            return False
        n = {"id": f"table::{t}", "kind": "table", "name": t, "path": rel, "language": language}
        table_nodes[t] = n
        nodes.append(n)
        tableset.add(t)
        return True

    def _mint_col(t, c, rel, language, table_edge_kind):
        """Mint column node (t, c) once. Emit a column-granular edge from `rel` to the column.
        Only called when the table is KNOWN (_mint returned True) so no orphan column nodes.
        Bounded by _MAX_COLUMNS per table. Never-crash (exceptions silently ignored).

        Edge kind: `alters_col` for DDL sources, `queries_col` for query sources — distinct from
        the table-level `alters`/`queries` kinds so that existing gates that count table-level
        alters/queries edges are unaffected (the column layer is purely additive and the coupling
        layer can independently consume `alters_col`/`queries_col` + column nodes)."""
        try:
            if col_count.get(t, 0) >= _MAX_COLUMNS:
                _mark_incomplete(schema_loss, rel)
                return
            key = (t, c)
            if key not in column_nodes:
                col_count[t] = col_count.get(t, 0) + 1
                col_id = f"column::{t}.{c}"
                n = {"id": col_id, "kind": "column", "name": c, "table": t,
                     "path": rel, "language": language}
                column_nodes[key] = n
                nodes.append(n)
            # Use distinct kinds for column-granular edges so table-level edge counts are stable.
            col_kind = "alters_col" if table_edge_kind == "alters" else "queries_col"
            edges.append({"src": rel, "dst": f"{t}.{c}", "kind": col_kind})
        except Exception:
            _mark_incomplete(schema_loss, rel)

    for path, ext in source_files:
        if not (ext == ".sql" or ext == ".py" or ext == ".prisma" or ext in _GRAMMAR_BY_EXT):
            continue
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        try:
            if ext == ".sql":
                text = _read_sql_text_bounded(path, schema_loss, rel)
            else:
                text = _read_capped(path, schema_loss, rel)
                if text is None:
                    continue
        except OSError:
            _mark_incomplete(schema_loss, rel)
            continue
        if ext == ".sql":
            nodes.append({"id": rel, "kind": "file", "path": rel, "language": "sql"})
            sql_text = _strip_sql_comments(text)   # commented-out / rollback DDL must not mint ghost tables
            # CREATE-class subset of this .sql file's DDL → the tables this file BRINGS INTO EXISTENCE
            # (a creator). ALTER/DROP/INDEX/TRIGGER touches below are NOT creators (recall: a later
            # ALTER migration must not count as a redundant definer of an existing table).
            sql_created = _sql_creator_refs(sql_text)
            for t in sql_created:
                sql_creators.setdefault(t, set()).add(rel)
            seen = set()
            for rx in (_DDL_RE, _INDEX_RE, _TRIGGER_RE):
                for m in rx.finditer(sql_text):
                    t = _norm_table(m.group(1))
                    if t and t not in _SQL_NONTABLE and t not in seen:
                        seen.add(t)
                        if _mint(t, rel, "sql"):
                            edges.append({"src": rel, "dst": t, "kind": "alters"})
            # ADDITIVE: column-level DDL extraction from CREATE TABLE + ALTER ADD COLUMN.
            # Table-level edges above are UNCHANGED; these are ADDITIONAL column edges.
            for t, c in _ddl_columns_from_create(sql_text):
                if t in tableset:
                    _mint_col(t, c, rel, "sql", "alters")
            for t, c in _ddl_columns_from_alter(sql_text):
                if t in tableset:
                    _mint_col(t, c, rel, "sql", "alters")
        else:
            # ORM declarations: mint a table node + a participant edge. A migration
            # ALTERs; a model / other file QUERIES — they couple on the shared name.
            #
            # FIX 1 (PRECISION): skip ORM TABLE MINTING for test files. A test file that
            # declares an ORM model enters the known-table set, causing every production
            # migration touching the same table to false-couple to it → false PAUSE.
            # Measured: SQLAlchemy 6,284 / TypeORM 1,336 / Django 275 spurious pairs.
            # RECALL-SAFE: the file is still appended to code_files so pass-2 raw SQL
            # queries in test files still couple to production migrations (a test that
            # writes INSERT INTO users … is a real consumer of the users schema).
            if not _is_test_path(rel):
                orm_tables = _orm_table_refs(text)
                if orm_tables:
                    is_mig = _is_migration(rel)
                    kind = "alters" if is_mig else "queries"
                    lang = _GRAMMAR_BY_EXT.get(ext, "python" if ext == ".py" else None)
                    for t in orm_tables:
                        if _mint(t, rel, lang):
                            edges.append({"src": rel, "dst": t, "kind": kind})
                    # Role-split CREATOR tracking (only CREATE-class declarations count). An ORM
                    # MIGRATION file's CREATE-class ops (CreateModel / Schema::create / op.create_table
                    # / create_table / migrationBuilder.CreateTable …) → sql_creators; a NON-migration
                    # ORM MODEL that OWNS the table (__tablename__ / @Entity / gorm struct …) →
                    # model_creators. ALTER-class ops (AddField / Schema::table / add_column) are NOT
                    # creators, so a later ALTER migration keeps the table single-creator (recall-safe).
                    creators = _orm_creator_refs(text)
                    if creators:
                        target = sql_creators if is_mig else model_creators
                        for t in creators:
                            if t in table_nodes:   # only track creators of tables we actually minted
                                target.setdefault(t, set()).add(rel)
            code_files.append((rel, text))
    # FIX 3 (PERF): if no table was declared anywhere in pass-1, the tableset is empty and
    # no pass-2 `queries` edge can ever survive the `if e["dst"] in tableset` guard. Skip
    # the entire pass-2 re-read. One guard; never-crash; recall-safe (empty tableset →
    # no queries edges possible regardless). Measured 8.5s wasted on k8s repos.
    if not tableset:
        _flush_schema_loss()
        return nodes, edges
    for rel, text in code_files:   # pass 2: code → queries to KNOWN tables only
        for e in _sql_query_edges(rel, text):
            if e["dst"] in tableset:
                edges.append(e)
        # ADDITIVE: column-level query edges from explicit SQL in code files.
        # Only emitted to KNOWN tables (same pass-2 guard as table-level edges).
        # SELECT * / ORM-only files / bare attribute access → no column edge (recall-safe).
        for t, c in _sql_update_col_edges(rel, text):
            if t in tableset:
                _mint_col(t, c, rel, None, "queries")
        for t, c in _sql_select_col_edges(rel, text):
            if t in tableset:
                _mint_col(t, c, rel, None, "queries")
    _flush_schema_loss()
    return _suppress_multi_definer_tables(
        nodes,
        edges,
        sql_creators,
        model_creators,
        incomplete_paths_out=incomplete_paths_out,
    )


def _suppress_multi_definer_tables(
    nodes,
    edges,
    sql_creators,
    model_creators,
    incomplete_paths_out=None,
):
    """MULTI-DEFINER SUPPRESSION (precision; mirrors openapi #329 / iac #330 / api-contract #332 /
    config #339 / routes #340 — adapted for the table CREATE-then-ALTER lifecycle). A table is
    AMBIGUOUS when >1 file CREATES it in the SAME role — >1 migration/DDL source that CREATEs it
    (sql_creators) OR >1 non-migration ORM model that owns it (model_creators). The downstream
    shared-resource adjacency (db/schema res_adj) couples any two files that both carry an
    alters/queries edge to the same table NAME, so an ambiguous table would couple every querier to
    ALL of its redundant creators AND the creators to each other — the false fan-out the siblings
    measured (66-91% of their false couples). Those definitions merely share a name; they are not one
    shared resource, so an ambiguous table anchors NO cross-file coupling.

    Only CREATORS count — a migration that merely ALTERs an existing table is not a creator, so the
    normal one-CREATE-many-ALTER migration lifecycle (every evolved table in a real Laravel/Rails/
    Django/Alembic repo) keeps a table SINGLE-creator and FULLY coupled (the recall-critical guard).

    We retain every alters/queries/alters_col/queries_col EDGE whose destination is an ambiguous table
    (table-level dst == t; column-level dst == "t.col") with
    ``reference_status="ambiguous"``. The table NODE (and its column nodes) are also KEPT and marked —
    the table genuinely exists. Persistence and observability can therefore account for the complete
    evidence while effective adjacency excludes the ambiguous edges and degrades affected paths to
    Unknown instead of either false-coupling or silently clearing them.

    RECALL-SAFE on four counts: (1) a SINGLE-creator table (the common case) is never ambiguous and is
    untouched; (2) a table CREATE-d once and ALTER-ed by many later migrations stays single-creator and
    fully coupled; (3) the legitimate crown-jewel pair — ONE create migration + ONE ORM model that maps
    it — has exactly 1 sql-creator AND 1 model-creator, so it is NOT ambiguous and the moat coupling
    survives; (4) test/fixture-dir files are already excluded as ORM definers upstream (Fix 1), so a
    real table shadowed by a test factory stays single-creator and KEEPS its coupling, while a test file
    that merely QUERIES a real table (raw SQL in pass 2) is not a creator and is unaffected.

    Never-crash: pure dict/list filtering; any unexpected shape degrades to returning the inputs
    unchanged (the pre-guard behaviour — never a crash of build_graph)."""
    try:
        ambiguous = {
            t for t in (set(sql_creators) | set(model_creators))
            if len(sql_creators.get(t, ())) > 1 or len(model_creators.get(t, ())) > 1
        }
        if not ambiguous:
            return nodes, edges
        for node in nodes:
            table = (
                node.get("name")
                if node.get("kind") == "table"
                else node.get("table")
                if node.get("kind") == "column"
                else None
            )
            if table in ambiguous:
                node["ambiguous"] = True
        _COUPLING_KINDS = ("alters", "queries", "alters_col", "queries_col")

        def _edge_table(e):
            """The table NAME an alters/queries edge anchors on. Table-level dst is the table name
            itself; column-level dst is "table.col" (the table name carries no dot — _norm_table took
            the last dotted segment — so the FIRST dot splits off the table)."""
            dst = e.get("dst")
            if not isinstance(dst, str):
                return None
            if e.get("kind") in ("alters_col", "queries_col"):
                return dst.split(".", 1)[0]
            return dst

        kept_edges = [
            (
                {**e, "reference_status": "ambiguous"}
                if (
                    e.get("kind") in _COUPLING_KINDS
                    and _edge_table(e) in ambiguous
                )
                else e
            )
            for e in edges
        ]
        return nodes, kept_edges
    except Exception:
        # A precision guard must never abort build_graph — degrade to the un-suppressed graph.
        for node in nodes:
            _mark_incomplete(incomplete_paths_out, node.get("path"))
        for edge in edges:
            _mark_incomplete(incomplete_paths_out, edge.get("src"))
        return nodes, edges
