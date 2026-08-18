#!/usr/bin/env python3
"""CONTENT-FREE EGRESS — the DB-STORAGE half (closes the gap the offline gate left open).

tests/test_content_free_egress.py proves build_graph's IN-MEMORY output is reference-shaped, and the render +
alert surfaces are body-free — but it is PURE/offline: it never INGESTS a graph through the real SQL and never
queries Postgres. So the "stored graph carries only path/symbol/line/edge metadata" half of the moat claim is
unproven at the DB layer — a regression in core.ingest_graph_with_authority that stored a raw body-bearing field
into core.code_node / core.code_edge would ship GREEN. This gate closes that, end to end on a real database:

  PART 1 (pipeline): drive the REAL extractor over a small repo, ingest the result through the REAL
    core.ingest_graph_with_authority as the REAL least-privilege veripsa_app role, then read back EVERY stored
    text column (node_id, path, name, language, edge src, edge dst) and assert each is REFERENCE-SHAPED — no body
    fragment (no newline/tab/control char, no run of whitespace, bounded length) ever lands in storage. If the
    extractor ever regressed to leak a body, this catches it AT THE STORE.

  PART 2 (ingest sanitiser): feed core.ingest_graph_with_authority a HAND-CRAFTED graph whose node `name` and edge
    `dst` carry a multi-line, whitespace-laden, SECRET-bearing source FRAGMENT (bypassing the extractor's own
    strip), and assert the STORED value is the sanitised one — `_safe_ref_token` remains the display-storage wall.
    The exact reference equality is retained separately as a non-reversible semantic digest.

Run:  python3 tests/test_content_free_egress_db.py   (needs local PostgreSQL — run_gates stands up its own)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile

import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X   # noqa: E402

DB = "veripsa_cfdb_" + str(os.getpid())
REPO = "acme/contentfree"
SECRET = "S3CR3T_BODY_PAYLOAD"
# a value with the SHAPE of a source body fragment: a newline, a tab, a run of whitespace, the secret marker.
POISON = f"return password +  {SECRET}\n\tif x:  leak(secret)   # body"


def _reference_shaped(v: str) -> bool:
    """A stored string is reference-shaped (a path / symbol / module ref) — NOT a source body fragment."""
    if v is None or v == "":
        return True
    if SECRET in v:                      # the body marker itself
        return False
    if any(ord(c) < 0x20 for c in v):    # any control char (newline/tab/…) = a multi-line body fragment
        return False
    if re.search(r"\s{2,}", v):          # a RUN of whitespace = laid-out source, never a real ref/path/identifier
        return False
    return len(v) <= 1600                # bounded


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    def db_app(sql, args=()):
        conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()

    def stored_strings():
        """EVERY stored text value across code_node + code_edge, read as the cluster superuser so RLS can't hide a
        row — we are auditing what physically landed in storage, not testing tenant scoping."""
        conn = psycopg2.connect(f"postgresql:///{DB}")        # default (superuser) role → bypasses FORCE RLS
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT node_id, path, COALESCE(name,''), COALESCE(language,'') FROM core.code_node")
                node_rows = cur.fetchall()
                cur.execute("SELECT src, dst FROM core.code_edge")
                edge_rows = cur.fetchall()
            return node_rows, edge_rows
        finally:
            conn.close()

    checks = []

    # ── PART 1: the REAL pipeline (extractor → ingest → store) keeps EVERY stored column reference-shaped ───────
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "lib.py"), "w") as fh:
            fh.write("def base():\n    return 0\n\nclass Helper:\n    def go(self):\n        return base()\n")
        with open(os.path.join(d, "app.py"), "w") as fh:
            fh.write("from lib import base, Helper\n\ndef run():\n    return base() + Helper().go()\n")
        with open(os.path.join(d, "page.js"), "w") as fh:
            # a normal JS import (resolves) + a require — the tree-sitter string-specifier vector, with clean refs
            fh.write('import { base } from "./lib";\nconst h = require("./helper");\nexport function go(){ return base(); }\n')
        graph = X.build_graph(d)
    db_app("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, "main", "a" * 40))

    node_rows, edge_rows = stored_strings()
    bad = []
    for row in node_rows:
        for v in row:
            if not _reference_shaped(v):
                bad.append(("code_node", v[:60]))
    for row in edge_rows:
        for v in row:
            if not _reference_shaped(v):
                bad.append(("code_edge", v[:60]))
    checks.append(("PART 1 — every stored code_node/code_edge string from the REAL extractor→ingest pipeline is "
                   "reference-shaped (no body fragment in any node_id/path/name/language/src/dst)",
                   not bad and len(node_rows) > 0 and len(edge_rows) > 0))
    if bad:
        print("  body-shaped stored values:", bad[:5])

    # ── PART 2: the INGEST sanitiser strips a body fragment fed straight into name + dst ──────────────────────
    poison_graph = {
        "nodes": [
            {"id": "poison.py", "kind": "file", "path": "poison.py"},
            {"id": "poison.py::leak", "kind": "def", "path": "poison.py", "name": POISON,
             "start_line": 1, "end_line": 2},
        ],
        "edges": [
            {"src": "poison.py", "dst": "./clean_module", "kind": "imports"},   # negative control: a clean ref
            {"src": "poison.py", "dst": POISON, "kind": "imports"},             # poison dst
        ],
    }
    db_app("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(poison_graph), REPO, "main", "b" * 40))
    node_rows, edge_rows = stored_strings()

    def _flattened(v) -> bool:
        """_safe_ref_token's DB-write guarantee."""
        return v is None or v == "" or (
            all(ord(c) >= 0x20 for c in v) and "\n" not in v and "\t" not in v and not re.search(r"\s{2,}", v))

    names = [r[2] for r in node_rows]
    dsts = [r[1] for r in edge_rows]
    name_flat = all(_flattened(n) for n in names) and len(names) > 0
    dst_flat = all(_flattened(dv) for dv in dsts)
    sanitised = (POISON not in names) and (POISON not in dsts)
    control_survived = any(dv == "./clean_module" for dv in dsts)     # a clean ref still produces its edge (no over-block)
    checks.append(("PART 2 — a multi-line body fragment fed straight into node `name` + edge `dst` is FLATTENED "
                   "at the DB write; the raw fragment never lands in display storage, and a clean control import "
                   "still produces its edge",
                   name_flat and dst_flat and sanitised and control_survived))

    # ── report ────────────────────────────────────────────────────────────────────────────────────────────────
    print("\n── CONTENT-FREE DB EGRESS ─────────────────────────────────")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and passed
    print("\nCONTENT-FREE DB EGRESS GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)
