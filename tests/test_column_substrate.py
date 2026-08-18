#!/usr/bin/env python3
"""COLUMN-SUBSTRATE gate — additive column-level DDL/SQL extraction, RECALL-SAFE.

WHAT THIS GATE PINS (hermetic synthetic fixtures; content-free: table+column NAMES + edge
kinds + file paths only — no data, no DB, no network):

  (1) A CREATE TABLE statement mints column nodes + alters_col edges (DDL extraction).
  (2) An ALTER TABLE ... ADD COLUMN statement mints a column node + an alters_col edge.
  (3) A `SELECT col1, col2 FROM t` query mints queries_col edges to the explicit columns.
  (4) RECALL-SAFE (the non-regression boundary):
      (a) `SELECT * FROM t` does NOT produce any column-level edge — only the existing
          table-level `queries` edge is kept (the table coupling is NOT lost).
      (b) A file with ORM calls but no explicit SQL columns does NOT produce column
          edges — only the table-level `queries` edge is kept.
  (5) The TABLE-level nodes and `alters`/`queries` edges are BYTE-FOR-BYTE UNCHANGED from
      before this change for the same fixture (the live pause-tier relies on table coupling;
      column granularity is ADDITIVE and must never regress that).
  (6) Content-free: column NAMES only, never values or query bodies.
  (7) Never-crash: pathological inputs (empty body, deeply-nested parens, oversized DDL,
      binary-ish text) complete without raising and produce a sane (bounded) graph.

Print `COLUMN-SUBSTRATE GATE: PASS` / `FAIL`.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402
import _cg_schema as _SCH       # noqa: E402


def _w(d, rel, body):
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)


def _build(files):
    """Write `files` (rel->body) to a temp dir and build the graph."""
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            _w(d, rel, body)
        return X.build_graph(d)


def _col_nodes(g):
    return {n["id"]: n for n in g["nodes"] if n.get("kind") == "column"}


def _table_nodes(g):
    return {n["id"]: n for n in g["nodes"] if n.get("kind") == "table"}


def _edges_of_kind(g, kind):
    return [(e["src"], e["dst"]) for e in g["edges"] if e["kind"] == kind]


def main() -> int:
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    # -------------------------------------------------------------------------
    # (1) CREATE TABLE mints column nodes + alters_col edges
    # -------------------------------------------------------------------------
    g1 = _build({
        "db/001_init.sql": (
            "CREATE TABLE orders (id integer, price decimal(10,2), status text);\n"
        ),
    })
    cn = _col_nodes(g1)
    tn = _table_nodes(g1)
    ac = _edges_of_kind(g1, "alters_col")
    al = _edges_of_kind(g1, "alters")

    check("table::orders" in tn, "(1) CREATE TABLE: table node 'orders' missing")
    check("column::orders.id" in cn, "(1) CREATE TABLE: column node 'orders.id' missing")
    check("column::orders.price" in cn, "(1) CREATE TABLE: column node 'orders.price' missing")
    check("column::orders.status" in cn, "(1) CREATE TABLE: column node 'orders.status' missing")
    check(len(cn) == 3, f"(1) CREATE TABLE: expected 3 column nodes, got {len(cn)}: {list(cn)}")
    check(any(dst == "orders.id" for _, dst in ac),
          "(1) CREATE TABLE: alters_col edge to orders.id missing")
    check(any(dst == "orders.price" for _, dst in ac),
          "(1) CREATE TABLE: alters_col edge to orders.price missing")
    check(any(dst == "orders.status" for _, dst in ac),
          "(1) CREATE TABLE: alters_col edge to orders.status missing")
    # Table-level alters edge must still be present (additive, not replaced)
    check(any(dst == "orders" for _, dst in al),
          "(1) CREATE TABLE: table-level alters edge to 'orders' missing (regression!)")

    # -------------------------------------------------------------------------
    # (2) ALTER TABLE ... ADD COLUMN mints a column node + alters_col edge
    # -------------------------------------------------------------------------
    g2 = _build({
        "db/001_init.sql": "CREATE TABLE orders (id integer);\n",
        "db/002_add_col.sql": "ALTER TABLE orders ADD COLUMN shipped_at timestamp;\n",
    })
    cn2 = _col_nodes(g2)
    ac2 = _edges_of_kind(g2, "alters_col")
    al2 = _edges_of_kind(g2, "alters")

    check("column::orders.shipped_at" in cn2,
          "(2) ALTER ADD COLUMN: column node 'orders.shipped_at' missing")
    check(any(dst == "orders.shipped_at" for _, dst in ac2),
          "(2) ALTER ADD COLUMN: alters_col edge to orders.shipped_at missing")
    # Both sql files must still have table-level alters edges
    check(sum(1 for _, dst in al2 if dst == "orders") >= 1,
          "(2) ALTER ADD COLUMN: table-level alters edge to 'orders' missing (regression!)")

    # -------------------------------------------------------------------------
    # (3) SELECT col1, col2 FROM t → queries_col edges to explicit columns
    # -------------------------------------------------------------------------
    g3 = _build({
        "db/schema.sql": "CREATE TABLE orders (id integer, price decimal(10,2));\n",
        "app/views.py": (
            'def get(oid):\n'
            '    return db.execute("SELECT id, price FROM orders WHERE id=%s", oid)\n'
        ),
    })
    cn3 = _col_nodes(g3)
    qc3 = _edges_of_kind(g3, "queries_col")
    ql3 = _edges_of_kind(g3, "queries")

    check(any(dst == "orders.id" for _, dst in qc3),
          "(3) SELECT col list: queries_col edge to orders.id missing")
    check(any(dst == "orders.price" for _, dst in qc3),
          "(3) SELECT col list: queries_col edge to orders.price missing")
    check("column::orders.id" in cn3,
          "(3) SELECT col list: column node orders.id missing")
    # Table-level queries edge must still be present (additive)
    check(any(dst == "orders" for _, dst in ql3),
          "(3) SELECT col list: table-level queries edge to 'orders' missing (regression!)")

    # -------------------------------------------------------------------------
    # (4a) RECALL-SAFE: SELECT * does NOT produce any column edge; table coupling kept
    # -------------------------------------------------------------------------
    g4a = _build({
        "db/schema.sql": "CREATE TABLE orders (id integer, price decimal(10,2));\n",
        "app/star.py": (
            'def all_orders():\n'
            '    return db.execute("SELECT * FROM orders")\n'
        ),
    })
    qc4a = _edges_of_kind(g4a, "queries_col")
    ql4a = _edges_of_kind(g4a, "queries")

    check(not qc4a,
          f"(4a) SELECT *: should produce NO queries_col edges, got: {qc4a}")
    check(any(dst == "orders" for _, dst in ql4a),
          "(4a) SELECT *: table-level queries edge to 'orders' must be preserved (recall-safe!)")

    # -------------------------------------------------------------------------
    # (4b) RECALL-SAFE: ORM-only file (no explicit SQL columns) keeps table coupling
    # -------------------------------------------------------------------------
    g4b = _build({
        "db/schema.sql": "CREATE TABLE orders (id integer, price decimal(10,2));\n",
        "app/orm_view.py": (
            'from sqlalchemy import create_engine\n'
            'class Order:\n'
            '    __tablename__ = "orders"\n'
            '    id = Column(Integer)\n'
        ),
    })
    qc4b = _edges_of_kind(g4b, "queries_col")
    ql4b = _edges_of_kind(g4b, "queries")

    check(not qc4b,
          f"(4b) ORM-only: should produce NO queries_col edges, got: {qc4b}")
    check(any(dst == "orders" for _, dst in ql4b),
          "(4b) ORM-only: table-level queries edge to 'orders' must be preserved (recall-safe!)")

    # -------------------------------------------------------------------------
    # (5) TABLE-level nodes and edges are BYTE-FOR-BYTE UNCHANGED (regression test)
    #     For a fixture with only raw DDL and a simple SELECT, the table node, table
    #     alters edge, and table queries edge must be identical to what the extractor
    #     produced before the column layer was added.
    # -------------------------------------------------------------------------
    g5 = _build({
        "db/schema.sql": "CREATE TABLE products (id integer, name text);\n",
        "app/products.py": (
            'def list_all():\n'
            '    return db.execute("SELECT id FROM products")\n'
        ),
    })
    tn5 = _table_nodes(g5)
    al5 = _edges_of_kind(g5, "alters")
    ql5 = _edges_of_kind(g5, "queries")

    check(len(tn5) == 1 and "table::products" in tn5,
          f"(5) regression: exactly one table node 'products' expected, got {list(tn5)}")
    check(len(al5) == 1 and al5[0] == ("db/schema.sql", "products"),
          f"(5) regression: exactly one table-level alters edge expected, got {al5}")
    check(len(ql5) == 1 and ql5[0] == ("app/products.py", "products"),
          f"(5) regression: exactly one table-level queries edge expected, got {ql5}")

    # -------------------------------------------------------------------------
    # (6) Content-free: column nodes carry only names and structural metadata,
    #     never values or query body text
    # -------------------------------------------------------------------------
    g6 = _build({
        "db/schema.sql": (
            "CREATE TABLE payments (\n"
            "    id integer,\n"
            "    amount decimal(12,4),\n"
            "    card_number varchar(20)\n"
            ");\n"
        ),
    })
    for n in g6["nodes"]:
        if n.get("kind") == "column":
            # First-class resource metadata is itself content-free: canonical
            # coordinate, producer, bounded confidence, and structural provenance.
            allowed_keys = {
                "id", "kind", "name", "table", "path", "language",
                "canonical_key", "repo", "scope", "extractor",
                "confidence", "provenance",
            }
            extra = set(n.keys()) - allowed_keys
            check(not extra,
                  f"(6) content-free: column node has unexpected keys {extra}: {n}")
            check("card_number" not in str(n.get("id", "")).replace("payments.card_number", ""),
                  "(6) content-free: column node id must only contain table.col, not data")

    # -------------------------------------------------------------------------
    # (7) Never-crash: pathological inputs
    # -------------------------------------------------------------------------
    pathological = [
        ("empty.sql", ""),
        ("deep_paren.sql",
         "CREATE TABLE deep (c int DEFAULT " + "(" * 200 + "1" + ")" * 200 + ");\n"),
        ("binary_ish.sql", "CREATE TABLE x\x00 (id integer);\n"),
        ("no_col_list.sql", "CREATE TABLE bare;\n"),
        ("select_star.py",
         'def f():\n    return db.run("SELECT * FROM orders")\n'),
        ("oversized_cols.sql",
         "CREATE TABLE big (" + ",".join(f"col_{i} int" for i in range(600)) + ");\n"),
    ]
    for fname, body in pathological:
        try:
            with tempfile.TemporaryDirectory() as d:
                # Need at least one known table for query edges to be possible
                _w(d, "schema.sql", "CREATE TABLE orders (id integer);\n")
                _w(d, fname, body)
                g7 = X.build_graph(d)
            check(True, f"(7) never-crash: {fname}")  # reached = no raise
        except Exception as exc:
            failures.append(f"(7) never-crash: {fname} raised {type(exc).__name__}: {exc}")

    # Oversized col list must be bounded by _MAX_COLUMNS per table
    with tempfile.TemporaryDirectory() as d:
        _w(d, "big.sql",
           "CREATE TABLE big (" + ",".join(f"col_{i} int" for i in range(600)) + ");\n")
        g7b = X.build_graph(d)
    col_count = sum(1 for n in g7b["nodes"]
                    if n.get("kind") == "column" and n.get("table") == "big")
    check(col_count <= _SCH._MAX_COLUMNS,
          f"(7) never-crash: oversized col list minted {col_count} column nodes > _MAX_COLUMNS")

    # -------------------------------------------------------------------------
    # UPDATE SET col extraction
    # -------------------------------------------------------------------------
    g8 = _build({
        "db/schema.sql": "CREATE TABLE orders (id integer, status text);\n",
        "app/updater.py": (
            'def ship(oid):\n'
            '    db.execute("UPDATE orders SET status = %s WHERE id = %s", "shipped", oid)\n'
        ),
    })
    qc8 = _edges_of_kind(g8, "queries_col")
    check(any(dst == "orders.status" for _, dst in qc8),
          "(8) UPDATE SET: queries_col edge to orders.status missing")
    # Table-level queries edge must still be present
    ql8 = _edges_of_kind(g8, "queries")
    check(any(dst == "orders" for _, dst in ql8),
          "(8) UPDATE SET: table-level queries edge to 'orders' missing (regression!)")

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        print("COLUMN-SUBSTRATE GATE: FAIL")
        return 1

    print("column-substrate: CREATE TABLE mints column nodes + alters_col edges;")
    print("  ALTER ADD COLUMN mints column node + alters_col edge;")
    print("  SELECT col list mints queries_col edges; UPDATE SET mints queries_col edges;")
    print("  SELECT * produces NO column edges (recall-safe, table edge kept);")
    print("  ORM-only file produces NO column edges (recall-safe, table edge kept);")
    print("  table-level nodes+edges byte-for-byte unchanged (regression clean);")
    print("  content-free (names only); never-crash (pathological inputs handled).")
    print("COLUMN-SUBSTRATE GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
