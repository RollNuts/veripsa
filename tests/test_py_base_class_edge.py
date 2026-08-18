#!/usr/bin/env python3
"""PYTHON BASE-CLASS RECALL gate (measure-first, this lane).

A Python `class Sub(Base):` DEPENDS ON the file that defines `Base` — editing `Base`'s interface
ripples to every subclass. The CALL graph missed this: a base appears in a ClassDef's `.bases` as a
Name/Attribute, NOT an ast.Call, so no `calls` edge was ever emitted. This is the SAME high-precision
`sym_use: base` capture the generic tree-sitter spec already gives Java/C#/Rust/PHP/C++/Swift; Python
(the bespoke ast extractor) was the one language never given it. MEASURED on real repos: pallets/flask
live (dampened) co-change recall 30.7% → 31.3% (+2 GT pairs), raw 40.8% → 41.4%; every new live pair
verified a REAL inheritance link (one file defines `Flask`/`View`, the other extends it) — zero
fabricated couplings.

This gate proves, end-to-end through the LIVE `core.main_impact_surface` (the customer brain):

  REC) RECALL — a base class IS now a real coupling. `views.py` defines `View`; `myviews.py` does
       `class Home(View)` and IMPORTS it. Editing both must WARN (the inheritance coupling the call
       graph used to silently CLEAR). Proves the edge is load-bearing.

  PRE) PRECISION — a base whose name has MULTIPLE definers and is NOT imported must STILL be CLEAR.
       `a/Base.py` and `b/Base.py` both define `Handler`; `c/sub.py` does `class X(Handler)` with NO
       import. The base name is genuinely unresolvable (factory/duck/DI) → it flows through the SAME
       _claim_adjacency single-definer/import-confirmation guard every call passes and is DROPPED.
       This is exactly the false_coupling_precision invariant, asserted for the base-class edge too.

  XTR) EXTRACTION — the edge is emitted, CONTENT-FREE (dst = bare base NAME only, never the file body),
       qualified/generic bases collapse to the bare trailing name, and dunder/`object` plumbing is not
       a coupling decoy. A CONTROL (delete the edge) proves the recall check is load-bearing, not a
       tautology.

PROCESS-UNIQUE scratch DB (parallel-safe), exactly like the other gates.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402
import code_graph_extract as X  # noqa: E402

DB = "veripsa_pybase_" + str(os.getpid())


def db(sql, args=()):
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def _w(d, rel, body):
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(body)


def _graph(files):
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            _w(d, rel, body)
        return X.build_graph(d)


def _ingest(repo, files):
    g = _graph(files)
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(g), repo, "main", "a" * 40))


def _claim(cid, path, author, repo):
    db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (cid, path, repo, "main", author))


def _surface(repo):
    imp = db("SELECT core.main_impact_surface(%s,%s)", (repo, "main"))
    if isinstance(imp, str):
        imp = json.loads(imp)
    return {c["change_id"]: c for c in imp.get("changes", [])}


def main() -> int:
    checks = []

    # =============================================================================================
    # XTR) Pure-extractor checks — no DB. The edge exists, is content-free, and the control proves it.
    # =============================================================================================
    g = _graph({
        "views.py":   "class View:\n    def dispatch(self):\n        return 1\n",
        "myviews.py": "from views import View\nclass Home(View):\n    pass\n",
        "generic.py": "from typing import Protocol\nclass P(Protocol[int]):\n    pass\n",
        "plumb.py":   "class Z(object):\n    pass\n",   # `object` allowed but ubiquitous → engine drops it
    })
    calls = {(e["src"], e["dst"]) for e in g["edges"] if e["kind"] == "calls"}
    checks.append(("XTR: base class IS emitted as a `calls` edge (Home extends View) — the recall edge exists",
                   ("myviews.py", "View") in calls))
    checks.append(("XTR: a GENERIC base collapses to the bare trailing name (Protocol[int] → 'Protocol')",
                   ("generic.py", "Protocol") in calls))
    # content-free: every base dst is a bare identifier (no path, no '/', no body), like every call dst
    base_dsts = {d for s, d in calls if s in ("myviews.py", "generic.py", "plumb.py")}
    checks.append(("XTR: edge is CONTENT-FREE — dst is a bare base NAME, never a path or file body "
                   f"({sorted(base_dsts)})",
                   all(("/" not in d and "::" not in d and "\n" not in d) for d in base_dsts)))
    # CONTROL: a class with NO base produces NO base edge (the check is load-bearing, not a no-op)
    g_ctrl = _graph({"lone.py": "class Solo:\n    def m(self):\n        return 1\n"})
    ctrl_calls = {(e["src"], e["dst"]) for e in g_ctrl["edges"] if e["kind"] == "calls"}
    checks.append(("XTR-CONTROL: a base-less class emits NO base `calls` edge (fix is load-bearing, not blanket)",
                   not ctrl_calls))

    # =============================================================================================
    # DB-backed end-to-end (recall + precision) through the live customer brain.
    # =============================================================================================
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    # REC) RECALL — inheritance IS a coupling now. Home extends View AND imports it (unambiguous).
    repo_rec = "pb/recall"
    _ingest(repo_rec, {
        "app/views.py":   "class View:\n    def dispatch(self):\n        return 1\n",
        "app/myviews.py": "from app.views import View\n\nclass Home(View):\n    def get(self):\n        return 2\n",
    })
    _claim("PR-V:app/views.py", "app/views.py", "vdev", repo_rec)
    _claim("PR-H:app/myviews.py", "app/myviews.py", "hdev", repo_rec)
    s = _surface(repo_rec)
    v, h = s.get("PR-V", {}), s.get("PR-H", {})
    h_cw = h.get("contested_with", []) or []
    checks.append(("REC: editing a subclass + its base file WARNS (inheritance coupling the call graph "
                   f"used to silently CLEAR) — subclass verdict='{h.get('verdict')}', base verdict='{v.get('verdict')}'",
                   h.get("verdict") == "warn" and v.get("verdict") == "warn"
                   and any("vdev" in str(x) for x in h_cw)))

    # PRE) PRECISION — a multi-definer, un-imported base must STILL be CLEAR (same guard as a call).
    repo_pre = "pb/precision"
    _ingest(repo_pre, {
        "a/base.py": "class Handler:\n    def run(self):\n        return 'a'\n",
        "b/base.py": "class Handler:\n    def run(self):\n        return 'b'\n",
        "c/sub.py":  "class X(Handler):\n    pass\n",   # extends 'Handler' with NO import → unresolvable
    })
    _claim("PR-A:a/base.py", "a/base.py", "adev", repo_pre)
    _claim("PR-C:c/sub.py", "c/sub.py", "cdev", repo_pre)
    sp = _surface(repo_pre)
    a_p, c_p = sp.get("PR-A", {}), sp.get("PR-C", {})
    checks.append(("PRE: subclass of a MULTI-DEFINER base with NO import is CLEAR — base edge obeys the "
                   "SAME single-definer/import guard a call does (no false fan-out) "
                   f"(sub verdict='{c_p.get('verdict')}', a/base verdict='{a_p.get('verdict')}')",
                   c_p.get("verdict") == "clear" and not (c_p.get("contested_with") or [])
                   and a_p.get("verdict") == "clear" and not (a_p.get("contested_with") or [])))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("PY BASE-CLASS RECALL GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
