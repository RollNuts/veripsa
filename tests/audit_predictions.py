#!/usr/bin/env python3
"""End-to-end PREDICTION audit on a real repo — graph → prediction → verdict, proven on real couplings.

The ultimate product validation (and 'run it on your repo' sales evidence): point it at a checkout, and it
auto-picks a REAL coupling (two files joined by an import edge) and a REAL non-coupling (two files with no
direct edge), registers them as in-flight PRs by different authors, and asserts core.main_impact_surface:
  • the coupled pair is WARNED/contested  (recall — a real coupling is caught: a missed collision is what we
    sell against)
  • the uncoupled pair is CLEAR           (precision — no over-warning: over-warning is the #1 adoption killer)

Needs local Postgres with the veripsa roles. Not a hermetic gate (needs a checkout). Run by hand:
  python3 tests/audit_predictions.py [path-to-a-repo-checkout]   (defaults to the latest /tmp/audit-flask-*)
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import code_graph_extract as X  # noqa: E402
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): this audit bootstraps + drops its own DB, so a FIXED name lets concurrent
# runs drop each other's DB mid-run. Per-PID, like db/smoke.sh (veripsa_smoke_$$) + run_gates (veripsa_gates_$$).
DB = "veripsa_predaudit_" + str(os.getpid())
REPO = "audit/real"


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


_TEST_OR_EXAMPLE = ("test", "tests", "spec", "__tests__", "examples", "example", "fixtures")


def _primary_source_files(fp):
    """Real source files in the PRIMARY tree — exclude tests/examples/fixtures and package markers
    (__init__/index): those couple to whole packages and make poor 'unrelated' picks."""
    out = []
    for p in fp:
        # Kotlin/Swift (#49) + Java included so the end-to-end prediction audit also covers the
        # languages that build_graph now resolves to real file->file edges (a Kotlin repo like okio has
        # hundreds of kt->kt couplings — without .kt here the pair-picker found nothing and the audit
        # aborted on a perfectly healthy repo).
        if not p.endswith((".py", ".ts", ".tsx", ".js", ".go", ".rb", ".kt", ".swift", ".java")):
            continue
        if any(seg in _TEST_OR_EXAMPLE for seg in p.split("/")):
            continue
        if os.path.splitext(os.path.basename(p))[0] in ("__init__", "index"):
            continue
        out.append(p)
    return sorted(out)


def pick_pairs(g):
    """A real COUPLED pair (an import edge between two PRIMARY-source files) + a real UNCOUPLED pair (two
    primary-source files in different dirs with NO direct import edge either way).

    The coupled pair must NOT target a HUB: the engine deliberately dampens adjacency THROUGH a widely-
    imported file (utils / a base class / a shared module imported by > hub_degree files) so a hub PR does
    not flag every importer — that is correct precision, not a missed coupling. Picking a hub edge here
    made the audit falsely FAIL on a perfectly healthy repo (real example: rack/protection.rb in sinatra,
    imported by 22 middlewares → a hub → correctly NOT warned, but the audit read it as a recall miss).
    So we exclude any dst whose importer-degree exceeds the hub cutoff (mirror core's veripsa.hub_degree
    default of 8), matching what the engine will actually warn on."""
    import collections
    HUB_DEGREE = 8                                              # mirrors core.veripsa.hub_degree default
    fp = {n["path"] for n in g["nodes"] if n.get("kind") == "file"}
    src = set(_primary_source_files(fp))
    res = [(e["src"], e["dst"]) for e in g["edges"]
           if e["kind"] == "imports" and e["dst"] in fp and e["src"] in src and e["dst"] in src and e["src"] != e["dst"]]
    undirected = {frozenset((s, d)) for s, d in res}
    # importer-degree over the WHOLE resolved import graph (not just primary→primary): a hub is dampened
    # by how many files depend on it overall, exactly as the engine computes hub_files.
    indeg = collections.Counter(e["dst"] for e in g["edges"]
                                if e["kind"] == "imports" and e["dst"] in fp and e["src"] != e["dst"])
    non_hub = [(s, d) for s, d in res if indeg[d] <= HUB_DEGREE and indeg[s] <= HUB_DEGREE]
    coupled = non_hub[0] if non_hub else (res[0] if res else None)
    # Whether the coupled pick is a REAL testable coupling (non-hub) or a FORCED hub fallback. When a
    # repo's ONLY internal imports go through a hub (a single-umbrella-module library: every file does
    # `import Kingfisher` → Sources/General/Kingfisher.swift, indeg 76), there is NO non-hub coupling to
    # test recall on — the engine CORRECTLY dampens the hub to 'unknown' (precision, not a miss), so the
    # recall scenario is INCONCLUSIVE on this repo, not a FAIL. The caller skips recall when this is False.
    coupled_is_nonhub = bool(non_hub)
    pick_pairs.coupled_is_nonhub = coupled_is_nonhub   # surfaced to main (avoids a signature change)

    files = sorted(src)
    used = set(coupled or ())
    uncoupled = None
    for i, a in enumerate(files):
        if a in used:
            continue
        for b in files[i + 1:]:
            if b in used or os.path.dirname(a) == os.path.dirname(b):
                continue
            if frozenset((a, b)) not in undirected:
                uncoupled = (a, b)
                break
        if uncoupled:
            break
    return coupled, uncoupled


def verdict_of(surf, change_id):
    for c in surf.get("changes", []):
        if c.get("change_id") == change_id:
            return c
    return {}


def _surface(db):
    s = db("SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
    return s if isinstance(s, dict) else json.loads(s)


def main() -> int:
    flask = sorted(glob.glob("/tmp/audit-flask-*"))
    other = sorted(glob.glob("/tmp/audit-*"))
    root = sys.argv[1] if len(sys.argv) > 1 else ((flask or other or [None])[-1])
    if not root or not os.path.isdir(root):
        print("usage: python3 tests/audit_predictions.py <repo-checkout>  (no /tmp/audit-* clone found)"); return 2
    g = X.build_graph(root)
    coupled, uncoupled = pick_pairs(g)
    if not coupled or not uncoupled:
        print(f"could not auto-pick pairs (coupled={coupled}, uncoupled={uncoupled})"); return 2

    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    try:
        db = make_db("veripsa_app")
        db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
           (json.dumps({"nodes": g["nodes"], "edges": g["edges"]}), REPO, "main", "a" * 40))
        print(f"== end-to-end prediction audit: {root} ==")

        # SCENARIO A (recall): two PRs on a REAL import coupling → both must WARN + contest each other.
        db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (f"PR-1:{coupled[0]}", coupled[0], REPO, "main", "alice"))
        db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (f"PR-2:{coupled[1]}", coupled[1], REPO, "main", "bob"))
        sa = _surface(db)
        v1, v2 = verdict_of(sa, "PR-1"), verdict_of(sa, "PR-2")
        print(f"  COUPLED   PR-1 {coupled[0]}  (imports →)  PR-2 {coupled[1]}")
        print(f"     PR-1 verdict={v1.get('verdict')} contested_with={v1.get('contested_with')} depends_on_changing={v1.get('depends_on_changing')}")
        print(f"     PR-2 verdict={v2.get('verdict')} contested_with={v2.get('contested_with')}")
        # release scenario A so it cannot cross-couple to scenario B
        db("SELECT core.release_change_on_main_with_authority(%s,%s,%s)", ("PR-1", REPO, "main"))
        db("SELECT core.release_change_on_main_with_authority(%s,%s,%s)", ("PR-2", REPO, "main"))

        # SCENARIO B (precision): two PRs on UNRELATED files (no edge) → both must be CLEAR.
        db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (f"PR-3:{uncoupled[0]}", uncoupled[0], REPO, "main", "carol"))
        db("SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s)", (f"PR-4:{uncoupled[1]}", uncoupled[1], REPO, "main", "dave"))
        sb = _surface(db)
        v3, v4 = verdict_of(sb, "PR-3"), verdict_of(sb, "PR-4")
        print(f"  UNCOUPLED PR-3 {uncoupled[0]}   |   PR-4 {uncoupled[1]}")
        print(f"     PR-3 verdict={v3.get('verdict')} contested_with={v3.get('contested_with')}")
        print(f"     PR-4 verdict={v4.get('verdict')} contested_with={v4.get('contested_with')}")

        checks = []
        coupled_caught = (v1.get("verdict") in ("warn", "serialize") and v2.get("verdict") in ("warn", "serialize")
                          and any("PR-2" in str(x) for x in (v1.get("contested_with") or []))
                          and any("PR-1" in str(x) for x in (v2.get("contested_with") or [])))
        # When the repo's only internal couplings go through a hub (single-umbrella-module library), the
        # engine correctly DAMPENS the coupled pair to 'unknown' — recall is INCONCLUSIVE here, not a miss.
        recall_testable = getattr(pick_pairs, "coupled_is_nonhub", True)
        if recall_testable:
            checks.append(("RECALL: a real import coupling is CAUGHT (PR-1 ↔ PR-2 warn + contest each other)",
                           coupled_caught))
        else:
            print("  [SKIP] RECALL: only hub couplings in this repo (umbrella module) — the engine "
                  "correctly dampens the hub pair to 'unknown'; recall is inconclusive here, not a miss.")
        uncoupled_clean = (v3.get("verdict") == "clear" and v4.get("verdict") == "clear")
        checks.append(("PRECISION: unrelated files are CLEAR (PR-3 / PR-4 clear, no over-warning)", uncoupled_clean))

        ok = True
        for name, cond in checks:
            print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
            ok = ok and bool(cond)
        print("PREDICTION AUDIT:", "PASS" if ok else ("FAIL" if recall_testable else "PASS (recall inconclusive — hub-only repo)"))
        return 0 if ok else 1
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
