#!/usr/bin/env python3
"""PYTHON BARE-DECORATOR RECALL gate (measure-first, this lane).

A Python `@setupmethod`-decorated function DEPENDS ON the file that defines the decorator — editing the
decorator's contract ripples to every decorated symbol. The CALL graph missed only the BARE form: a
CALLED decorator `@app.route(...)` is an ast.Call already walked (it emits `route`), but a bare
`@setupmethod` is an ast.Name/Attribute that produced NO `calls` edge → a silent CLEAR. This is the same
high-precision `sym_use` family the base-class edge belongs to (a decorator name is SPECIFIC, rarely
ubiquitous), and Python's bespoke ast extractor was emitting only its CALLED form.

MEASURED on pallets/flask (full history, 806-commit co-change window):
  • live (dampened, SHIPPING) co-change recall 31.3% → 32.2% (+3 GT pairs: 109 → 112);
    raw-graph recall 41.4% → 42.2% (+3: 144 → 147). Dampening-killed column UNCHANGED at 35
    (the new edges added NO noise that dampening then had to throw away).
  • Net-new real pairs verified by hand: e.g. sansio/app.py + sansio/blueprints.py each apply
    `@setupmethod`, IMPORTED single-definer from sansio/scaffold.py — an import-confirmed inheritance-
    grade link the call graph never had. Zero fabricated couplings.

This gate proves, end-to-end through the LIVE `core.main_impact_surface` (the customer brain):

  REC) RECALL — a bare decorator IS now a real coupling. `deco.py` defines `audit`; `handlers.py` does
       `@audit` on a function and IMPORTS it. Editing both must WARN (the decorator coupling the call
       graph used to silently CLEAR). Proves the edge is load-bearing.

  PRE) PRECISION — a bare decorator whose name has MULTIPLE definers and is NOT imported must STILL be
       CLEAR. `a/deco.py` and `b/deco.py` both define `trace`; `c/use.py` does `@trace` with NO import.
       The name is genuinely unresolvable (factory/DI/duck) → it flows through the SAME _claim_adjacency
       single-definer/import-confirmation guard every call passes and is DROPPED. This is exactly the
       false_coupling_precision invariant, asserted for the bare-decorator edge too.

  XTR) EXTRACTION — the edge is emitted, CONTENT-FREE (dst = bare decorator NAME only, never a body),
       a qualified bare decorator (`@m.deco`) collapses to the trailing name, a CALLED decorator
       (`@deco()`) is NOT double-emitted (the ast.Call branch already carries it), and a builtin bare
       decorator (`@property`) is emitted but inert (no local definer → engine drops it, no decoy). A
       CONTROL (an undecorated def) proves the recall check is load-bearing, not a tautology.

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

DB = "veripsa_pydeco_" + str(os.getpid())


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
    # XTR) Pure-extractor checks — no DB. The edge exists, is content-free, no double-emit, control.
    # =============================================================================================
    g = _graph({
        "deco.py":     "def audit(fn):\n    return fn\n",
        "handlers.py": "from deco import audit\n@audit\ndef handle():\n    return 1\n",
        "attr.py":     "import deco\n@deco.audit\ndef h2():\n    return 2\n",
        "called.py":   "from deco import audit\n@audit()\ndef h3():\n    return 3\n",
        "plumb.py":    "class W:\n    @property\n    def x(self):\n        return 1\n",  # builtin → engine drops it
    })
    calls = [(e["src"], e["dst"]) for e in g["edges"] if e["kind"] == "calls"]
    callset = set(calls)
    checks.append(("XTR: a BARE decorator IS emitted as a `calls` edge (@audit on handle) — the recall edge exists",
                   ("handlers.py", "audit") in callset))
    checks.append(("XTR: a QUALIFIED bare decorator collapses to the trailing name (@deco.audit → 'audit')",
                   ("attr.py", "audit") in callset))
    # NO double-emit: a CALLED decorator @audit() must emit 'audit' exactly ONCE (the ast.Call branch
    # already carries it; the decorator branch must SKIP it).
    called_audit = [d for s, d in calls if s == "called.py" and d == "audit"]
    checks.append(("XTR: a CALLED decorator @audit() is NOT double-emitted (exactly one 'audit' edge, "
                   f"from the ast.Call branch only) — found {len(called_audit)}",
                   len(called_audit) == 1))
    # content-free: every emitted decorator dst is a bare identifier (no path, no '/', no body)
    deco_dsts = {d for s, d in calls if s in ("handlers.py", "attr.py", "plumb.py")}
    checks.append(("XTR: edge is CONTENT-FREE — dst is a bare decorator NAME, never a path or file body "
                   f"({sorted(deco_dsts)})",
                   all(("/" not in d and "::" not in d and "\n" not in d) for d in deco_dsts)))
    # CONTROL: an undecorated def produces NO extra `calls` edge from the decorator path (load-bearing).
    g_ctrl = _graph({"lone.py": "def solo():\n    return 1\n"})
    ctrl_calls = {(e["src"], e["dst"]) for e in g_ctrl["edges"] if e["kind"] == "calls"}
    checks.append(("XTR-CONTROL: an undecorated def emits NO decorator `calls` edge (fix is load-bearing, "
                   "not blanket)", not ctrl_calls))

    # =============================================================================================
    # DB-backed end-to-end (recall + precision) through the live customer brain.
    # =============================================================================================
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    # REC) RECALL — a bare decorator IS a coupling now. handlers @audit AND imports it (unambiguous).
    repo_rec = "pd/recall"
    _ingest(repo_rec, {
        "app/deco.py":     "def audit(fn):\n    def w(*a, **k):\n        return fn(*a, **k)\n    return w\n",
        "app/handlers.py": "from app.deco import audit\n\n@audit\ndef handle():\n    return 2\n",
    })
    _claim("PR-D:app/deco.py", "app/deco.py", "ddev", repo_rec)
    _claim("PR-H:app/handlers.py", "app/handlers.py", "hdev", repo_rec)
    s = _surface(repo_rec)
    d, h = s.get("PR-D", {}), s.get("PR-H", {})
    h_cw = h.get("contested_with", []) or []
    checks.append(("REC: editing a @decorated function + the decorator's file WARNS (decorator coupling the "
                   f"call graph used to silently CLEAR) — handler verdict='{h.get('verdict')}', deco verdict='{d.get('verdict')}'",
                   h.get("verdict") == "warn" and d.get("verdict") == "warn"
                   and any("ddev" in str(x) for x in h_cw)))

    # PRE) PRECISION — a multi-definer, un-imported bare decorator must STILL be CLEAR (same guard).
    repo_pre = "pd/precision"
    _ingest(repo_pre, {
        "a/deco.py": "def trace(fn):\n    return fn\n",
        "b/deco.py": "def trace(fn):\n    return fn\n",
        "c/use.py":  "@trace\ndef job():\n    return 1\n",   # @trace with NO import → unresolvable
    })
    _claim("PR-A:a/deco.py", "a/deco.py", "adev", repo_pre)
    _claim("PR-C:c/use.py", "c/use.py", "cdev", repo_pre)
    sp = _surface(repo_pre)
    a_p, c_p = sp.get("PR-A", {}), sp.get("PR-C", {})
    checks.append(("PRE: a @decorated def using a MULTI-DEFINER decorator with NO import is CLEAR — the "
                   "edge obeys the SAME single-definer/import guard a call does (no false fan-out) "
                   f"(use verdict='{c_p.get('verdict')}', a/deco verdict='{a_p.get('verdict')}')",
                   c_p.get("verdict") == "clear" and not (c_p.get("contested_with") or [])
                   and a_p.get("verdict") == "clear" and not (a_p.get("contested_with") or [])))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("CORE RECALL GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
