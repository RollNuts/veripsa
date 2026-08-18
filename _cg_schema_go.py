"""Go ORM (gorm / ent) schema extraction for the code-graph extractor.

Owns the Go substrate of the schema graph: gorm struct detection (`gorm.Model` embed,
gorm struct tags), ent schema detection (`ent.Schema` embed + Fields/Annotations guard),
the explicit-name forms (`TableName()` return, `gorm:"table:…"` tag,
`entsql.Annotation{Table: …}`), the CamelCase→snake_case-plural table-name converter, and
the Go single-line-comment stripper that runs before all of them so doc-comment examples
never mint a table.

These live in their own module (NOT in the generic `_ORM_PATTERNS` list) because the Go
forms must have comments stripped FIRST. Split out of the former god-file `_cg_schema.py`
as a PURE behaviour-preserving move (no logic / regex / signature change). `_norm_table`
and `_SQL_NONTABLE` are shared and imported from `_cg_schema_shared`. Content-free:
struct/table NAMES only.
"""
import re

from _cg_schema_shared import _norm_table, _SQL_NONTABLE

# Go gorm — struct embeds gorm.Model (the canonical ORM marker):
#   `type User struct { gorm.Model; ... }` or `type User struct {\n\tgorm.Model\n`.
# Only fires when the struct BODY (up to 512 chars from the opening brace) contains
# `gorm.Model` as an embedded field — the most reliable gorm marker short of an explicit
# TableName() method. The struct NAME (CamelCase) is extracted; the caller converts it
# to a snake_case plural table candidate via _go_table_name(). Precision guard: the
# embedded field must literally be `gorm.Model` (not `gorm.SomethingElse`), anchored
# at word boundary so `mygorm.Model` does not match.
_GO_GORM_MODEL_RE = re.compile(
    r'\btype\s+([A-Za-z_]\w*)\s+struct\s*\{[^}]{0,512}\bgorm\.Model\b', re.S)
# Go gorm — struct field carries a gorm tag (`gorm:"..."`). Precision guard: the tag
# must contain at least one real gorm key (column / primaryKey / uniqueIndex / not null /
# autoCreateTime / autoUpdateTime / type / default / constraint / embedded / foreignKey /
# references / polymorphic / many2many / joinForeignKey / joinReferences / check / size /
# scale / precision / permissons / index / unique). We look for the STRUCT that owns the
# field, not just any occurrence of gorm:" in the file. Pattern: the struct name precedes
# a `struct {` block whose body (up to 2048 chars) contains a backtick-quoted gorm tag.
# Multi-group: group 1 = struct name.
_GO_GORM_TAG_STRUCT_RE = re.compile(
    r'\btype\s+([A-Za-z_]\w*)\s+struct\s*\{[^}]{0,2048}?\bgorm:"[^"]{1,512}"', re.S)
# Go ent — struct embeds ent.Schema (the canonical ent marker):
#   `type User struct { ent.Schema }`. The struct also needs `Fields() []ent.Field`
#   or `Annotations()` in the same FILE (not necessarily the same block) to confirm it is
#   a real schema type (not a mixin stub). We capture the struct name here and check the
#   whole-file guard in _go_ent_schema_names(). The embedded field is `ent.Schema` anchored
#   at word boundary; `mymixin.Schema` does not match.
_GO_ENT_SCHEMA_RE = re.compile(
    r'\btype\s+([A-Za-z_]\w*)\s+struct\s*\{[^}]{0,256}\bent\.Schema\b', re.S)


_GO_CAMEL_RE = re.compile(r'(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])')
# Go single-line comment stripper — removes `// ...` lines so that commented-out code
# examples (e.g. `//\ttype T struct { ent.Schema }` in Go doc comments) do not
# accidentally match the struct patterns. Block comments (`/* … */`) are left in place
# because they rarely contain compilable struct literals and stripping them adds complexity.
_GO_LINE_COMMENT_RE = re.compile(r'//[^\n]*')
# Struct names that are known gorm/ent framework internals — the framework's own type
# definitions embed gorm.Model or ent.Schema but are NOT user table declarations. A user
# app never names its model `Model` (that would conflict with gorm.Model) or `Schema`
# (that is the ent base type). Single-letter names like `T`, `S`, `M` are placeholders
# in documentation/generics, not real entity names. We drop these before minting.
_GO_SKIP_STRUCT_NAMES = frozenset({
    "model",       # gorm's own `type Model struct` in model.go
    "schema",      # ent's own Schema base type
    "t", "s", "m", "u", "v",  # single-letter generics / doc placeholders
})


