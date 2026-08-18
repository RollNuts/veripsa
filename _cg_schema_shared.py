"""Shared schema-extraction primitives used across the SQL / Go / ORM passes.

Owns the two cross-language helpers every language group needs: `_norm_table`
(the table-name normalizer that makes `public."Orders"` / `orders` / `[orders]`
collide on `orders`) and `_SQL_NONTABLE` (the keyword stop-set). They live here —
not in any one language module — so `_cg_schema_sql`, `_cg_schema_go`, and
`_cg_schema_orm` can all import them WITHOUT a circular import (the ORM pass also
calls into the Go pass, so a shared leaf module is the only cycle-free home).

Split out of the former god-file `_cg_schema.py` as a PURE behaviour-preserving
move (no logic / regex / signature change). Content-free: table NAMES only.
"""

_SQL_NONTABLE = frozenset({
    "only", "if", "exists", "table", "select", "where", "from", "into", "set",
    "and", "or", "not", "null", "values"})


def _norm_table(name):
    """A table name normalized for matching: last dotted segment, lowercased,
    quotes/brackets stripped (so `public."Orders"` / `orders` / `[orders]` all collide
    on `orders`)."""
    if not name:
        return None
    n = name.strip().strip('`"[]').split(".")[-1].strip('`"[]').lower()
    return n or None
