#!/usr/bin/env python3
"""CROSS-TIER ROUTE↔CALL coupling — END-TO-END engine gate (PR #270 probe → integration).

PROVES the live coupling engine (core.main_impact_surface — the customer-facing brain) now WARNS on a
client/server CONTRACT it was BLIND to before this change: a backend file DEFINES `/api/orders/{id}` and a
frontend file FETCHES `/api/orders/5`. Two in-flight PRs, one on each file, must be COUPLED (warn/contested)
even though there is NO code edge / NO shared symbol / NO import between the two files — the contract is a
cross-tier, cross-language, cross-directory dependency that the call graph and the import graph cannot see.

The signal was MEASURE-FIRST proven in PR #270 (matched route↔call pairs co-change ~17× median, 86% vs 22%
random once the specificity floors apply). This gate is the INTEGRATION proof: the proven probe logic, wired
into build_graph as the _cg_routes producer (a file→file `imports` edge — kind ∈ the existing CHECK set, NO
schema change), actually reaches the engine and makes it couple the contract.

Three cases on the REAL engine, each its own synthetic full-stack repo:

  A) THE COUPLING (the win): backend defines `/api/orders/{id}`, frontend fetches `/api/orders/5`.
     Editing the backend route file and editing the frontend fetch file MUST be coupled (warn / contested).
     This coupling did NOT exist before the producer (no import/call/schema/config edge links the two).

  B) THE FLOOR HOLDS (precision): a BARE/ubiquitous route on both sides (`/` and `/health`). Editing the
     two files must STAY CLEAR — a ubiquitous route must not couple everything (the #270 specificity floor).

  C) THE BASELINE (recall sanity): a frontend file that fetches an UNRELATED specific route the backend
     does NOT define (`/api/widgets/9`) is NOT coupled to the orders backend — no spurious cross-tier fan-out.

PROCESS-UNIQUE scratch DB (parallel-safe), exactly like the other engine gates.
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

DB = "veripsa_xtroute_" + str(os.getpid())

# --- synthetic full-stack file bodies (content-free fixtures) ----------------------------------------
_BACKEND_ORDERS = (
    "from fastapi import APIRouter\n"
    "router = APIRouter()\n\n"
    "@router.get('/api/orders/{id}')\n"
    "def get_order(id):\n"
    "    return {'id': id}\n"
)
_FRONTEND_ORDERS = (
    "export async function loadOrder(id) {\n"
    "  const r = await fetch(`/api/orders/${id}`);\n"
    "  return r.json();\n"
    "}\n"
)
# bare / ubiquitous routes on both sides — must NOT couple (the specificity floor)
_BACKEND_HEALTH = (
    "from fastapi import APIRouter\n"
    "r = APIRouter()\n\n"
    "@r.get('/health')\n"
    "@r.get('/')\n"
    "def health():\n"
    "    return {'ok': True}\n"
)
_FRONTEND_HEALTH = (
    "export const ping = () => fetch('/health');\n"
    "export const home = () => fetch('/');\n"
)
# an UNRELATED specific route the backend does NOT define — must NOT couple to the orders backend
_FRONTEND_WIDGETS = (
    "export const loadWidget = (id) => fetch(`/api/widgets/${id}`);\n"
)


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


def _ingest(repo, files):
    with tempfile.TemporaryDirectory() as d:
        for rel, body in files.items():
            _w(d, rel, body)
        g = X.build_graph(d)
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(g), repo, "main", "a" * 40))
    return g


def _claim(cid, path, author, repo):
    db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (cid, path, repo, "main", author))


def _surface(repo):
    imp = db("SELECT core.main_impact_surface(%s,%s)", (repo, "main"))
    if isinstance(imp, str):
        imp = json.loads(imp)
    return {c["change_id"]: c for c in imp.get("changes", [])}


def _has_xtier_edge(graph, f1, f2):
    """True iff the built graph carries the cross-tier file→file `imports` edge between f1 and f2 (either
    direction). This proves the PRODUCER emitted the contract edge BEFORE we even touch the engine."""
    for e in graph["edges"]:
        if e.get("kind") != "imports":
            continue
        s, d = e.get("src"), e.get("dst")
        if (s == f1 and d == f2) or (s == f2 and d == f1):
            return True
    return False


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    checks = []
    coupled = lambda c: c.get("verdict") in ("warn", "contested") and bool(c.get("contested_with") or [])

    # ----------------------------------------------------------------------------------------------
    # A) THE COUPLING — the contract MUST couple (the win this integration ships).
    repo_a = "xt/orders-contract"
    ga = _ingest(repo_a, {
        "backend/api.py": _BACKEND_ORDERS,
        "frontend/orders.ts": _FRONTEND_ORDERS,
    })
    # Producer-level proof: the cross-tier edge exists in the graph (no schema change — it's an `imports` edge).
    has_edge = _has_xtier_edge(ga, "backend/api.py", "frontend/orders.ts")
    checks.append(("A: producer emitted the cross-tier file→file `imports` edge backend/api.py ↔ "
                   f"frontend/orders.ts (kind=imports, no schema change) (has_edge={has_edge})", has_edge))
    _claim("PR-BE:backend/api.py", "backend/api.py", "bedev", repo_a)
    _claim("PR-FE:frontend/orders.ts", "frontend/orders.ts", "fedev", repo_a)
    sa = _surface(repo_a)
    be_a = sa.get("PR-BE", {})
    fe_a = sa.get("PR-FE", {})
    checks.append(("A: backend route file (defines /api/orders/{id}) is COUPLED with the frontend fetch file "
                   f"(verdict='{be_a.get('verdict')}', contested_with={be_a.get('contested_with')})",
                   coupled(be_a) and any("fedev" in str(x) or "frontend" in str(x)
                                         for x in (be_a.get("contested_with") or []))))
    checks.append(("A: frontend fetch file (calls /api/orders/5) is COUPLED with the backend route file "
                   f"(verdict='{fe_a.get('verdict')}', contested_with={fe_a.get('contested_with')})",
                   coupled(fe_a) and any("bedev" in str(x) or "backend" in str(x)
                                         for x in (fe_a.get("contested_with") or []))))

    # ----------------------------------------------------------------------------------------------
    # B) THE FLOOR HOLDS — a BARE/ubiquitous route (`/`, `/health`) must NOT couple (precision).
    repo_b = "xt/ubiquitous-floor"
    gb = _ingest(repo_b, {
        "backend/health.py": _BACKEND_HEALTH,
        "frontend/ping.ts": _FRONTEND_HEALTH,
    })
    no_edge = not _has_xtier_edge(gb, "backend/health.py", "frontend/ping.ts")
    checks.append(("B: NO cross-tier edge minted for a bare/ubiquitous route (`/`, `/health`) — the "
                   f"specificity floor drops it (no_ubiquitous_edge={no_edge})", no_edge))
    _claim("PR-BE:backend/health.py", "backend/health.py", "bedev", repo_b)
    _claim("PR-FE:frontend/ping.ts", "frontend/ping.ts", "fedev", repo_b)
    sb = _surface(repo_b)
    be_b = sb.get("PR-BE", {})
    fe_b = sb.get("PR-FE", {})
    checks.append(("B: ubiquitous-route files stay CLEAR — `/health` does not couple everything "
                   f"(backend verdict='{be_b.get('verdict')}', frontend verdict='{fe_b.get('verdict')}')",
                   be_b.get("verdict") == "clear" and fe_b.get("verdict") == "clear"
                   and not (be_b.get("contested_with") or []) and not (fe_b.get("contested_with") or [])))

    # ----------------------------------------------------------------------------------------------
    # C) THE BASELINE — a frontend fetch of a SPECIFIC route the backend does NOT define is NOT coupled.
    repo_c = "xt/unrelated-route"
    gc = _ingest(repo_c, {
        "backend/api.py": _BACKEND_ORDERS,            # defines /api/orders/{id} only
        "frontend/widgets.ts": _FRONTEND_WIDGETS,     # fetches /api/widgets/9 — no matching backend route
    })
    no_edge_c = not _has_xtier_edge(gc, "backend/api.py", "frontend/widgets.ts")
    checks.append(("C: a frontend fetch of an UNRELATED specific route (/api/widgets/9) is NOT coupled to the "
                   f"orders backend — no spurious cross-tier fan-out (no_edge={no_edge_c})", no_edge_c))
    _claim("PR-BE:backend/api.py", "backend/api.py", "bedev", repo_c)
    _claim("PR-FE:frontend/widgets.ts", "frontend/widgets.ts", "fedev", repo_c)
    sc = _surface(repo_c)
    be_c = sc.get("PR-BE", {})
    fe_c = sc.get("PR-FE", {})
    checks.append(("C: backend orders file is CLEAR vs the unrelated widgets fetch "
                   f"(verdict='{be_c.get('verdict')}')",
                   be_c.get("verdict") == "clear" and not (be_c.get("contested_with") or [])))
    checks.append(("C: frontend widgets file is CLEAR vs the orders backend "
                   f"(verdict='{fe_c.get('verdict')}')",
                   fe_c.get("verdict") == "clear" and not (fe_c.get("contested_with") or [])))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("CROSS-TIER ROUTE COUPLING GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