def _go_table_name(struct_name):
    """Convert a Go CamelCase struct name to a gorm/ent default table name:
    snake_case plural. gorm pluralizes by appending 's' (not full English
    inflection); we mirror that simple rule so `User` → `users`, `OrderItem` →
    `order_items`. Precision is more important than recall here — we only do
    minimal pluralization (append 's') and never try to handle 'es'/'ies' forms,
    since a wrong plural is worse than a missing table. Returns None if the name
    is in the skip list."""
    low = struct_name.lower()
    if low in _GO_SKIP_STRUCT_NAMES:
        return None
    snake = _GO_CAMEL_RE.sub('_', struct_name).lower()
    return snake + 's' if snake else None


def _go_strip_comments(text):
    """Remove Go single-line comments (`// …`) to prevent doc-comment examples
    from matching the struct patterns. Bounded and never-crash."""
    try:
        return _GO_LINE_COMMENT_RE.sub('', text)
    except Exception:
        return text


# Go gorm/ent explicit name patterns — run AFTER comment stripping so that
# doc-comment examples (`//\tentsql.Annotation{Table: "Name"}`) do not fire.
# These are Go-only and live here (not in _ORM_PATTERNS) so comments are stripped first.
_GO_TABLENAME_RE = re.compile(
    r'\bfunc\s*\([^)]*\)\s*TableName\s*\(\s*\)\s*string\s*\{[^}]{0,256}return\s*[\'"]([A-Za-z_][\w.]*)[\'"]',
    re.S)
# gorm struct tag `gorm:"table:name"` — explicit override (rare).
_GO_GORM_TABLE_TAG_RE = re.compile(r'\bgorm:"[^"]*\btable:([A-Za-z_][\w.]*)\b')
# entsql.Annotation{Table: "name"} — explicit ent table name.
_GO_ENT_TABLE_ANNOT_RE = re.compile(
    r'\bentsql\.Annotation\s*\{[^}]{0,256}\bTable\s*:\s*[\'"]([A-Za-z_][\w.]*)[\'"]\s*\}', re.S)


def _go_explicit_refs(text):
    """Explicit Go gorm/ent table name declarations (after comment stripping):
    TableName() return value, gorm table: tag, entsql.Annotation{Table: ...}.
    Returns a set of normalized table names — content-free."""
    stripped = _go_strip_comments(text)
    out = set()
    for pat in (_GO_TABLENAME_RE, _GO_GORM_TABLE_TAG_RE, _GO_ENT_TABLE_ANNOT_RE):
        for m in pat.finditer(stripped):
            t = _norm_table(m.group(1))
            if t and t not in _SQL_NONTABLE and t not in _GO_SKIP_STRUCT_NAMES:
                out.add(t)
    # DETERMINISM: emit in a STABLE order. A bare set's str iteration order varies by
    # PYTHONHASHSEED, so a downstream caller that iterates this into node/edge emission would
    # reorder the graph bytes across otherwise-identical runs. Sort by the table NAME
    # (content-free — the names are unchanged, only their order is fixed).
    return sorted(out)


def _go_gorm_struct_names(text):
    """Go gorm struct names in a source file → set of snake_case plural table names.
    Only fires when the struct has a clear gorm marker (embeds gorm.Model OR has a
    gorm struct tag). Returns names only — content-free."""
    stripped = _go_strip_comments(text)
    out = set()
    # gorm.Model embedded — strongest marker: the struct is unambiguously a gorm model.
    for m in _GO_GORM_MODEL_RE.finditer(stripped):
        t = _go_table_name(m.group(1))
        if t:
            out.add(t)
    # gorm struct tag — struct contains at least one `gorm:"..."` field tag.
    # Only add structs not already caught by the gorm.Model pattern (they are the same
    # struct if gorm.Model is embedded, so adding twice is harmless since `out` is a set).
    for m in _GO_GORM_TAG_STRUCT_RE.finditer(stripped):
        t = _go_table_name(m.group(1))
        if t:
            out.add(t)
    # DETERMINISM: stable order (see _go_explicit_refs) — sort by table NAME, content-free.
    return sorted(out)


def _go_ent_schema_names(text):
    """Go ent struct names that embed ent.Schema AND have a Fields() or Annotations()
    method in the same file → set of snake_case plural table names. The two-signal guard
    prevents plain `ent.Schema`-embedding mixin stubs (which have no Fields method) from
    minting spurious table nodes. Content-free: struct names only."""
    stripped = _go_strip_comments(text)
    # File-level guard: must have at least one Fields() or Annotations() definition in
    # non-comment code, otherwise it is probably a mixin helper or framework type.
    has_fields = bool(re.search(r'\bfunc\s*\([^)]*\)\s*(?:Fields|Annotations)\s*\(', stripped))
    if not has_fields:
        return set()
    out = set()
    for m in _GO_ENT_SCHEMA_RE.finditer(stripped):
        t = _go_table_name(m.group(1))
        if t:
            out.add(t)
    # DETERMINISM: stable order (see _go_explicit_refs) — sort by table NAME, content-free.
    return sorted(out)
