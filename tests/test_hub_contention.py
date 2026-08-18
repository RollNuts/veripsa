#!/usr/bin/env python3
"""HUB / HOTSPOT dampening gate — a popular shared file must NOT make Veripsa over-warn (the noise death).

The classic failure of graph-coupling tools: a HUB file (utils.py imported by N files) is graph-adjacent to
ALL its importers, so a PR touching the hub gets flagged as colliding with EVERY in-flight PR in its
neighborhood — a wall of warnings that gets the whole product ignored. This builds exactly that shape against
the REAL gate and proves:
  1. WITHOUT dampening (hub cutoff raised) the hub PR explodes — contested with all N importers.
  2. WITH dampening (default cutoff) the hub PR is NOT contested with its importers (noise gone).
  3. a DIRECT same-file collision still SERIALIZES (hub dampening never weakens the exact-path lock).
  4. a NORMAL (non-hub) cross-file coupling still WARNS (we suppressed only the hub noise, not real signal).
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

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run → "does not exist".
# Per-PID, exactly like db/smoke.sh (veripsa_smoke_$$), run_gates (veripsa_gates_$$), test_server.py.
DB = "veripsa_hubtest_" + str(os.getpid())
REPO = "acme/hub"
N = 20   # leaf importers of the import hub
M = 12   # files all touching the same HOT shared table (resource hub; > the default cutoff 8)


def db(role, sql, args=(), hub_degree=None):
    conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            if hub_degree is not None:                       # tune the hub cutoff on THIS connection (GUC)
                cur.execute("SET veripsa.hub_degree = %s", (str(hub_degree),))
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def build_graph():
    import code_graph_extract as X
    with tempfile.TemporaryDirectory() as d:
        # a HUB: hub.py imported by N leaves
        with open(os.path.join(d, "hub.py"), "w") as fh:
            fh.write("def helper():\n    return 1\n")
        for i in range(N):
            with open(os.path.join(d, f"leaf_{i}.py"), "w") as fh:
                fh.write(f"from hub import helper\n\ndef use_{i}():\n    return helper()\n")
        # a NORMAL (non-hub) coupling: pair_a imports pair_b (pair_b imported by exactly 1 → not a hub)
        with open(os.path.join(d, "pair_b.py"), "w") as fh:
            fh.write("def only():\n    return 2\n")
        with open(os.path.join(d, "pair_a.py"), "w") as fh:
            fh.write("from pair_b import only\n\ndef go():\n    return only()\n")
        # a SOLO file (no coupling) for the direct same-file collision test
        with open(os.path.join(d, "solo.py"), "w") as fh:
            fh.write("def alone():\n    return 3\n")
        # a HOT SHARED TABLE: M .sql files all ALTER the same table `hot` (a hot table half the repo touches).
        # Without resource-hub dampening, editing one would couple to all the others (res_adj explosion).
        for i in range(M):
            with open(os.path.join(d, f"mig_{i}.sql"), "w") as fh:
                fh.write(f"ALTER TABLE hot ADD COLUMN c{i} integer;\n")
        return X.build_graph(d)


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    graph = build_graph()
    db("veripsa_app", "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, "main", "a" * 40))

    def claim(cid, path, author):
        db("veripsa_app", "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (cid, path, REPO, "main", author))

    claim("PR-HUB:hub.py", "hub.py", "hubdev")
    for i in range(N):
        claim(f"PR-L{i}:leaf_{i}.py", f"leaf_{i}.py", f"leafdev{i}")
    claim("PR-A:pair_a.py", "pair_a.py", "adev")
    claim("PR-B:pair_b.py", "pair_b.py", "bdev")
    claim("PR-S1:solo.py", "solo.py", "sdev1")
    claim("PR-S2:solo.py", "solo.py", "sdev2")             # same file, different author → must serialize
    for i in range(M):                                     # M PRs each editing one migration on the HOT table
        claim(f"PR-R{i}:mig_{i}.sql", f"mig_{i}.sql", f"resdev{i}")

    def impact(hub_degree):
        imp = db("veripsa_app", "SELECT core.main_impact_surface(%s,%s)", (REPO, "main"), hub_degree=hub_degree)
        if isinstance(imp, str):
            imp = json.loads(imp)
        return {c["change_id"]: c for c in imp.get("changes", [])}

    # 1) WITHOUT dampening (cutoff raised above N) → the hub explodes.
    undamped = impact(hub_degree=100)
    hub_undamped = len(undamped.get("PR-HUB", {}).get("contested_with", []) or [])

    # 2) WITH dampening (default cutoff 8 < N) → the hub no longer warns about its importers.
    damped = impact(hub_degree=8)
    hubc = damped.get("PR-HUB", {})
    hub_damped = len(hubc.get("contested_with", []) or [])
    pra = damped.get("PR-A", {})
    pra_contested = pra.get("contested_with", []) or []
    prs2 = damped.get("PR-S2", {})
    prs1 = damped.get("PR-S1", {})
    res_undamped = len(undamped.get("PR-R0", {}).get("contested_with", []) or [])
    res_damped = len(damped.get("PR-R0", {}).get("contested_with", []) or [])

    # AUDIT3 unknown-first invariant over hub dampening: the hub PR has REAL (extracted) import couplings to its
    # in-flight importers that dampening suppressed. It must NOT 'warn' (that is the noise explosion we suppress)
    # and must NOT 'clear' (that was the SILENT false-clear bug — a missed collision rendered safe). The honest
    # verdict is 'unknown', and the suppressed couplings are SURFACED in dampened_with so silence is visible.
    hub_damp_with = hubc.get("dampened_with", []) or []
    checks = []
    checks.append((f"WITHOUT dampening the hub EXPLODES: contested with all {N} importers (got {hub_undamped})",
                   hub_undamped == N))
    checks.append((f"WITH dampening the hub PR is NOT contested with its importers (noise gone; got {hub_damped})",
                   hub_damped == 0))
    checks.append((f"UNKNOWN-FIRST: the dampened hub PR is NEVER a silent 'clear' (got '{hubc.get('verdict')}')",
                   hubc.get("verdict") != "clear"))
    checks.append((f"UNKNOWN-FIRST: the dampened hub PR does NOT explode to 'warn' either (got '{hubc.get('verdict')}')",
                   hubc.get("verdict") != "warn"))
    checks.append((f"UNKNOWN-FIRST: the dampened hub PR is honest 'unknown' (got '{hubc.get('verdict')}')",
                   hubc.get("verdict") == "unknown"))
    checks.append((f"VISIBILITY: the suppressed couplings are surfaced in dampened_with (got {len(hub_damp_with)} entries)",
                   len(hub_damp_with) >= 1 and all("via_hub" in e and "by" in e for e in hub_damp_with)))
    checks.append((f"a NORMAL non-hub coupling STILL warns: PR-A contested with bdev (got {pra_contested})",
                   any("bdev" in str(x) for x in pra_contested) and pra.get("verdict") == "warn"))
    checks.append((f"a DIRECT same-file collision STILL serializes: PR-S2 verdict (got '{prs2.get('verdict')}')",
                   prs2.get("verdict") == "serialize" or prs1.get("verdict") == "serialize"))
    checks.append((f"WITHOUT dampening a HOT TABLE explodes: PR-R0 contested with the other {M-1} touchers (got {res_undamped})",
                   res_undamped == M - 1))
    checks.append((f"WITH dampening a HOT shared table no longer couples its touchers (got {res_damped})",
                   res_damped == 0))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("HUB CONTENTION GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
