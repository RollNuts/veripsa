#!/usr/bin/env python3
"""FALSE-COUPLING / OVER-WARN PRECISION gate (audit 2026-06-18).

Veripsa's quality is PRECISE SILENCE: a false graph edge → a false "Heads up" warn / spurious contested
pair → wallpaper that destroys trust. The single sharpest false-edge source in the CALL graph is a call
to a symbol NAME that is defined in MULTIPLE files, where the caller IMPORTS NONE of them — the call
target is genuinely UNRESOLVABLE (it is reached via a factory / dependency-injection / duck typing). The
prior engine FANNED such a call out to ALL same-name definers (≤3), so editing the caller was reported
CONTESTED with every unrelated file that happens to define a method of the same name (`process`, `save`,
`validate`, …). That is a cry-wolf warn between files with NO real call/import edge.

This proves the live `core.main_impact_surface` (the customer-facing brain) on three constructed cases:

  A) FALSE COUPLING (the bug): `worker.py` calls `h.process()` on a handler from a factory and imports
     NEITHER definer; `process` is defined on an UNRELATED `payments/Gateway` AND `images/ImageFilter`.
     Editing worker and editing images/filter must NOT be contested — there is no real coupling. CLEAR.

  B) RECALL — import-confirmed: `worker.py` IMPORTS `payments.gateway` and calls `process`. It MUST warn
     vs payments (real coupling) and must NOT warn vs the unrelated images/filter (right one only).

  C) RECALL — single-definer, no import: a name defined in exactly ONE file, called with no explicit
     import (same-package / dynamic). It is UNAMBIGUOUS (the only place the name lives) → MUST still warn.

The fix tightens _claim_adjacency's out_adj/in_adj: a no-import call is kept ONLY when the name has a
SINGLE definer (unambiguous); a no-import call to a name with MULTIPLE definers is dropped (no false
fan-out). Import-confirmed and single-definer couplings — the evidence-backed ones — are untouched.

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

DB = "veripsa_fctest_" + str(os.getpid())


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


def _claim(cid, path, author, repo):
    db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (cid, path, repo, "main", author))


def _surface(repo):
    imp = db("SELECT core.main_impact_surface(%s,%s)", (repo, "main"))
    if isinstance(imp, str):
        imp = json.loads(imp)
    return {c["change_id"]: c for c in imp.get("changes", [])}


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    checks = []

    # ----------------------------------------------------------------------------------------------
    # A) FALSE COUPLING — must be CLEAR (the bug this audit fixes).
    # `process` defined on two UNRELATED classes; worker calls .process() via a factory, imports neither.
    repo_a = "fc/falsecouple"
    _ingest(repo_a, {
        "payments/gateway.py": "class Gateway:\n    def process(self):\n        return 'pay'\n",
        "images/filter.py":    "class ImageFilter:\n    def process(self):\n        return 'img'\n",
        "app/worker.py":       "from app.factory import make_handler\n\n"
                               "def run():\n    h = make_handler()\n    return h.process()\n",
        "app/factory.py":      "def make_handler():\n    return None\n",
    })
    _claim("PR-W:app/worker.py", "app/worker.py", "wdev", repo_a)
    _claim("PR-IMG:images/filter.py", "images/filter.py", "imgdev", repo_a)
    sa = _surface(repo_a)
    w_a = sa.get("PR-W", {})
    img_a = sa.get("PR-IMG", {})
    checks.append(("A: worker (no-import multi-definer call) is NOT contested with unrelated images/filter "
                   f"(verdict='{w_a.get('verdict')}', contested_with={w_a.get('contested_with')})",
                   w_a.get("verdict") == "clear" and not (w_a.get("contested_with") or [])))
    checks.append(("A: images/filter (the other side) is likewise CLEAR — symmetric, no false warn "
                   f"(verdict='{img_a.get('verdict')}')",
                   img_a.get("verdict") == "clear" and not (img_a.get("contested_with") or [])))

    # ----------------------------------------------------------------------------------------------
    # B) RECALL — import-confirmed call must STILL warn, and ONLY against the right (imported) definer.
    repo_b = "fc/recall-import"
    _ingest(repo_b, {
        "payments/gateway.py": "class Gateway:\n    def process(self):\n        return 'pay'\n",
        "images/filter.py":    "class ImageFilter:\n    def process(self):\n        return 'img'\n",
        "app/worker.py":       "from payments.gateway import Gateway\n\n"
                               "def run():\n    g = Gateway()\n    return g.process()\n",
    })
    _claim("PR-W:app/worker.py", "app/worker.py", "wdev", repo_b)
    _claim("PR-PAY:payments/gateway.py", "payments/gateway.py", "paydev", repo_b)
    _claim("PR-IMG:images/filter.py", "images/filter.py", "imgdev", repo_b)
    sb = _surface(repo_b)
    w_b = sb.get("PR-W", {})
    pay_b = sb.get("PR-PAY", {})
    img_b = sb.get("PR-IMG", {})
    w_b_cw = w_b.get("contested_with", []) or []
    checks.append(("B: worker IMPORTS payments.gateway + calls process → WARN vs payments (real coupling kept) "
                   f"(verdict='{w_b.get('verdict')}', contested_with={w_b_cw})",
                   w_b.get("verdict") == "warn" and any("paydev" in str(x) for x in w_b_cw)))
    checks.append(("B: payments side mirrors the warn (symmetric) "
                   f"(verdict='{pay_b.get('verdict')}')",
                   pay_b.get("verdict") == "warn"))
    checks.append(("B: the UNRELATED images/filter is NOT contested (wrong same-name definer dropped) "
                   f"(verdict='{img_b.get('verdict')}')",
                   img_b.get("verdict") == "clear" and not (img_b.get("contested_with") or [])))

    # ----------------------------------------------------------------------------------------------
    # C) RECALL — single-definer, no import: unambiguous → must STILL warn (the fix must not over-drop).
    repo_c = "fc/recall-single"
    _ingest(repo_c, {
        "core/engine.py": "def transmogrify(x):\n    return x\n",
        "app/caller.py":  "def run():\n    return transmogrify(1)\n",   # no import line, exactly one definer
    })
    _claim("PR-E:core/engine.py", "core/engine.py", "edev", repo_c)
    _claim("PR-C:app/caller.py", "app/caller.py", "cdev", repo_c)
    sc = _surface(repo_c)
    e_c = sc.get("PR-E", {})
    c_c = sc.get("PR-C", {})
    checks.append(("C: single-definer 'transmogrify' called with NO import → STILL warns (unambiguous; recall kept) "
                   f"(caller verdict='{c_c.get('verdict')}', definer verdict='{e_c.get('verdict')}')",
                   c_c.get("verdict") == "warn" and e_c.get("verdict") == "warn"))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("FALSE-COUPLING PRECISION GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
