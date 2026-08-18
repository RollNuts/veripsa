#!/usr/bin/env python3
"""Topological suggested-land-order gate.

When in-flight PRs form a dependency CHAIN px → py → {pz1,pz2,pz3} (py imports px; the pz* import py), the
suggested land order must put the UPSTREAM (foundational) change FIRST so the rest rebase onto it once — even
though the middle change py has MORE DIRECT dependents (3) than its own upstream px (1). The old blast-count
heuristic ranked by direct dependents and would wrongly put py before px; the transitive-dependent ranking
fixes it. Content-free; runs the real core.main_impact_surface over a real graph + real claims.

Run:  python3 tests/test_topo_order.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run → "does not exist".
# Per-PID, exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_server.py.
DB = "veripsa_topotest_" + str(os.getpid())
REPO = "acme/topo"


def make_db(role):
    def run(sql, args=()):
        conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    return run


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    db = make_db("veripsa_app")
    checks = []

    # graph: px is foundational; py imports px; pz1/pz2/pz3 each import py  (chain px → py → pz*)
    nodes = [{"id": p, "kind": "file", "path": p, "language": "python"}
             for p in ["px.py", "py.py", "pz1.py", "pz2.py", "pz3.py"]]
    edges = [{"src": "py.py", "dst": "px.py", "kind": "imports"},
             {"src": "pz1.py", "dst": "py.py", "kind": "imports"},
             {"src": "pz2.py", "dst": "py.py", "kind": "imports"},
             {"src": "pz3.py", "dst": "py.py", "kind": "imports"}]
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
       (json.dumps({"nodes": nodes, "edges": edges}), REPO, "main", "a" * 40))

    # in-flight PRs by different authors: A edits px (foundational), B edits py, C1/C2/C3 edit pz1/pz2/pz3
    for cid, path, who in [("PR-1:px.py", "px.py", "alice"), ("PR-2:py.py", "py.py", "bob"),
                           ("PR-3:pz1.py", "pz1.py", "carol"), ("PR-4:pz2.py", "pz2.py", "dave"),
                           ("PR-5:pz3.py", "pz3.py", "erin")]:
        db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (cid, path, REPO, "main", who))

    surf = db("SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
    surf = surf if isinstance(surf, dict) else json.loads(surf)
    clusters = surf.get("clusters") or []
    cl = max(clusters, key=lambda c: c.get("size", 0)) if clusters else {}
    order = cl.get("suggested_order") or []

    def idx(token):
        for i, lbl in enumerate(order):
            if token in lbl:
                return i
        return -1
    iA, iB = idx("PR-1"), idx("PR-2")
    iC = [idx("PR-3"), idx("PR-4"), idx("PR-5")]

    checks.append((f"one cluster holds all 5 chained in-flight changes (size={cl.get('size')}, order={order})",
                   cl.get("size") == 5 and len(order) == 5))
    checks.append((f"topological: the foundational change PR-1(px) lands FIRST (idx={iA})", iA == 0))
    checks.append((f"topological: upstream PR-2(py) lands before its downstream PR-3/4/5 (iB={iB}, iC={iC})",
                   iB >= 0 and all(c > iB for c in iC)))
    checks.append((f"beats the count heuristic: PR-2(py) has 3 direct dependents vs PR-1(px)'s 1, yet lands "
                   f"AFTER PR-1 (iA={iA} < iB={iB})", 0 <= iA < iB))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("TOPO ORDER GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
