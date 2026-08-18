#!/usr/bin/env python3
"""INTERACTION gate — #127 soften-same-line × #122 freshness-gated symbol demotion (× #130 conflict signal).

These three features each shipped on `main` from a concurrent session and were proven INDIVIDUALLY:
  - #122 freshness-gated demotion (test_stale_demotion.py): a STALE graph must NOT silently demote a real
    SAME-symbol collision to a non-serialize verdict — when the graph's file hash != the PR's base hash the
    symbol spans are unverifiable, so the file-level 'serialize' is KEPT (recall held, no silent miss).
  - #127 soften same-line collisions (test_lowvalue_collision.py): a surviving collision ONLY on append-mostly
    build/test-RUNNER / registration-list files (run_gates.sh and kin, plus an OWNER-EXTENSIBLE basename glob)
    softens from the hard "wait in line" to a low-stakes 'serialize_soft' — STILL surfaced, never silent.
  - #130 mechanical conflict anticipation (test_conflict_anticipation.py): a separate, ADDITIVE axis — even a
    softened collision still forewarns a likely git merge conflict.

But NO existing gate proved the THREE-WAY COMPOSITION, and it had a real hole (found in audit/review-codex-
features):

  THE BUG: `low_value` (the soften trigger) was computed from the PATH alone, INDEPENDENT of the freshness /
  symbol relation. The built-in allowlist is all .sh runner scripts (no def/class spans → the finer engine can
  only ever return 'unknown' there, never 'same') so the built-ins compose safely. BUT the owner-extension glob
  (`low_value_collision_globs`) can be pointed at a file that DOES have symbols (e.g. `*.py`). Then a PROVEN
  same-symbol collision — INCLUDING one #122 deliberately KEPT hard 'serialize' because the graph was STALE —
  was SOFTENED to a non-blocking 'serialize_soft', re-introducing exactly the silent under-warn #122 exists to
  prevent. (Plus a contract bug: the owner glob was read as raw SQL-LIKE, so the documented `*.py` silently
  matched NOTHING while an undocumented `%.py` matched.)

  THE FIX (db/schema/80_contention.sql): (1) translate the owner GLOB (`*`,`?`) to SQL LIKE, escaping the
  owner's literal LIKE metacharacters first, so the documented syntax works; (2) a wait is `low_value` ONLY when
  its path is low-value AND the finer engine did NOT prove it a 'same'-symbol collision — a proven same-symbol
  overlap (stale OR fresh) is a real logic collision and stays HARD regardless of the file's name/glob.

This gate locks the composition end to end against the REAL gate + extractor + engine, content-free:

  (1) GLOB SEMANTICS: the documented filesystem glob `*.py` now matches a `.py` basename (was a silent no-op);
      a literal-dot basename glob still matches only that name (no accidental wildcard from the owner string).
  (2) THE INTERACTION SILENT-MISS: a file the owner globbed low-value (`*.py`) carrying a STALE (unverifiable-
      freshness) SAME-symbol collision stays HARD 'serialize' — soften does NOT mask a real, kept-by-freshness
      collision. (The exact #127×#122 hole.)
  (3) GUARD PRECISION (no over-serialize): the SAME globbed `.py`, but a FRESH, symbol-DISJOINT pair, is still
      correctly dropped (not serialized) — the guard does not break #122's precision win.
  (4) NO REGRESSION: a genuine runner-list collision (run_gates.sh, no symbols → 'unknown', never 'same') still
      SOFTENS to 'serialize_soft' — the anti-wallpaper win is intact.
  (5) #130 STILL RIDES ALONGSIDE: the now-HARD stale same-symbol collision (2) ALSO carries merge_conflict_
      likely (the additive conflict signal is independent of the severity axis and survives the guard).
  (6) CONTENT-FREE throughout: no diff body / secret reaches the engine surface.

Run:  python3 tests/test_soften_freshness_interaction.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a per-PID name lets concurrent runs /
# sibling agents not drop each other's DB mid-run (matches db/smoke.sh, run_gates, test_stale_demotion).
DB = "veripsa_softfresh_" + str(os.getpid())
REPO = "acme/softfresh"
ACCT = "ACCT-DEMO"

# A SECRET-LOOKING token that lives ONLY in a diff BODY. If it ever reaches the engine surface the content-free
# contract is broken. The whole pipeline must derive its signal from line ranges + symbol spans, never bytes.
SECRET = "SUPER_SECRET_SOFTFRESH_42xyz"

GRAPH_HASH = "b" * 40   # the hash the graph was INGESTED at (the symbol spans are valid for THIS version)
STALE_BASE = "f" * 40   # a PR base hash that DIFFERS from the graph hash → freshness unverifiable (the stale case)


def db(role, sql, args=()):
    conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    import render as R

    checks = []

    def ingest(graph):
        db("veripsa_app", "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)",
           (json.dumps(graph), REPO, "main", "a" * 40))

    def set_policy(key, value):
        db("veripsa_app", "SELECT core.set_policy_with_authority(%s,%s)", (key, value))

    def claim(cid, path, author, ranges, base_hash):
        rj = json.dumps(ranges) if ranges else None
        db("veripsa_app", "SELECT core.act_for_claim_with_authority(%s,%s,%s,%s,%s,%s::jsonb,%s)",
           (cid, path, REPO, "main", author, rj, base_hash))

    def reset_claims():
        conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account',%s,true)", (ACCT,))
            cur.execute("SELECT set_config('core.governed_write_token','claim',true)")
            cur.execute("UPDATE core.claim SET claim_state='released', released_at=now() WHERE claim_state IN ('active','waiting')")
        conn.close()

    def low_value(path):
        conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT set_config('core.current_account',%s,true)", (ACCT,))
                cur.execute("SELECT core._is_low_value_collision_path(%s)", (path,))
                return cur.fetchone()[0]
        finally:
            conn.close()

    def impact():
        imp = db("veripsa_app", "SELECT core.main_impact_surface(%s,%s)", (REPO, "main"))
        if isinstance(imp, str):
            imp = json.loads(imp)
        return {c["change_id"]: c for c in imp.get("changes", [])}, imp

    # The graph: pipeline.py (a REAL python source: alpha 1-3, beta 7-9) + run_gates.sh (a low-value runner, no
    # symbols). pipeline.py is NOT a built-in low-value file — the owner will glob it low-value to exercise the seam.
    graph = {"nodes": [
        {"id": "pipeline.py", "kind": "file", "path": "pipeline.py", "language": "python", "content_hash": GRAPH_HASH},
        {"id": "pipeline.py::alpha", "kind": "def", "name": "alpha", "path": "pipeline.py", "language": "python",
         "start_line": 1, "end_line": 3, "content_hash": GRAPH_HASH},
        {"id": "pipeline.py::beta", "kind": "def", "name": "beta", "path": "pipeline.py", "language": "python",
         "start_line": 7, "end_line": 9, "content_hash": GRAPH_HASH},
        {"id": "run_gates.sh", "kind": "file", "path": "run_gates.sh", "language": "shell", "content_hash": "c" * 40},
    ]}
    ingest(graph)

    # ── (1) GLOB SEMANTICS: the documented filesystem glob must actually work. ─────────────────────────────────
    checks.append(("(1) BEFORE any owner glob: pipeline.py is NOT low-value (only the built-in .sh runners are)",
                   low_value("pipeline.py") is False))
    set_policy("low_value_collision_globs", "*.py")
    checks.append(("(1) the documented filesystem glob `*.py` now matches a .py basename (was a silent no-op — "
                   "raw SQL-LIKE read `*` as a literal char)", low_value("pipeline.py") is True))
    checks.append(("(1) a built-in runner (run_gates.sh) stays low-value regardless of the owner glob",
                   low_value("run_gates.sh") is True))
    checks.append(("(1) an un-globbed, non-runner source file (server.go) is NOT low-value (the glob is narrow)",
                   low_value("server.go") is False))
    # the owner's literal `.` must NOT act as a LIKE `_` wildcard (escaped before glob-translation): `pipeline.py`
    # as a glob matches `pipeline.py` but NOT `pipelineXpy` (a bare `_`-as-dot would falsely match).
    set_policy("low_value_collision_globs", "pipeline.py")
    checks.append(("(1) a literal basename glob matches exactly (pipeline.py) and the `.` is not a wildcard "
                   "(pipelineXpy does NOT match)",
                   low_value("pipeline.py") is True and low_value("pipelineXpy") is False))
    set_policy("low_value_collision_globs", "*.py")   # restore the broad glob for the interaction tests

    # ── (2) THE INTERACTION SILENT-MISS (#127 × #122): a file the owner globbed low-value, carrying a STALE
    #    (unverifiable-freshness) SAME-symbol collision, MUST stay HARD 'serialize'. Soften must not mask a real
    #    collision #122 deliberately kept. Both PRs edit alpha (lines 1-3) but their base hash != the graph hash
    #    → freshness unverifiable → the finer engine keeps it file-level; the guard then refuses to soften it. ──
    reset_claims()
    claim("PR-A:pipeline.py", "pipeline.py", "alice", [[1, 3]], STALE_BASE)   # alpha, stale base
    claim("PR-B:pipeline.py", "pipeline.py", "bob", [[1, 3]], STALE_BASE)     # alpha — SAME symbol, stale base
    ch, imp = impact()
    prB = ch.get("PR-B", {})
    checks.append((f"(2) THE FIX — a STALE same-symbol collision on an owner-globbed low-value (*.py) file stays "
                   f"HARD 'serialize', NOT softened (verdict='{prB.get('verdict')}', "
                   f"serialize_count={imp.get('serialize_count')}, soft={imp.get('serialize_soft_count')})",
                   prB.get("verdict") == "serialize"
                   and imp.get("serialize_count") == 1 and imp.get("serialize_soft_count") == 0))

    # ── (3) GUARD PRECISION (no over-serialize): the SAME globbed *.py, but a FRESH, symbol-DISJOINT pair (alpha
    #    vs beta, base hash == graph hash) must STILL be dropped — the soften-guard must not break #122's win. ──
    reset_claims()
    claim("PR-C:pipeline.py", "pipeline.py", "carol", [[1, 3]], GRAPH_HASH)   # alpha
    claim("PR-D:pipeline.py", "pipeline.py", "dave", [[7, 9]], GRAPH_HASH)    # beta — disjoint, FRESH → dropped
    ch, imp = impact()
    prD = ch.get("PR-D", {})
    checks.append((f"(3) GUARD PRECISION: a FRESH symbol-DISJOINT pair on the SAME globbed *.py is still DROPPED "
                   f"(not serialized/softened) (verdict='{prD.get('verdict')}', "
                   f"serialize={imp.get('serialize_count')}, soft={imp.get('serialize_soft_count')})",
                   prD.get("verdict") not in ("serialize", "serialize_soft")
                   and imp.get("serialize_count") == 0 and imp.get("serialize_soft_count") == 0))

    # ── (4) NO REGRESSION: a genuine runner-list collision (run_gates.sh has NO symbols → 'unknown', never
    #    'same') still SOFTENS to 'serialize_soft' — the guard only refuses to soften PROVEN same-symbol pairs. ──
    reset_claims()
    claim("PR-E:run_gates.sh", "run_gates.sh", "erin", [[205, 210]], "c" * 40)
    claim("PR-F:run_gates.sh", "run_gates.sh", "frank", [[205, 210]], "c" * 40)
    ch, imp = impact()
    prF = ch.get("PR-F", {})
    checks.append((f"(4) NO REGRESSION: a genuine run_gates.sh append collision (no symbols → unknown, never "
                   f"same) still SOFTENS (verdict='{prF.get('verdict')}', soft={imp.get('serialize_soft_count')})",
                   prF.get("verdict") == "serialize_soft" and imp.get("serialize_soft_count") >= 1))

    # ── (5) #130 RIDES ALONGSIDE the hardened verdict: re-establish the STALE same-symbol pair (case 2) and prove
    #    the mechanical merge-conflict signal still fires (overlapping ranges) — additive, independent of severity. ─
    reset_claims()
    claim("PR-G:pipeline.py", "pipeline.py", "gina", [[1, 3]], STALE_BASE)
    claim("PR-H:pipeline.py", "pipeline.py", "hank", [[1, 3]], STALE_BASE)   # overlapping ranges → conflict-likely
    ch, imp = impact()
    prH = ch.get("PR-H", {})
    rendered = R.render_pr_check(imp, "PR-H")
    body = (rendered.get("summary") or "") + "\n" + (rendered.get("comment") or "")
    checks.append((f"(5) #130 ADDITIVE: the now-HARD stale same-symbol collision (verdict='{prH.get('verdict')}') "
                   f"ALSO carries merge_conflict_likely (got {prH.get('merge_conflict_likely')})",
                   prH.get("verdict") == "serialize" and prH.get("merge_conflict_likely") is True))
    checks.append(("(5) the rendered HARD-serialize comment names the wait-in-line AND the conflict heads-up "
                   "(both axes surfaced honestly)",
                   "wait in line" in body.lower() and "merge conflict" in body.lower()))

    # ── (6) CONTENT-FREE: the diff body / secret never reaches the engine surface (the signal is line + span only).
    surface_blob = json.dumps(imp)
    checks.append(("(6) CONTENT-FREE: the secret token never appears in the engine surface output",
                   SECRET not in surface_blob))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("SOFTEN×FRESHNESS INTERACTION GATE:", "PASS" if ok else "FAIL")
    subprocess.run(["dropdb", DB], cwd=ROOT, capture_output=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
